#!/usr/bin/env python3
"""Collect Doberman AKC records from registration-number candidates in batches."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

ROOT = Path(__file__).resolve().parents[1]
AKC_SCRAPER_DIR = ROOT / "akc-scrape"
sys.path.insert(0, str(AKC_SCRAPER_DIR))

from dataset_journal import DatasetJournal
from akc_pedigree_scraper import (
    AkcFormError,
    AkcLookupError,
    CONTACT_FIELDS,
    ENTRY_FORM_VALUE,
    MANAGE_DOGS_URL,
    POPUP_EVENT_TIMEOUT,
    apply_schema,
    build_pdf_number_index,
    choose_dog_by_number,
    detail_value,
    dog_details,
    medical_screens,
    managed_row,
    PlaywrightError,
    PlaywrightTimeoutError,
    select_managed_row,
    wait_for_context_page,
    extract_pdf,
    hydrate_record,
    log_step,
    mapped_pdf_ids,
    refresh_relationship_schema,
    reusable_pdf,
    same_breed,
    save_records,
    serialized_record,
    sync_playwright,
    login,
)

NUMBER_PATTERN = re.compile(r"^[A-Z]{1,3}\d{5,8}$")


def load_json(path: Path, fallback: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else fallback


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def normalize_number(value: str) -> str:
    return re.sub(r"\s+", "", value.upper())


def candidate_numbers(numbers_path: Path | None, dobequest_path: Path | None) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    if numbers_path and numbers_path.exists():
        for line in numbers_path.read_text(encoding="utf-8").splitlines():
            number = normalize_number(line)
            if NUMBER_PATTERN.fullmatch(number):
                candidates.setdefault(number, {"sources": []})["sources"].append({"type": "file"})
    if dobequest_path and dobequest_path.exists():
        raw = load_json(dobequest_path, {})
        for key, profile in raw.items():
            number = normalize_number(str(profile.get("akcNum") or ""))
            if not NUMBER_PATTERN.fullmatch(number):
                continue
            name = " ".join(
                part for part in (profile.get("titles"), profile.get("registeredName"), profile.get("suffix")) if part
            ).strip()
            candidate = candidates.setdefault(number, {"sources": []})
            candidate["sources"].append({
                "type": "dobequest",
                "dobequest_id": str(profile.get("did") or key),
                "name": name,
            })
    return candidates


def load_records(output: Path, journal: DatasetJournal) -> dict[str, dict[str, Any]]:
    dataset = load_json(output, {})
    records: dict[str, dict[str, Any]] = dataset.get("records", dataset)
    replayed = journal.replay(records)
    if replayed:
        print(f"Replayed {replayed} un-compacted dataset journal events.", flush=True)
    for record in records.values():
        hydrate_record(record)
    refresh_relationship_schema(records)
    return records


def known_numbers(records: dict[str, dict[str, Any]], downloads_dir: Path) -> set[str]:
    result = {normalize_number(str(record.get("akc_number") or "")) for record in records.values()}
    for pdf in downloads_dir.glob("*.pdf"):
        result.update(NUMBER_PATTERN.findall(pdf.stem.upper()))
    return result - {""}


def dobequest_provenance(candidate: dict[str, Any]) -> dict[str, Any] | None:
    source = next((item for item in candidate.get("sources", []) if item.get("type") == "dobequest"), None)
    if not source:
        return None
    return {"did": source["dobequest_id"], "match_status": "bulk_registration_candidate"}


def manifest_entry(manifest: dict[str, Any], number: str) -> dict[str, Any]:
    return manifest.setdefault("numbers", {}).setdefault(number, {"status": "pending"})


def update_manifest(manifest: dict[str, Any], path: Path, number: str, status: str, **fields: Any) -> None:
    entry = manifest_entry(manifest, number)
    entry.update(fields)
    entry["status"] = status
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_json(path, manifest)


def save_bulk_record(
    records: dict[str, dict[str, Any]],
    output: Path,
    pdf: Path,
    details: dict[str, Any],
    canonical_name: str,
    number: str,
    provenance: dict[str, Any] | None,
    journal: DatasetJournal,
) -> dict[str, Any]:
    record = extract_pdf(pdf)
    if str(record.get("akc_number") or "") != number:
        raise AkcFormError(f"Batch PDF contains AKC number {record.get('akc_number')!r}, expected {number!r}")
    record.update(details)
    record["akc_lookup_status"] = "resolved_by_bulk_akc_number"
    record["pdf_id"] = pdf.stem
    record["aliases"] = list(dict.fromkeys([canonical_name, record.get("name", "")]))
    record["collected_at"] = datetime.now(timezone.utc).isoformat()
    apply_schema(record, "resolved_by_bulk_akc_number", provenance)
    records[record["id"]] = record
    journal.upsert(record["id"], serialized_record(record))
    return record


def direct_number_lookup_add(page: Any, akc_number: str) -> tuple[str, str]:
    """Use MyAKC's authenticated registration lookup POST without an Add Dog popup."""
    lookup_url = "https://www.apps.akc.org/apps/online_accounts/dog_management/index_popup_dog.cfm?action=add_dog"
    response = page.context.request.post(
        lookup_url,
        form={"get_dog_by": "D", "dog_reg_num": akc_number, "litter_num": "", "dog_name": "", "bvgcode": "0"},
    )
    if not response.ok:
        raise AkcLookupError("number_mismatch", f"Direct AKC lookup returned HTTP {response.status} for {akc_number!r}.")
    body = response.text()
    if re.search(r"not found in our database|could not be found", body, re.I):
        raise AkcLookupError("number_mismatch", f"AKC could not resolve number {akc_number!r}.")
    number_match = re.search(r"name=['\"]dog_reg_num['\"][^>]*value=['\"]([^'\"]+)", body, re.I)
    name_match = re.search(r"<strong[^>]*>\s*(.*?)\s*</strong>", body, re.I | re.S)
    if not number_match or not name_match:
        raise AkcLookupError("number_mismatch", f"Direct AKC lookup returned unexpected HTML for {akc_number!r}.")
    resolved_number = unescape(number_match.group(1)).strip()
    canonical_name = re.sub(r"<[^>]+>", "", unescape(name_match.group(1))).strip()
    if resolved_number != akc_number:
        raise AkcLookupError("number_mismatch", f"AKC returned {resolved_number!r} for {akc_number!r}.")
    add_url = "https://www.apps.akc.org/apps/online_accounts/dog_management/index_popup_dog.cfm?action=add_dog_io"
    added = page.context.request.post(
        add_url,
        form={"dog_dio_int": "DOI", "dog_active": "NONE", "dog_reg_num": akc_number, "Submit": "Next >"},
    )
    if not added.ok:
        raise AkcLookupError("number_mismatch", f"Direct AKC Add Dog request returned HTTP {added.status} for {akc_number!r}.")
    return canonical_name, resolved_number


def direct_table_rows(page: Any, numbers: list[str]) -> dict[str, dict[str, str]]:
    """Fetch populated table row keys, retrying while MyAKC catches up with adds."""
    table_url = "https://www.apps.akc.org/apps/online_accounts/dog_management/index.cfm?action=refresh_index_add_1"
    missing: list[str] = numbers
    for attempt in range(3):
        table_response = page.context.request.get(table_url)
        if not table_response.ok:
            raise AkcFormError(f"Direct MyAKC table request returned HTTP {table_response.status}")
        table_html = table_response.text()
        rows: dict[str, dict[str, str]] = {}
        for akc_number in numbers:
            row_pattern = re.compile(
                rf"dog_reg_number={re.escape(akc_number)}(?:&|&amp;)ks_number=(\d+)(?:&|&amp;)Fixed_ID=(\d+)(?:&|&amp;)is_owned=(\d+)(?:&|&amp;)click_action=View(?:&|&amp;)ownership_type=([A-Z]+)",
                re.I,
            )
            match = row_pattern.search(table_html)
            if not match:
                continue
            key_stkhldr, fixed_id, is_owned, ownership_type = match.groups()
            rows[akc_number] = {"key_stkhldr": key_stkhldr, "fixed_id": fixed_id, "is_owned": is_owned, "ownership_type": ownership_type}
        missing = sorted(set(numbers) - set(rows))
        if not missing:
            return rows
        if attempt < 2:
            log_step(f"MyAKC table is missing {len(missing)} new row(s); retrying key discovery", 2)
            page.wait_for_timeout(750)
    raise AkcFormError(f"Direct MyAKC table response lacks row keys after retries: {', '.join(missing)}")


def direct_dog_details(page: Any, akc_number: str, row: dict[str, str] | None = None) -> dict[str, Any]:
    """Retrieve MyAKC Dog Information by HTTP using current table row keys."""
    row = row or direct_table_rows(page, [akc_number])[akc_number]
    key_stkhldr = row["key_stkhldr"]
    fixed_id = row["fixed_id"]
    is_owned = row["is_owned"]
    ownership_type = row["ownership_type"]
    detail_url = (
        "https://www.apps.akc.org/apps/online_accounts/dog_management/index_tab_iframe.cfm?"
        + urlencode(
            {
                "action": "refresh_index_iframe",
                "active_tab_row_A": "1",
                "active_tab_col_A": "1",
                "dog_reg_number": akc_number,
                "ks_number": key_stkhldr,
                "Fixed_ID": fixed_id,
                "is_owned": is_owned,
                "click_action": "View",
                "ownership_type": ownership_type,
            }
        )
    )
    detail_response = page.context.request.post(
        detail_url,
        form={
            "sort_by": "",
            "sort_dir": "",
            "focus_id": "",
            "focus_id1": "",
            "focus_id3": "1",
            "focus_id4": "",
            "display_list": "0",
            "select_list": "",
            "action_items": "",
        },
    )
    if not detail_response.ok:
        raise AkcFormError(f"Direct Dog Information request returned HTTP {detail_response.status} for {akc_number!r}")
    detail_html = detail_response.text()
    if akc_number not in detail_html:
        raise AkcFormError(f"Direct Dog Information response does not contain {akc_number!r}")
    text = re.sub(r"(?i)<br\s*/?>|</(?:td|tr|p|li|div|h[1-6])>", "\n", detail_html)
    text = re.sub(r"<[^>]+>", " ", text)
    lines = [line.strip() for line in unescape(text).splitlines() if line.strip()]
    screens = medical_screens(lines)
    return {
        "_myakc_breed": detail_value(lines, ("Breed",)),
        "breed_variety": detail_value(lines, ("Variety", "Breed Variety")),
        "color": detail_value(lines, ("Color",)),
        "markings": detail_value(lines, ("Markings",)),
        "ofa_number": screens.get("OFA Number", detail_value(lines, ("OFA No.", "OFA #"))),
        "medical_screens": screens,
    }


def direct_batch_chooser(page: Any, rows: dict[str, dict[str, str]], numbers: list[str]) -> dict[str, Any]:
    """Create one batch chooser through MyAKC's authenticated action request."""
    keys = [rows[number]["key_stkhldr"] for number in numbers]
    form = {
        "sort_by": "",
        "sort_dir": "",
        "focus_id": "",
        "focus_id1": "",
        "focus_id3": "1",
        "focus_id4": "",
        "display_list": "0",
        "select_list": "",
        "action_items": "6",
    }
    form.update({f"CB{index}": key for index, key in enumerate(keys)})
    action_url = "https://www.apps.akc.org/apps/online_accounts/dog_management/index.cfm?action=filters"
    action = page.context.request.post(action_url, form=form)
    if not action.ok:
        raise AkcFormError(f"Direct batch Create Forms request returned HTTP {action.status}")
    chooser_url = "https://www.apps.akc.org/apps/online_accounts/dog_management/blocks/dsp_choose_pdf_form.cfm?" + urlencode({"p_dogActionList": ",".join(keys), "report_type": "6"})
    chooser = page.context.request.get(chooser_url)
    if not chooser.ok:
        raise AkcFormError(f"Direct batch chooser request returned HTTP {chooser.status}")
    chooser_html = chooser.text()
    customer = re.search(r"name=['\"]key_customer['\"][^>]*value=['\"]([^'\"]+)", chooser_html, re.I)
    if not customer:
        raise AkcFormError("Direct batch chooser response is missing key_customer")
    return {"key_customer": unescape(customer.group(1)), "rows": rows}


def direct_batch_cleanup(page: Any, rows: dict[str, dict[str, str]]) -> None:
    """Clear a dedicated bulk account through one authenticated All My Dogs action."""
    form = {
        "sort_by": "",
        "sort_dir": "",
        "focus_id": "",
        "focus_id1": "",
        "focus_id3": "1",
        "focus_id4": "",
        "display_list": "0",
        "select_list": "0",
        "action_items": "1",
    }
    form.update({f"CB{index}": row["key_stkhldr"] for index, row in enumerate(rows.values())})
    response = page.context.request.post("https://www.apps.akc.org/apps/online_accounts/dog_management/index.cfm?action=filters", form=form)
    if not response.ok:
        raise AkcFormError(f"Direct batch cleanup request returned HTTP {response.status}")


def download_batch_entry_form_http_data(page: Any, chooser_data: dict[str, Any], akc_number: str, downloads_dir: Path) -> Path:
    """Generate one PDF from the shared direct batch chooser state."""
    key_stkhldr = chooser_data["rows"][akc_number]["key_stkhldr"]
    params = {
        "key_stkhldr": key_stkhldr,
        "dog_reg_num": akc_number,
        "isowner": "",
        "form": ENTRY_FORM_VALUE,
        "pdfType": "blank",
        "Useraction": "generatePDFEntryForm",
        "key_customer": chooser_data["key_customer"],
    }
    report_url = f"https://www.apps.akc.org/apps/dynamicpdfs/index.cfm?{urlencode(params)}"
    report = page.context.request.get(report_url)
    if not report.ok:
        raise AkcFormError(f"Direct MyAKC PDF report request returned HTTP {report.status} for {akc_number!r}")
    match = re.search(r'''href=["']([^"']*/akcecom/pdf/cms/[^"']+)["']''', report.text(), re.I)
    if not match:
        raise AkcFormError(f"Direct MyAKC PDF report has no PDF link for {akc_number!r}")
    pdf_url = urljoin(report.url, match.group(1).replace("&amp;", "&"))
    response = page.context.request.get(pdf_url)
    if not response.ok:
        raise AkcFormError(f"Could not download direct batch PDF for {akc_number!r}: HTTP {response.status}")
    target = downloads_dir / Path(urlparse(pdf_url).path).name
    target.write_bytes(response.body())
    return target


def open_batch_entry_form(page: Any, dogs: list[tuple[str, str, dict[str, Any]]]) -> Any:
    """Open one entry-form chooser containing only the current batch dogs."""
    page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
    checkboxes = page.locator("form#dog_management input[type='checkbox']")
    for checkbox in checkboxes.all():
        if checkbox.is_checked():
            checkbox.uncheck()
    for canonical_name, number, _ in dogs:
        row = managed_row(page, canonical_name, number)
        row.locator("input[type='checkbox']").check(timeout=8_000)
    selected = page.locator("form#dog_management input[type='checkbox']:checked")
    if selected.count() != len(dogs):
        raise AkcFormError(f"MyAKC selected {selected.count()} batch dogs; expected {len(dogs)}")
    page.locator("select[name='action_items']").select_option("6")
    try:
        with page.expect_popup(timeout=POPUP_EVENT_TIMEOUT) as chooser_info:
            page.locator("#go_filters").click()
        chooser = chooser_info.value
        chooser.wait_for_load_state("domcontentloaded")
    except PlaywrightTimeoutError:
        chooser = wait_for_context_page(page, "dsp_choose_pdf_form", 30_000)
        if not chooser:
            raise AkcFormError("MyAKC did not open the batch entry-form chooser")
    chooser.locator(f"input[name='form'][value='{ENTRY_FORM_VALUE}']").wait_for(timeout=30_000)
    chooser.locator(f"input[name='form'][value='{ENTRY_FORM_VALUE}']").check()
    for field_name in CONTACT_FIELDS:
        checkbox = chooser.locator(f"input[name='{field_name}']")
        if checkbox.is_checked():
            checkbox.uncheck()
    return chooser


def chooser_dog_radio(chooser: Any, akc_number: str) -> Any:
    """Locate a batch chooser dog radio in both multi- and single-dog markup."""
    radio = chooser.locator(f"input[name='key_stkhldr'][onclick*=\"set_reg_num('{akc_number}')\"]")
    if radio.count():
        return radio
    selected_number = chooser.locator("input[name='dog_reg_num']").input_value().strip()
    radios = chooser.locator("input[name='key_stkhldr']")
    if selected_number == akc_number and radios.count() == 1:
        return radios
    raise AkcFormError(f"Batch entry chooser does not include AKC number {akc_number!r}")


def download_batch_entry_form_http(page: Any, chooser: Any, akc_number: str, downloads_dir: Path) -> Path:
    """Generate a batch PDF through the chooser's authenticated report request."""
    radio = chooser_dog_radio(chooser, akc_number)
    key_stkhldr = radio.get_attribute("value") or ""
    key_customer = chooser.locator("input[name='key_customer']").input_value().strip()
    isowner = chooser.locator("input[name='isowner']").input_value().strip()
    if not key_stkhldr or not key_customer:
        raise AkcFormError(f"Batch chooser is missing report keys for {akc_number!r}")
    params = {
        "key_stkhldr": key_stkhldr,
        "dog_reg_num": akc_number,
        "isowner": isowner,
        "form": ENTRY_FORM_VALUE,
        "pdfType": "blank",
        "Useraction": "generatePDFEntryForm",
        "key_customer": key_customer,
    }
    report_url = f"https://www.apps.akc.org/apps/dynamicpdfs/index.cfm?{urlencode(params)}"
    report = page.context.request.get(report_url)
    if not report.ok:
        raise AkcFormError(f"Direct MyAKC PDF report request returned HTTP {report.status} for {akc_number!r}")
    report_text = report.text()
    match = re.search(r'''href=["']([^"']*/akcecom/pdf/cms/[^"']+)["']''', report_text, re.I)
    if not match:
        raise AkcFormError(f"Direct MyAKC PDF report has no PDF link for {akc_number!r}")
    pdf_url = urljoin(report.url, match.group(1).replace("&amp;", "&"))
    response = page.context.request.get(pdf_url)
    if not response.ok:
        raise AkcFormError(f"Could not download direct batch PDF for {akc_number!r}: HTTP {response.status}")
    target = downloads_dir / Path(urlparse(pdf_url).path).name
    target.write_bytes(response.body())
    return target


def download_batch_entry_form(page: Any, chooser: Any, akc_number: str, downloads_dir: Path) -> Path:
    """Generate one dog PDF while retaining the shared batch chooser."""
    radio = chooser_dog_radio(chooser, akc_number)
    radio.check()
    try:
        with chooser.expect_popup(timeout=POPUP_EVENT_TIMEOUT) as report_info:
            chooser.locator("input[type='button'][value='Submit']").click()
        report = report_info.value
        report.wait_for_load_state("domcontentloaded")
    except PlaywrightTimeoutError:
        report = wait_for_context_page(chooser, "/apps/dynamicpdfs/index.cfm", 30_000)
        if not report:
            raise AkcFormError(f"MyAKC did not open a PDF report for {akc_number!r}")
        try:
            report.locator("a[href*='/akcecom/pdf/cms/']").wait_for(timeout=30_000)
        except PlaywrightTimeoutError as error:
            raise AkcFormError(f"MyAKC report page did not expose a PDF link for {akc_number!r}") from error
    report_number = parse_qs(urlparse(report.url).query).get("dog_reg_num", [""])[0]
    if report_number and report_number != akc_number:
        raise AkcFormError(f"MyAKC batch report selected {report_number!r}, expected {akc_number!r}")
    href = report.locator("a[href*='/akcecom/pdf/cms/']").get_attribute("href")
    if not href:
        raise AkcFormError(f"MyAKC report page has no PDF link for {akc_number!r}")
    response = page.context.request.get(urljoin(report.url, href))
    if not response.ok:
        raise AkcFormError(f"Could not download batch PDF for {akc_number!r}: HTTP {response.status}")
    target = downloads_dir / Path(urlparse(urljoin(report.url, href)).path).name
    target.write_bytes(response.body())
    if report is not chooser and not report.is_closed():
        report.close()
    return target


def clear_all_dogs_of_interest(page: Any) -> None:
    """Use the dedicated account's MyAKC Select All removal action."""
    page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
    page.locator("select[name='select_list']").select_option("0")
    page.wait_for_timeout(250)
    checkboxes = page.locator("form#dog_management input[type='checkbox']")
    if checkboxes.count() and page.locator("form#dog_management input[type='checkbox']:checked").count() != checkboxes.count():
        raise RuntimeError("MyAKC Select All did not select every Dog of Interest row.")
    page.locator("select[name='action_items']").select_option("1")
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
            page.locator("#go_filters").click(no_wait_after=True)
    except PlaywrightTimeoutError:
        log_step("Bulk MyAKC cleanup navigation timed out; refreshing management page", 1)
        page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")


def process_batch(
    page: Any,
    batch: list[tuple[str, dict[str, Any]]],
    records: dict[str, dict[str, Any]],
    output: Path,
    downloads_dir: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
    breed: str,
    pdf_number_index: dict[str, list[Path]],
    journal: DatasetJournal,
    http_optimization: bool,
) -> int:
    added: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
    discovered: list[tuple[str, str, dict[str, Any]]] = []
    saved_count = 0
    for position, (number, candidate) in enumerate(batch, 1):
        print(f"\n{number}\n{'-' * 72}\nBatch dog {position}/{len(batch)}", flush=True)
        update_manifest(manifest, manifest_path, number, "processing", sources=candidate["sources"])
        try:
            if http_optimization:
                try:
                    canonical_name, resolved_number = direct_number_lookup_add(page, number)
                except AkcLookupError as error:
                    log_step(f"Direct registration request failed; using browser fallback: {error}", 1)
                    canonical_name, resolved_number = choose_dog_by_number(page, number)
            else:
                canonical_name, resolved_number = choose_dog_by_number(page, number)
            if resolved_number != number:
                update_manifest(manifest, manifest_path, number, "number_mismatch", returned_number=resolved_number)
                continue
            discovered.append((canonical_name, resolved_number, candidate))
        except AkcLookupError as error:
            update_manifest(manifest, manifest_path, number, error.status, detail=str(error))

    rows: dict[str, dict[str, str]] = {}
    if http_optimization and discovered:
        try:
            rows = direct_table_rows(page, [number for _, number, _ in discovered])
        except AkcFormError as error:
            log_step(f"Direct batch table request failed; using browser metadata fallback: {error}", 1)
            page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")

    for canonical_name, number, candidate in discovered:
        try:
            if http_optimization and number in rows:
                try:
                    details = direct_dog_details(page, number, rows[number])
                except AkcFormError as error:
                    log_step(f"Direct metadata request failed; using browser fallback: {error}", 1)
                    page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
                    details = dog_details(page, canonical_name, number)
            else:
                if http_optimization:
                    page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
                details = dog_details(page, canonical_name, number)
            myakc_breed = str(details.pop("_myakc_breed", ""))
            if not same_breed(breed, myakc_breed):
                update_manifest(manifest, manifest_path, number, "breed_mismatch", returned_name=canonical_name, returned_breed=myakc_breed)
                continue
            update_manifest(manifest, manifest_path, number, "added_to_batch", returned_name=canonical_name, returned_breed=myakc_breed)
            added.append((canonical_name, number, candidate, details))
        except PlaywrightTimeoutError as error:
            update_manifest(manifest, manifest_path, number, "metadata_timeout", detail=str(error))
            log_step(f"MyAKC metadata view timed out: {error}", 1)

    needs_pdf = [dog for dog in added if not reusable_pdf(downloads_dir, dog[1], mapped_pdf_ids(records), pdf_number_index)]
    chooser = None
    chooser_data: dict[str, Any] | None = None
    if needs_pdf:
        try:
            log_step(f"Opening one entry-form chooser for {len(added)} batch dogs", 1)
            if http_optimization and all(number in rows for _, number, _, _ in added):
                chooser_data = direct_batch_chooser(page, rows, [number for _, number, _, _ in added])
            else:
                chooser = open_batch_entry_form(page, [(name, number, candidate) for name, number, candidate, _ in added])
        except AkcFormError as error:
            if http_optimization:
                log_step(f"Direct batch chooser failed; using browser fallback: {error}", 1)
                try:
                    page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
                    chooser = open_batch_entry_form(page, [(name, number, candidate) for name, number, candidate, _ in added])
                except AkcFormError as fallback_error:
                    error = fallback_error
            if chooser is None and chooser_data is None:
                for _, number, _, _ in needs_pdf:
                    update_manifest(manifest, manifest_path, number, "form_timeout", detail=str(error))
                log_step(f"Batch form chooser failed: {error}", 1)

    for canonical_name, number, candidate, details in added:
        log_step(f"Generating PDF for batch AKC number {number}", 1)
        provenance = dobequest_provenance(candidate)
        try:
            pdf = reusable_pdf(downloads_dir, number, mapped_pdf_ids(records), pdf_number_index)
            if not pdf:
                if chooser_data is not None:
                    try:
                        pdf = download_batch_entry_form_http_data(page, chooser_data, number, downloads_dir)
                    except AkcFormError as error:
                        log_step(f"Direct PDF request failed; using browser fallback: {error}", 2)
                        if chooser is None:
                            page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
                            chooser = open_batch_entry_form(page, [(name, reg, source) for name, reg, source, _ in added])
                        pdf = download_batch_entry_form(page, chooser, number, downloads_dir)
                elif chooser is not None:
                    pdf = download_batch_entry_form(page, chooser, number, downloads_dir)
                else:
                    continue
                pdf_number_index.setdefault(number, []).append(pdf)
            record = save_bulk_record(records, output, pdf, details, canonical_name, number, provenance, journal)
            saved_count += 1
            update_manifest(manifest, manifest_path, number, "saved", record_id=record["id"], pdf_id=pdf.stem)
            log_step(f"Saved record as {record['id']}", 1)
        except AkcFormError as error:
            update_manifest(manifest, manifest_path, number, "form_timeout", detail=str(error))
            log_step(f"Form generation failed: {error}", 1)
    if chooser is not None and not chooser.is_closed():
        chooser.close()
    log_step("Clearing all Dogs of Interest from the dedicated bulk account", 1)
    if http_optimization and rows and all(number in rows for _, number, _, _ in added):
        try:
            direct_batch_cleanup(page, rows)
        except AkcFormError as error:
            log_step(f"Direct batch cleanup failed; using browser fallback: {error}", 1)
            page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
            clear_all_dogs_of_interest(page)
    else:
        clear_all_dogs_of_interest(page)
    return saved_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--numbers", type=Path, help="Optional newline-delimited AKC registration numbers.")
    parser.add_argument("--dobequest-data", type=Path, help="Optional DobeQuest JSON used only as a number-candidate source.")
    parser.add_argument("--output", type=Path, default=AKC_SCRAPER_DIR / "dogs.json")
    parser.add_argument("--downloads-dir", type=Path, default=AKC_SCRAPER_DIR / "downloads")
    parser.add_argument("--manifest", type=Path, help="Manifest path (default: output path with .bulk-manifest.json suffix).")
    parser.add_argument("--journal", type=Path, help="Append-only dataset journal path (default: output with .jsonl suffix).")
    parser.add_argument("--checkpoint-every", type=int, default=100, help="Compact journal into --output after this many saved dogs.")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--breed", default="Doberman Pinschers")
    parser.add_argument("--login-timeout", type=int, default=300)
    parser.add_argument("--auth-state", type=Path, default=AKC_SCRAPER_DIR / ".akc-auth-state.json")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--http-optimization", action="store_true", help="Use direct authenticated HTTP for lookup, metadata, and PDF reports with browser fallback.")
    parser.add_argument("--record-har", type=Path, help="Record all browser-context page and popup traffic to this HAR file.")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    if args.batch_size < 1 or args.checkpoint_every < 1:
        parser.error("--batch-size and --checkpoint-every must be at least 1")
    if args.manifest is None:
        args.manifest = args.output.with_suffix(".bulk-manifest.json")
    if not args.numbers and not args.dobequest_data:
        parser.error("provide --numbers, --dobequest-data, or both")

    username = os.environ.get("AKC_USERNAME", "")
    password = os.environ.get("AKC_PASSWORD", "")
    if not username or not password:
        parser.error("set AKC_USERNAME and AKC_PASSWORD environment variables")

    candidates = candidate_numbers(args.numbers, args.dobequest_data)
    journal = DatasetJournal(args.output, args.journal)
    records = load_records(args.output, journal)
    args.downloads_dir.mkdir(parents=True, exist_ok=True)
    pdf_number_index = build_pdf_number_index(args.downloads_dir)
    manifest = load_json(args.manifest, {"schema_version": 1, "numbers": {}})
    existing = known_numbers(records, args.downloads_dir)
    failed = {"not_found", "number_mismatch", "breed_mismatch", "form_timeout", "metadata_timeout", "popup_timeout"}
    pending: list[tuple[str, dict[str, Any]]] = []
    already_collected = 0
    for number, candidate in sorted(candidates.items(), reverse=True):
        if number in existing:
            already_collected += 1
            continue
        entry = manifest.get("numbers", {}).get(number, {})
        status = entry.get("status", "pending")
        if status not in {"pending", "processing", "added_to_batch"} and not (args.retry_failed and status in failed):
            continue
        pending.append((number, candidate))
    save_json(args.manifest, manifest)
    print(f"Bulk candidates: {len(candidates)}; already collected: {already_collected}; pending: {len(pending)}; batch size: {args.batch_size}", flush=True)

    interrupted = False
    stop_requested = {"value": False}
    previous_sigint = signal.getsignal(signal.SIGINT)

    def request_stop(signum: int, frame: Any) -> None:
        if stop_requested["value"]:
            raise KeyboardInterrupt
        stop_requested["value"] = True
        print("Interrupt received; stopping after the current browser operation.", flush=True)

    signal.signal(signal.SIGINT, request_stop)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        context_options: dict[str, Any] = {"accept_downloads": True}
        if args.record_har:
            args.record_har.parent.mkdir(parents=True, exist_ok=True)
            context_options.update({"record_har_path": str(args.record_har), "record_har_content": "embed", "record_har_mode": "full"})
        if args.auth_state.exists():
            context_options["storage_state"] = str(args.auth_state)
        context = browser.new_context(**context_options)
        page = context.new_page()
        try:
            login(page, username, password, args.login_timeout)
            context.storage_state(path=str(args.auth_state))
            page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
            saved_since_checkpoint = 0
            for offset in range(0, len(pending), args.batch_size):
                if stop_requested["value"]:
                    interrupted = True
                    break
                saved_since_checkpoint += process_batch(page, pending[offset : offset + args.batch_size], records, args.output, args.downloads_dir, manifest, args.manifest, args.breed, pdf_number_index, journal, args.http_optimization)
                if saved_since_checkpoint >= args.checkpoint_every:
                    refresh_relationship_schema(records)
                    journal.compact(lambda: save_records(records, args.output))
                    saved_since_checkpoint = 0
                    log_step("Compacted dataset journal into dogs.json", 0)
        except PlaywrightError as error:
            if not stop_requested["value"] and "ERR_ABORTED" not in str(error):
                raise
            interrupted = True
            print("Browser navigation aborted during requested shutdown.", flush=True)
        except KeyboardInterrupt:
            interrupted = True
            print("Forced interrupt received; preserving journal without compaction.", flush=True)
        finally:
            if interrupted or stop_requested["value"]:
                interrupted = True
                print("Bulk journal is durable and will be replayed on the next run.", flush=True)
                try:
                    clear_all_dogs_of_interest(page)
                except Exception as error:
                    print(f"Bulk cleanup after interrupt failed: {error}", flush=True)
            elif journal.journal_path.exists() and journal.journal_path.stat().st_size:
                refresh_relationship_schema(records)
                journal.compact(lambda: save_records(records, args.output))
                log_step("Compacted final dataset journal into dogs.json", 0)
            try:
                context.close()
            except Exception as error:
                if not interrupted:
                    raise
                print(f"Context close after interrupt skipped: {error}", flush=True)
            try:
                browser.close()
            except Exception as error:
                if not interrupted:
                    raise
                print(f"Browser close after interrupt skipped: {error}", flush=True)
    signal.signal(signal.SIGINT, previous_sigint)
    return 130 if interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())