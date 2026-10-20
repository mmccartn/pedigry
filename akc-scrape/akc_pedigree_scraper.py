#!/usr/bin/env python3
"""Collect AKC dog details and parent relationships through MyAKC
entry forms."""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
import os
import re
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

from akc_titles import PREFIX_TITLES, SUFFIX_TITLES
from dataset_journal import DatasetJournal
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from pypdf import PdfReader
from playwright.sync_api import (
    Browser,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

LOGIN_URL = "https://www.apps.akc.org/apps/online_accounts/index.cfm"
MANAGE_DOGS_URL = (
    "https://www.apps.akc.org/apps/online_accounts/dog_management/"
    "index.cfm?get_template=dog_management_form"
)
ENTRY_FORM_VALUE = "AHN999_1003"
CONTACT_FIELDS = ("displayemail", "displayphone", "displayaddress")
POPUP_EVENT_TIMEOUT = 2_000
DOBEQUEST_TOKEN_EQUIVALENTS = {"vom": "v", "von": "v", "v": "v"}
ROMAN_NUMERAL_PATTERN = re.compile(r"^M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")
DOBEQUEST_IGNORED_TOKENS = {"am", "arg", "bda", "bfl", "brz", "can", "col", "ghc", "grch", "intl", "lc", "mex", "ukc", "urg", "ven", "world"}


class AkcLookupError(RuntimeError):
    """Represent an AKC name lookup that cannot be safely resolved."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


class AkcFormError(RuntimeError):
    """Represent a temporary MyAKC entry-form generation failure."""


def log_dog(name: str, level: int) -> None:
    """Print a readable header for processing one pedigree node."""
    print(f"\n{name}\n{'-' * 72}\nGeneration {level}", flush=True)


def log_step(message: str, depth: int = 0) -> None:
    """Print an indented step in the current dog processing outline."""
    print(f"{'  ' * depth}+- {message}", flush=True)


PDF_FIELDS = {
    "akc_number": ("text_akc_reg_num",),
    "name": ("text_dog_name_full",),
    "breed": ("desc_full_text_breed",),
    "sex": ("desc_sex",),
    "birth_date": ("date_whelped",),
    "place_of_birth": ("pob_foreign", "desc_birth_country"),
    "breeder": ("text_breeder_name_full",),
    "sire": ("text_sire_name_full",),
    "dam": ("text_dam_name_full",),
}


def value(field: Any) -> str:
    """Return a normalized value from a PDF AcroForm field."""
    result = field.get("/V", "") if field else ""
    return str(result).strip("/ ")


def extract_pdf(pdf_path: Path) -> dict[str, Any]:
    """Extract configured pedigree fields from an AKC entry-form PDF."""
    fields = PdfReader(pdf_path).get_fields() or {}
    record = {
        key: next(
            (
                value(fields.get(field_name))
                for field_name in names
                if value(fields.get(field_name))
            ),
            "",
        )
        for key, names in PDF_FIELDS.items()
    }
    record["sex"] = {"M": "Male", "F": "Female"}.get(record["sex"].upper(), record["sex"])
    registration_fields = {
        "AKC": "reg_akc",
        "Litter": "reg_lit",
        "PAL/ILP": "reg_ilp",
        "FSS": "reg_fss",
        "Foreign": "reg_for",
    }
    record["registration_type"] = next(
        (
            name
            for name, field_name in registration_fields.items()
            if value(fields.get(field_name))
        ),
        "",
    )
    return record


def aliases_for(*names: str) -> list[str]:
    """Return unique nonempty name variants in their original order."""
    aliases: list[str] = []
    for name in names:
        if name and name.casefold() not in {alias.casefold() for alias in aliases}:
            aliases.append(name)
    return aliases


def unresolved_record(name: str, breed: str, status: str) -> dict[str, Any]:
    """Create a checkpoint record for a dog unavailable through AKC."""
    return {
        "akc_number": "",
        "akc_lookup_status": status,
        "aliases": [name],
        "birth_date": "",
        "breed": breed,
        "breed_variety": "",
        "breeder": "",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "color": "",
        "dam": "",
        "dam_akc_number": "",
        "dam_lookup_status": "not_collected",
        "markings": "",
        "medical_screens": {},
        "name": name,
        "ofa_number": "",
        "pdf_id": "",
        "place_of_birth": "",
        "registration_type": "",
        "sex": "",
        "sire": "",
        "sire_akc_number": "",
        "sire_lookup_status": "not_collected",
    }


def enrich_relationships(records: dict[str, dict[str, Any]]) -> None:
    """Link parent names to AKC numbers and set lookup statuses."""
    aliases: dict[str, str] = {}
    for record in records.values():
        record["aliases"] = aliases_for(*record.get("aliases", []), str(record.get("name", "")))
        for alias in record["aliases"]:
            for candidate in name_candidates(alias):
                aliases.setdefault(candidate.casefold(), str(record.get("akc_number", "")))
    for record in records.values():
        for parent in ("sire", "dam"):
            if record.get(f"{parent}_lookup_status") == "manual_override":
                continue
            parent_name = str(record.get(parent, ""))
            parent_number = (
                next(
                    (
                        aliases[candidate.casefold()]
                        for candidate in name_candidates(parent_name)
                        if aliases.get(candidate.casefold())
                    ),
                    "",
                )
                if parent_name
                else ""
            )
            record[f"{parent}_akc_number"] = parent_number
            record[f"{parent}_lookup_status"] = (
                "resolved" if parent_number else "unresolved" if parent_name else "missing"
            )


def mapped_pdf_ids(records: dict[str, dict[str, Any]]) -> set[str]:
    """Return PDF IDs already assigned to saved dog records."""
    return {str(record["pdf_id"]) for record in records.values() if record.get("pdf_id")}


PDF_INDEX_CACHE_NAME = ".akc-pdf-index.json"


def build_pdf_metadata_index(downloads_dir: Path, report: bool = True) -> dict[str, dict[str, Any]]:
    """Cache PDF identities and refresh only files whose metadata changed."""
    cache_path = downloads_dir / PDF_INDEX_CACHE_NAME
    cached = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    paths = list(downloads_dir.glob("*.pdf"))
    pending: list[tuple[Path, Any]] = []
    entries: dict[str, dict[str, Any]] = {}
    for pdf_path in paths:
        stat = pdf_path.stat()
        prior = cached.get(pdf_path.name, {})
        if prior.get("size") == stat.st_size and prior.get("mtime_ns") == stat.st_mtime_ns:
            entries[pdf_path.name] = prior
        else:
            pending.append((pdf_path, stat))
    if report:
        print(
            f"PDF index: {len(paths)} files; {len(entries)} cached; "
            f"{len(pending)} new or changed.",
            flush=True,
        )
    for position, (pdf_path, stat) in enumerate(pending, 1):
        try:
            record = extract_pdf(pdf_path)
            entries[pdf_path.name] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "akc_number": str(record.get("akc_number") or ""),
                "name": str(record.get("name") or ""),
            }
        except Exception:
            entries[pdf_path.name] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "akc_number": "", "name": ""}
        if report and (position % 100 == 0 or position == len(pending)):
            print(f"PDF index: extracted metadata for {position}/{len(pending)} changed files.", flush=True)
    if pending or set(cached) != set(entries):
        cache_path.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return entries


def build_pdf_number_index(downloads_dir: Path) -> dict[str, list[Path]]:
    """Index local PDFs by AKC number using the persistent metadata cache."""
    index: dict[str, list[Path]] = {}
    for filename, entry in build_pdf_metadata_index(downloads_dir).items():
        akc_number = str(entry.get("akc_number") or "")
        if akc_number:
            index.setdefault(akc_number, []).append(downloads_dir / filename)
    return index


def reusable_pdf(
    downloads_dir: Path,
    akc_number: str,
    used_pdf_ids: set[str],
    pdf_number_index: dict[str, list[Path]] | None = None,
) -> Path | None:
    """Find an unmapped PDF matching the selected dog's AKC number."""
    candidates = (pdf_number_index or build_pdf_number_index(downloads_dir)).get(akc_number, [])
    return next((pdf_path for pdf_path in candidates if pdf_path.stem not in used_pdf_ids), None)


def build_pdf_name_index(downloads_dir: Path) -> dict[str, dict[str, Path]]:
    """Index local PDFs by normalized dog name and unique AKC number."""
    index: dict[str, dict[str, Path]] = {}
    for filename, entry in build_pdf_metadata_index(downloads_dir, report=False).items():
        name = registered_name(str(entry.get("name") or "")).casefold()
        akc_number = str(entry.get("akc_number") or "")
        if name and akc_number:
            index.setdefault(name, {}).setdefault(akc_number, downloads_dir / filename)
    return index


def local_pdf_match(index: dict[str, dict[str, Path]], name: str) -> Path | None:
    """Return a PDF only when one AKC number exactly matches the name."""
    matches = index.get(registered_name(name).casefold(), {})
    return next(iter(matches.values())) if len(matches) == 1 else None


def click_first(page: Page, selectors: list[str], description: str) -> None:
    """Click the first matching selector or raise an error."""
    last_error: Exception | None = None
    for selector in selectors:
        try:
            page.locator(selector).first.click(timeout=4_000)
            return
        except PlaywrightTimeoutError as error:
            last_error = error
    raise RuntimeError(
        f"Could not find {description}. The MyAKC page layout may have changed."
    ) from last_error


def fill_first(page: Page, selectors: list[str], text: str, description: str) -> None:
    """Fill the first matching selector or raise a descriptive error."""
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.fill(text, timeout=4_000)
            return
        except PlaywrightTimeoutError:
            continue
    raise RuntimeError(f"Could not find {description}. The MyAKC page layout may have changed.")


def login(page: Page, username: str, password: str, timeout_seconds: int) -> None:
    """Reuse or establish a MyAKC session, allowing time for MFA."""
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    user_menu = page.locator("#akc-app-logged-in-user-menu_user-name")
    try:
        user_menu.wait_for(timeout=5_000)
        return
    except PlaywrightTimeoutError:
        pass
    sign_in = page.locator("#akc-app-nav__sign-in-btn")
    if sign_in.is_visible():
        sign_in.click()
    login_screen = page.locator("#gigya-login-screen")
    login_screen.locator("input[name='username'][data-screenset-roles='instance']").fill(
        username, timeout=20_000
    )
    login_screen.locator("input[name='password'][data-screenset-roles='instance']").fill(password)
    login_screen.locator(
        "input.gigya-input-submit[value='Log In'][data-screenset-roles='instance']"
    ).click()
    user_menu = page.locator("#akc-app-logged-in-user-menu_user-name")
    try:
        user_menu.wait_for(timeout=timeout_seconds * 1_000)
    except PlaywrightTimeoutError as error:
        messages = [
            message.strip()
            for message in login_screen.locator(".gigya-error-msg-active").all_inner_texts()
            if message.strip()
        ]
        detail = f" Gigya reported: {'; '.join(messages)}" if messages else ""
        raise RuntimeError(
            f"MyAKC did not complete its SSO session setup within "
            f"{timeout_seconds} seconds.{detail}"
        ) from error


def title_map() -> tuple[frozenset[str], frozenset[str]]:
    """Return the static AKC prefix and suffix title abbreviations."""
    return PREFIX_TITLES, SUFFIX_TITLES


def registered_name(name: str) -> str:
    """Remove recognized AKC titles from both ends of a name."""
    prefix, suffix = title_map()
    words = name.split()
    while len(words) > 2 and words[0].casefold() in prefix:
        words.pop(0)
    while len(words) > 2 and words[-1].casefold() in suffix:
        words.pop()
    return " ".join(words)


def name_candidates(name: str) -> list[str]:
    """Return title-free and terminal-Roman-numeral-free name candidates."""
    normalized = registered_name(name)
    candidates = [name]
    if normalized.casefold() != name.casefold():
        candidates.append(normalized)
    words = normalized.split()
    if len(words) > 1 and is_roman_numeral(words[-1]):
        candidates.append(" ".join(words[:-1]))
    trailing_letters = words.copy()
    while len(trailing_letters) > 1 and len(trailing_letters[-1]) == 1 and trailing_letters[-1].isupper():
        trailing_letters.pop()
        candidates.append(" ".join(trailing_letters))
    return list(dict.fromkeys(candidates))


def has_terminal_single_letter(name: str) -> bool:
    """Return whether a title-normalized name ends in a single uppercase letter."""
    words = registered_name(name).split()
    return len(words) > 1 and len(words[-1]) == 1 and words[-1].isupper()


def is_roman_numeral(token: str) -> bool:
    """Return whether a token is a valid uppercase standard Roman numeral."""
    return bool(token) and token == token.upper() and bool(ROMAN_NUMERAL_PATTERN.fullmatch(token))


def has_terminal_roman_numeral(name: str) -> bool:
    """Return whether a title-normalized name ends in an uppercase Roman numeral."""
    words = registered_name(name).split()
    return len(words) > 1 and is_roman_numeral(words[-1])


def dobequest_tokens(name: str) -> list[str]:
    """Normalize a messy DobeQuest name into matching tokens."""
    prefix, suffix = title_map()
    tokens = re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", name.casefold())
    normalized = []
    for token in tokens:
        if token in prefix | suffix | DOBEQUEST_IGNORED_TOKENS or re.search(r"\d", token):
            continue
        token = DOBEQUEST_TOKEN_EQUIVALENTS.get(token, token)
        normalized.append(token.removesuffix("'s"))
    return normalized


def ordered_contains(tokens: list[str], candidate: list[str]) -> bool:
    """Return whether candidate tokens occur in order within tokens."""
    position = 0
    for token in candidate:
        try:
            position = tokens.index(token, position) + 1
        except ValueError:
            return False
    return True


def load_dobequest_index(path: Path | None) -> dict[str, Any]:
    """Build compact exact and token indexes from DobeQuest data."""
    if not path or not path.exists():
        return {"by_id": {}, "exact": {}, "profiles": []}
    with path.open(encoding="utf-8") as source:
        raw_profiles = json.load(source)
    by_id: dict[str, dict[str, Any]] = {}
    exact: dict[str, list[dict[str, Any]]] = {}
    profiles: list[dict[str, Any]] = []
    for did, profile in raw_profiles.items():
        full_name = " ".join(
            part
            for part in (
                profile.get("titles"),
                profile.get("registeredName"),
                profile.get("suffix"),
            )
            if part
        )
        profile_data = {
            "did": str(profile.get("did") or did),
            "akc_number": str(profile.get("akcNum") or "").strip(),
            "name": str(profile.get("registeredName") or "").strip(),
            "sire": profile.get("sire") or {},
            "dam": profile.get("dam") or {},
            "tokens": dobequest_tokens(full_name),
        }
        profiles.append(profile_data)
        by_id[profile_data["did"]] = profile_data
        names = {
            registered_name(full_name).casefold(),
            registered_name(str(profile.get("registeredName") or "")).casefold(),
            " ".join(dobequest_tokens(full_name)),
            " ".join(dobequest_tokens(str(profile.get("registeredName") or ""))),
        }
        for key in names - {""}:
            exact.setdefault(key, []).append(profile_data)
    return {"by_id": by_id, "exact": exact, "profiles": profiles}


def load_dobequest_overrides(path: Path | None) -> dict[str, dict[str, Any]]:
    """Load verified DobeQuest name-to-profile overrides from JSON."""
    if not path or not path.exists():
        return {}
    with path.open(encoding="utf-8") as source:
        overrides = json.load(source)
    return {registered_name(name).casefold(): value for name, value in overrides.items()}


def load_akc_number_overrides(path: Path | None) -> dict[str, dict[str, Any]]:
    """Load verified name-to-AKC-number overrides from JSON."""
    if not path or not path.exists():
        return {}
    with path.open(encoding="utf-8") as source:
        overrides = json.load(source)
    return {registered_name(name).casefold(): value for name, value in overrides.items()}


def load_pedigree_overrides(path: Path | None) -> dict[str, dict[str, Any]]:
    """Load verified AKC-number-to-parent overrides from JSON."""
    if not path or not path.exists():
        return {}
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def dobequest_match(
    index: dict[str, Any], name: str, overrides: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """Return a manual, exact, or unique containment DobeQuest match."""
    key = registered_name(name).casefold()
    override = (overrides or {}).get(key)
    if override:
        profile = index["by_id"].get(str(override["dobequest_id"]))
        if profile:
            return {
                **profile,
                "akc_number": str(override.get("akc_number", profile["akc_number"])),
                "match_status": "manual_override",
            }
    tokens = dobequest_tokens(name)
    exact = index["exact"].get(" ".join(tokens), [])
    if len(exact) == 1:
        return {**exact[0], "match_status": "token_equivalence"}
    exact = index["exact"].get(registered_name(name).casefold(), [])
    if len(exact) == 1:
        return {**exact[0], "match_status": "exact_normalized_name"}
    if len(tokens) < 3:
        return None
    matches = [
        profile for profile in index["profiles"] if ordered_contains(profile["tokens"], tokens)
    ]
    return (
        {**matches[0], "match_status": "unique_token_containment"} if len(matches) == 1 else None
    )


def pdf_identity_matches(requested_name: str, pdf_name: str, dobequest: dict[str, Any] | None) -> bool:
    """Validate a PDF name using AKC or DobeQuest-normalized identity."""
    if registered_name(requested_name).casefold() == registered_name(pdf_name).casefold():
        return True
    return bool(dobequest) and dobequest_tokens(requested_name) == dobequest_tokens(pdf_name)


def same_breed(expected: str, actual: str) -> bool:
    """Compare breed labels while ignoring case and a terminal plural s."""
    normalize = lambda value: re.sub(r"[^a-z0-9]+", "", value.casefold()).removesuffix("s")
    return bool(expected and actual) and normalize(expected) == normalize(actual)


def registration_number(row: Any) -> str:
    """Extract the AKC number encoded in a managed-list row."""
    onclick = row.locator("input[type='checkbox']").first.get_attribute("onclick") or ""
    match = re.search(r"dog_reg_number=([^&']+)", onclick)
    return match.group(1) if match else ""


def managed_row(page: Page, name: str, akc_number: str) -> Any:
    """Locate a managed row by AKC number or display name."""
    if akc_number:
        return page.locator(
            "tr",
            has=page.locator(f"input[type='checkbox'][onclick*='dog_reg_number={akc_number}']"),
        ).first
    return page.locator("tr", has=page.locator("a.gridlink", has_text=name)).first


def select_managed_row(page: Page, row: Any) -> None:
    """Clear stale managed-list selections and select exactly one row."""
    checkboxes = page.locator("form#dog_management input[type='checkbox']")
    for checkbox in checkboxes.all():
        if checkbox.is_checked():
            checkbox.uncheck()
    row.locator("input[type='checkbox']").check(timeout=8_000)
    if page.locator("form#dog_management input[type='checkbox']:checked").count() != 1:
        raise RuntimeError("MyAKC did not retain exactly one selected dog row.")


def choose_multi_result(popup: Page, candidate: str, expected_name: str) -> None:
    """Select the exact expected dog from an AKC multiple-result page."""
    matching_option = None
    for option in popup.locator("input[name='values_selected']").all():
        _, _, result_name = (option.get_attribute("value") or "").partition("|")
        if registered_name(result_name).casefold() == registered_name(expected_name).casefold():
            matching_option = option
            break
    if matching_option is None:
        results = [
            value.partition("|")[2].strip()
            for value in popup.locator("input[name='values_selected']").evaluate_all(
                "options => options.map(option => option.value)"
            )
        ]
        raise AkcLookupError(
            "ambiguous",
            f"AKC returned multiple dogs for {candidate!r}, but none matched "
            f"{expected_name!r}. Results: {', '.join(results)}",
        )
    matching_option.check()
    with popup.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
        popup.locator("#button").click()


def choose_dog(page: Page, name: str, breed: str) -> tuple[str, str]:
    """Add or reuse a dog and return its list name and AKC number."""
    page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
    candidates = name_candidates(name)
    if has_terminal_roman_numeral(name) or has_terminal_single_letter(name):
        shortest_candidate = min(candidates, key=lambda candidate: len(candidate.split()))
        candidates = [shortest_candidate, *[candidate for candidate in candidates if candidate != shortest_candidate]]
    candidate_names = {candidate.casefold() for candidate in candidates}
    for link in page.locator("a.gridlink").all():
        existing_name = link.inner_text().strip()
        if existing_name.casefold() in candidate_names:
            return existing_name, registration_number(link.locator("xpath=ancestor::tr"))
    try:
        with page.expect_popup(timeout=POPUP_EVENT_TIMEOUT) as popup_info:
            page.locator("a.purple", has_text="Add A Dog").click()
        popup = popup_info.value
        popup.wait_for_load_state("domcontentloaded")
    except PlaywrightTimeoutError:
        popup = wait_for_context_page(page, "index_popup_dog.cfm", 30_000)
        if not popup:
            raise RuntimeError("MyAKC did not open the Add Dog popup.")
    canonical_name = ""
    akc_number = ""
    too_many_candidates: list[str] = []
    for candidate in candidates:
        log_step(f"Trying AKC name candidate: {candidate}", 1)
        popup.locator("input[name='get_dog_by'][value='O']").check()
        popup.locator("#dog_name").fill(candidate)
        popup.locator("select[name='bvgcode']").select_option(label=breed)
        try:
            with popup.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
                popup.locator("#button").click(no_wait_after=True)
        except PlaywrightTimeoutError:
            log_step("MyAKC name lookup navigation did not finish; inspecting response state", 2)
            popup.wait_for_timeout(1_000)
        body = popup.locator("body").inner_text()
        if popup.locator("#dog_name").count() and not re.search(r"too many dogs were found|could not be added.*could not be found|no results were found|not found|no dog", body, re.I | re.S):
            raise AkcLookupError("lookup_timeout", f"MyAKC did not return a lookup result for {candidate!r}.")
        if re.search(r"too many dogs were found", body, re.I):
            too_many_candidates.append(candidate)
            continue
        if re.search(r"could not be added.*could not be found", body, re.I | re.S):
            continue
        if re.search(r"no results were found|not found|no dog", body, re.I):
            continue
        expected_name = name if has_terminal_roman_numeral(name) or has_terminal_single_letter(name) else candidate
        if popup.locator("input[name='values_selected']").count():
            choose_multi_result(popup, candidate, expected_name)
        canonical_name = popup.locator("form table strong").first.inner_text().strip()
        akc_number = popup.locator("#dog_reg_num").input_value().strip()
        if (
            canonical_name
            and registered_name(canonical_name).casefold() != registered_name(expected_name).casefold()
        ):
            log_step(f"AKC returned nonmatching result {canonical_name!r}; trying next candidate", 2)
            try:
                popup.go_back(wait_until="domcontentloaded", timeout=30_000)
            except PlaywrightError:
                popup.locator("#Back").click()
            canonical_name = ""
            akc_number = ""
            continue
        if canonical_name and akc_number:
            break
    if not canonical_name or not akc_number:
        if len(too_many_candidates) == len(candidates):
            raise AkcLookupError(
                "too_many_matches",
                f"AKC found too many dogs for every candidate: {', '.join(too_many_candidates)}.",
            )
        raise AkcLookupError(
            "not_found",
            f"AKC did not find a {breed} named {name!r} after removing recognized titles.",
        )
    popup.locator("input[name='dog_dio_int'][value='DOI']").check()
    with popup.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
        popup.locator("#Submit").click()
    try:
        with popup.expect_event("close", timeout=10_000):
            popup.locator("input[name='Continue'][value='Continue']").click(no_wait_after=True)
    except PlaywrightError as error:
        if "Target page, context or browser has been closed" not in str(error):
            raise
    page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
    return canonical_name, akc_number


def direct_name_lookup_add(page: Page, name: str, breed: str) -> tuple[str, str]:
    """Resolve and add a dog through captured MyAKC name-lookup POSTs."""
    breed_codes = {"Doberman Pinschers": "611"}
    breed_code = breed_codes.get(breed)
    if not breed_code:
        raise AkcLookupError("http_fallback", f"No direct MyAKC breed code is configured for {breed!r}.")
    candidates = name_candidates(name)
    if has_terminal_roman_numeral(name) or has_terminal_single_letter(name):
        shortest_candidate = min(candidates, key=lambda candidate: len(candidate.split()))
        candidates = [shortest_candidate, *[candidate for candidate in candidates if candidate != shortest_candidate]]
    lookup_url = "https://www.apps.akc.org/apps/online_accounts/dog_management/index_popup_dog.cfm?action=add_dog"
    too_many_candidates: list[str] = []
    canonical_name = ""
    akc_number = ""
    for candidate in candidates:
        response = page.context.request.post(
            lookup_url,
            form={"dog_reg_num": "", "litter_num": "", "get_dog_by": "O", "dog_name": candidate, "bvgcode": breed_code},
        )
        if not response.ok:
            raise AkcLookupError("http_fallback", f"Direct MyAKC lookup returned HTTP {response.status}.")
        body = response.text()
        if re.search(r"too many dogs were found", body, re.I):
            too_many_candidates.append(candidate)
            continue
        if re.search(r"no results were found|could not be found|not found", body, re.I):
            continue
        expected_name = name if has_terminal_roman_numeral(name) or has_terminal_single_letter(name) else candidate
        selected_body = body
        values = re.findall(r"<input[^>]*name=['\"]values_selected['\"][^>]*value=['\"]([^'\"]+)", body, re.I)
        if values:
            selected = next(
                (value for value in values if registered_name(unescape(value.partition("|")[2]).strip()).casefold() == registered_name(expected_name).casefold()),
                None,
            )
            if not selected:
                continue
            selected_response = page.context.request.post(
                "https://www.apps.akc.org/apps/online_accounts/dog_management/index_popup_dog.cfm?action=add_dog_other",
                form={"values_selected": unescape(selected)},
            )
            if not selected_response.ok:
                raise AkcLookupError("http_fallback", f"Direct MyAKC result selection returned HTTP {selected_response.status}.")
            selected_body = selected_response.text()
        number_match = re.search(r"name=['\"]dog_reg_num['\"][^>]*value=['\"]([^'\"]+)", selected_body, re.I)
        name_match = re.search(r"<strong[^>]*>\s*(.*?)\s*</strong>", selected_body, re.I | re.S)
        if not number_match or not name_match:
            raise AkcLookupError("http_fallback", "Direct MyAKC lookup returned unexpected HTML.")
        akc_number = unescape(number_match.group(1)).strip()
        canonical_name = re.sub(r"<[^>]+>", "", unescape(name_match.group(1))).strip()
        if registered_name(canonical_name).casefold() != registered_name(expected_name).casefold():
            canonical_name = ""
            akc_number = ""
            continue
        break
    if not canonical_name or not akc_number:
        status = "too_many_matches" if len(too_many_candidates) == len(candidates) else "not_found"
        raise AkcLookupError(status, f"Direct MyAKC did not resolve {name!r}.")
    add_response = page.context.request.post(
        "https://www.apps.akc.org/apps/online_accounts/dog_management/index_popup_dog.cfm?action=add_dog_io",
        form={"dog_dio_int": "DOI", "dog_active": "NONE", "dog_reg_num": akc_number, "Submit": "Next >"},
    )
    if not add_response.ok:
        raise AkcLookupError("http_fallback", f"Direct MyAKC Add Dog request returned HTTP {add_response.status}.")
    page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
    return canonical_name, akc_number


def choose_dog_by_number(page: Page, akc_number: str) -> tuple[str, str]:
    """Add or reuse a dog by a verified AKC registration number."""
    page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
    row = managed_row(page, "", akc_number)
    if row.count():
        return row.locator("a.gridlink").first.inner_text().strip(), akc_number
    popup: Page | None = None
    for attempt in range(2):
        try:
            with page.expect_popup(timeout=POPUP_EVENT_TIMEOUT) as popup_info:
                page.locator("a.purple", has_text="Add A Dog").click()
            popup = popup_info.value
            popup.wait_for_load_state("domcontentloaded")
            break
        except PlaywrightTimeoutError:
            popup = wait_for_context_page(page, "index_popup_dog.cfm", 10_000)
            if popup:
                break
            if attempt == 0:
                log_step("Add Dog popup did not open; refreshing MyAKC and retrying", 2)
                page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
    if not popup:
        raise AkcLookupError("popup_timeout", "MyAKC did not open the Add Dog popup.")
    log_step(f"Trying AKC number lookup: {akc_number}", 2)
    popup.locator("input[name='get_dog_by'][value='D']").check()
    popup.locator("#dog_reg_num").fill(akc_number)
    with popup.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
        popup.locator("#button").click()
    if not popup.locator("input[name='dog_dio_int'][value='DOI']").count():
        raise AkcLookupError("number_mismatch", f"AKC could not resolve number {akc_number!r}.")
    canonical_name = popup.locator("form table strong").first.inner_text().strip()
    result_number = popup.locator("#dog_reg_num").input_value().strip()
    if result_number != akc_number:
        raise AkcLookupError(
            "number_mismatch", f"AKC returned {result_number!r} for {akc_number!r}."
        )
    popup.locator("input[name='dog_dio_int'][value='DOI']").check()
    with popup.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
        popup.locator("#Submit").click()
    try:
        with popup.expect_event("close", timeout=10_000):
            popup.locator("input[name='Continue'][value='Continue']").click(no_wait_after=True)
    except PlaywrightError as error:
        if "Target page, context or browser has been closed" not in str(error):
            raise
    page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
    return canonical_name, result_number


def detail_value(lines: list[str], labels: tuple[str, ...]) -> str:
    """Return the value for the first matching detail label."""
    normalized_labels = {label.casefold() for label in labels}
    for index, line in enumerate(lines):
        text = line.strip()
        label, separator, value = text.partition(":")
        if label.strip().casefold() not in normalized_labels:
            continue
        if separator and value.strip():
            return value.strip()
        for next_line in lines[index + 1 :]:
            next_text = next_line.strip()
            if not next_text:
                continue
            if ":" in next_text:
                break
            return next_text
    return ""


def medical_screens(lines: list[str]) -> dict[str, str]:
    """Collect populated entries from the Medical Screens section."""
    result: dict[str, str] = {}
    in_section = False
    for line in lines:
        text = line.strip()
        if text.casefold() == "medical screens":
            in_section = True
            continue
        if not in_section or ":" not in text:
            continue
        label, value = (part.strip() for part in text.split(":", 1))
        if label and value:
            result[label] = value
    return result


def dog_details(page: Page, name: str, akc_number: str) -> dict[str, Any]:
    """Read color and medical data from the Dog Information frame."""
    if "dog_management" not in page.url:
        page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
    for attempt in range(2):
        page.locator("#DogList").wait_for(timeout=15_000)
        details = page.frame_locator("#DogList").locator("body")
        managed_row(page, name, akc_number).locator("a.gridlink").first.click()
        try:
            details.get_by_text(akc_number, exact=False).wait_for(timeout=30_000)
            break
        except PlaywrightTimeoutError:
            if attempt:
                raise
            log_step("Dog Information iframe delayed; refreshing managed list", 2)
            page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
    lines = details.inner_text().splitlines()
    screens = medical_screens(lines)
    return {
        "_myakc_breed": detail_value(lines, ("Breed",)),
        "breed_variety": detail_value(lines, ("Variety", "Breed Variety")),
        "color": detail_value(lines, ("Color",)),
        "markings": detail_value(lines, ("Markings",)),
        "ofa_number": screens.get("OFA Number", detail_value(lines, ("OFA No.", "OFA #"))),
        "medical_screens": screens,
    }


def context_page(page: Page, url_fragment: str) -> Page | None:
    """Return the newest open page whose URL contains the fragment."""
    return next(
        (candidate for candidate in reversed(page.context.pages) if url_fragment in candidate.url),
        None,
    )


def wait_for_context_page(page: Page, url_fragment: str, timeout_ms: int) -> Page | None:
    """Wait for a known popup page to appear in the browser context."""
    deadline = time.monotonic() + timeout_ms / 1_000
    while time.monotonic() < deadline:
        popup = context_page(page, url_fragment)
        if popup:
            return popup
        page.wait_for_timeout(250)
    return context_page(page, url_fragment)


def close_stale_entry_pages(page: Page) -> None:
    """Close old chooser and report pages before a new entry-form action."""
    for candidate in page.context.pages:
        if candidate is page or candidate.is_closed():
            continue
        if "dsp_choose_pdf_form" in candidate.url or "/apps/dynamicpdfs/index.cfm" in candidate.url:
            candidate.close()


def open_entry_form(page: Page, name: str, akc_number: str) -> Page:
    """Open an entry-form chooser and verify its selected AKC number."""
    form_selector = f"input[name='form'][value='{ENTRY_FORM_VALUE}']"
    close_stale_entry_pages(page)
    last_error: Exception | None = None
    for attempt in range(3):
        form: Page | None = None
        selected = False
        try:
            row = managed_row(page, name, akc_number)
            select_managed_row(page, row)
            page.locator("select[name='action_items']").select_option("6")
            try:
                with page.expect_popup(timeout=POPUP_EVENT_TIMEOUT) as popup_info:
                    page.locator("#go_filters").click()
                form = popup_info.value
                form.wait_for_load_state("domcontentloaded")
            except PlaywrightTimeoutError:
                form = wait_for_context_page(page, "dsp_choose_pdf_form", 30_000) or page
                form.locator(form_selector).wait_for(timeout=30_000)
            chooser_number = form.locator("input[name='dog_reg_num']").input_value().strip()
            if chooser_number == akc_number:
                selected = True
                return form
            log_step(f"Entry chooser mismatch: expected {akc_number}, received {chooser_number}", 2)
        except PlaywrightTimeoutError as error:
            last_error = error
            log_step(f"Entry chooser did not load (attempt {attempt + 1}/3)", 2)
        finally:
            if not selected and form is not None and form is not page and not form.is_closed():
                form.close()
        if attempt < 2:
            log_step("Retrying entry-form selection after refreshing MyAKC", 2)
            close_stale_entry_pages(page)
            page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
    detail = f": {last_error}" if last_error else ""
    raise AkcFormError(f"MyAKC entry chooser did not select AKC number {akc_number!r}{detail}")


def download_entry_form(page: Page, name: str, akc_number: str, downloads_dir: Path) -> Path:
    """Generate, download, and return the official AKC form PDF."""
    form = open_entry_form(page, name, akc_number)
    form_selector = f"input[name='form'][value='{ENTRY_FORM_VALUE}']"
    try:
        form.locator(form_selector).check()
    except PlaywrightTimeoutError as error:
        if form is not page and not form.is_closed():
            form.close()
        raise AkcFormError(f"MyAKC entry chooser did not expose form {ENTRY_FORM_VALUE!r}") from error
    for field_name in CONTACT_FIELDS:
        checkbox = form.locator(f"input[name='{field_name}']")
        if checkbox.is_checked():
            checkbox.uncheck()
    try:
        with form.expect_popup(timeout=POPUP_EVENT_TIMEOUT) as report_info:
            form.locator("input[type='button'][value='Submit']").click()
        report = report_info.value
        report.wait_for_load_state("domcontentloaded")
    except PlaywrightTimeoutError as error:
        report = wait_for_context_page(form, "/apps/dynamicpdfs/index.cfm", 30_000) or form
        try:
            report.locator("a[href*='/akcecom/pdf/cms/']").wait_for(timeout=30_000)
        except PlaywrightTimeoutError as wait_error:
            if report is not form and not report.is_closed():
                report.close()
            if form is not page and not form.is_closed():
                form.close()
            raise AkcFormError("MyAKC report page did not expose a PDF link") from wait_error
    report_number = parse_qs(urlparse(report.url).query).get("dog_reg_num", [""])[0]
    if report_number and report_number != akc_number:
        if report is not form and report is not page and not report.is_closed():
            report.close()
        if form is not page and not form.is_closed():
            form.close()
        raise RuntimeError(f"MyAKC report page selected {report_number!r}, expected {akc_number!r}.")
    pdf_href = report.locator("a[href*='/akcecom/pdf/cms/']").get_attribute("href")
    if not pdf_href:
        raise RuntimeError("AKC generated a report page without a PDF link.")
    pdf_url = urljoin(report.url, pdf_href)
    response = page.context.request.get(pdf_url)
    if not response.ok:
        raise RuntimeError(f"Could not download AKC PDF: HTTP {response.status}.")
    target = downloads_dir / Path(urlparse(pdf_url).path).name
    target.write_bytes(response.body())
    if report is not form and report is not page and not report.is_closed():
        report.close()
    if form is not page and not form.is_closed():
        form.close()
    return target


def apply_pedigree_override(record: dict[str, Any], overrides: dict[str, dict[str, Any]]) -> None:
    """Apply a verified parent correction for an AKC registration number."""
    override = overrides.get(str(record.get("akc_number") or ""))
    if not override:
        return
    for parent, value in override.get("parents", {}).items():
        record[parent] = str(value.get("name") or "")
        record[f"{parent}_akc_number"] = str(value.get("akc_number") or "")
        record[f"{parent}_dobequest_id"] = str(value.get("dobequest_id") or "")
        record[f"{parent}_lookup_status"] = "manual_override"


def apply_dobequest_parents(record: dict[str, Any], dobequest: dict[str, Any]) -> None:
    """Copy DobeQuest parent names and IDs into an external record."""
    for parent in ("sire", "dam"):
        source = dobequest.get(parent) or {}
        record[parent] = str(source.get("registeredName") or "")
        record[f"{parent}_dobequest_id"] = str(source.get("did") or "")
        record[f"{parent}_lookup_status"] = "dobequest" if record[parent] else "not_collected"


def apply_schema(
    record: dict[str, Any], resolution: str, dobequest: dict[str, Any] | None = None
) -> None:
    """Add source-aware version-two fields to a scraper record."""
    akc_number = str(record.get("akc_number") or "")
    dobequest_id = str((dobequest or {}).get("did") or record.get("dobequest_id") or "")
    record["id"] = (
        f"akc:{akc_number}"
        if akc_number
        else f"dobequest:{dobequest_id}"
        if dobequest_id
        else f"unresolved:{registered_name(record['name']).casefold()}"
    )
    record["dobequest_id"] = dobequest_id or None
    record["names"] = {
        "canonical": registered_name(str(record.get("name", ""))),
        "aliases": record.get("aliases", []),
    }
    record["parents"] = {
        parent: {
            "name": record.get(parent, ""),
            "id": f"akc:{record[f'{parent}_akc_number']}"
            if record.get(f"{parent}_akc_number")
            else None,
            "akc_number": record.get(f"{parent}_akc_number") or None,
            "dobequest_id": record.get(f"{parent}_dobequest_id") or None,
            "lookup_status": record.get(f"{parent}_lookup_status", "unresolved"),
            "source": "manual_override" if record.get(f"{parent}_lookup_status") == "manual_override" else "dobequest" if record.get(f"{parent}_dobequest_id") else "akc_pdf",
        }
        for parent in ("sire", "dam")
    }
    record["sources"] = {
        "akc": {"status": resolution},
        "dobequest": {
            "status": (dobequest or {}).get(
                "match_status", record.get("sources", {}).get("dobequest", {}).get("status", "not_checked")
            )
        },
    }


def refresh_relationship_schema(records: dict[str, dict[str, Any]]) -> None:
    """Synchronize all persisted parent objects after relationship enrichment."""
    enrich_relationships(records)
    for record in records.values():
        status = record.get("sources", {}).get("akc", {}).get(
            "status", record.get("akc_lookup_status", "legacy")
        )
        apply_schema(record, status)


def hydrate_record(record: dict[str, Any]) -> None:
    """Restore internal fields from persisted schema data."""
    record["aliases"] = record.get("names", {}).get("aliases", [record.get("name", "")])
    for parent, value in record.get("parents", {}).items():
        record[parent] = value.get("name", "")
        record[f"{parent}_akc_number"] = value.get("akc_number") or ""
        record[f"{parent}_dobequest_id"] = value.get("dobequest_id") or ""
        record[f"{parent}_lookup_status"] = value.get("lookup_status", "unresolved")


def serialized_record(record: dict[str, Any]) -> dict[str, Any]:
    """Remove internal duplicate fields from a persisted record."""
    result = deepcopy(record)
    result.pop("id", None)
    result.pop("aliases", None)
    for parent in ("sire", "dam"):
        result.pop(f"{parent}_akc_number", None)
        result.pop(f"{parent}_dobequest_id", None)
        result.pop(f"{parent}_lookup_status", None)
    return result


def write_unresolved_reports(records: dict[str, dict[str, Any]], directory: Path) -> None:
    """Write one name-per-line unresolved reports grouped by AKC status."""
    statuses = {"not_found", "ambiguous", "too_many_matches", "number_mismatch", "name_mismatch"}
    for status in statuses:
        names = sorted(record["name"] for record in records.values() if record.get("akc_lookup_status") == status)
        (directory / f"unresolved-{status}.txt").write_text("\n".join(names) + ("\n" if names else ""), encoding="utf-8")


def save_records(records: dict[str, dict[str, Any]], output: Path) -> None:
    """Write versioned checkpointed pedigree records as a dataset."""
    dataset = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "records": {key: serialized_record(record) for key, record in records.items()},
    }
    output.write_text(json.dumps(dataset, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_unresolved_reports(records, output.parent)


def remove_dog_of_interest(page: Page, name: str, akc_number: str) -> None:
    """Remove a successfully processed Dog of Interest from MyAKC."""
    if "dog_management" not in page.url:
        page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
    selector = f"input[type='checkbox'][onclick*='dog_reg_number={akc_number}']"
    if akc_number and not page.locator(selector).count():
        return
    row = managed_row(page, name, akc_number)
    select_managed_row(page, row)
    page.locator("select[name='action_items']").select_option("1")
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=15_000):
            page.locator("#go_filters").click(no_wait_after=True)
    except PlaywrightTimeoutError:
        log_step("MyAKC cleanup navigation timed out; continuing scrape", 2)
        page.goto(MANAGE_DOGS_URL, wait_until="domcontentloaded")
        return
    if (
        akc_number
        and page.locator(f"input[type='checkbox'][onclick*='dog_reg_number={akc_number}']").count()
    ):
        log_step(f"MyAKC did not remove AKC number {akc_number}; continuing scrape", 2)


def collect(
    browser: Browser,
    username: str,
    password: str,
    root_name: str,
    breed: str,
    output: Path,
    downloads_dir: Path,
    levels: int | None,
    login_timeout: int,
    auth_state: Path,
    remove_after_processing: bool,
    dobequest_data: Path | None,
    dobequest_overrides: Path | None,
    akc_number_overrides: Path | None,
    pedigree_overrides: Path | None,
    retry_not_found: bool,
    retry_unresolved: bool,
    prefer_local_pdfs: bool,
    journal_path: Path | None,
    checkpoint_every: int,
    http_optimization: bool,
    record_har: Path | None,
) -> None:
    """Collect a pedigree breadth-first and checkpoint each dog."""
    context_options: dict[str, Any] = {"accept_downloads": True}
    if record_har:
        record_har.parent.mkdir(parents=True, exist_ok=True)
        context_options.update({"record_har_path": str(record_har), "record_har_content": "embed", "record_har_mode": "full"})
    if auth_state.exists():
        context_options["storage_state"] = str(auth_state)
    context = browser.new_context(**context_options)
    page = context.new_page()
    login(page, username, password, login_timeout)
    context.storage_state(path=str(auth_state))
    dobequest_index = load_dobequest_index(dobequest_data)
    dobequest_override_map = load_dobequest_overrides(dobequest_overrides)
    akc_number_override_map = load_akc_number_overrides(akc_number_overrides)
    pedigree_override_map = load_pedigree_overrides(pedigree_overrides)
    journal = DatasetJournal(output, journal_path)
    existing_data = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    records: dict[str, dict[str, Any]] = existing_data.get("records", existing_data)
    replayed = journal.replay(records)
    if replayed:
        log_step(f"Replayed {replayed} un-compacted dataset journal events")
    for record in records.values():
        hydrate_record(record)
        apply_pedigree_override(record, pedigree_override_map)
    refresh_relationship_schema(records)
    if records:
        save_records(records, output)
        if journal.journal_path.exists():
            journal.journal_path.write_text("", encoding="utf-8")
    dirty_records = 0

    def persist(upsert_keys: list[str], delete_keys: list[str] | None = None) -> None:
        nonlocal dirty_records
        for key in upsert_keys:
            if key in records:
                journal.upsert(key, serialized_record(records[key]))
                dirty_records += 1
        for key in delete_keys or []:
            journal.delete(key)
            dirty_records += 1
        if dirty_records >= checkpoint_every:
            refresh_relationship_schema(records)
            journal.compact(lambda: save_records(records, output))
            dirty_records = 0
            log_step("Compacted dataset journal into dogs.json")

    pending = deque([(root_name, 1)])
    records_by_name = {
        alias.casefold(): record
        for record in records.values()
        for alias in record.get("aliases", [record.get("name", "")])
    }
    expanded: set[str] = set()
    downloads_dir.mkdir(parents=True, exist_ok=True)
    pdf_number_index = build_pdf_number_index(downloads_dir)
    pdf_name_index = build_pdf_name_index(downloads_dir) if prefer_local_pdfs else {}
    if prefer_local_pdfs:
        pdf_file_count = len(list(downloads_dir.glob("*.pdf")))
        pdf_identity_count = sum(len(matches) for matches in pdf_name_index.values())
        log_step(f"Indexed {pdf_identity_count} unique PDF identities from {pdf_file_count} local files")
    while pending:
        requested_name, level = pending.popleft()
        name_key = requested_name.casefold()
        if name_key in expanded:
            continue
        record = records_by_name.get(name_key)
        log_dog(requested_name, level)
        replacement_key = None
        unresolved_statuses = {"not_found", "ambiguous", "too_many_matches", "number_mismatch", "name_mismatch"}
        retryable = record and (
            record.get("akc_lookup_status") == "not_found" and retry_not_found
            or record.get("akc_lookup_status") in unresolved_statuses and retry_unresolved
        )
        if retryable:
            replacement_key = next(key for key, value in records.items() if value is record)
            record = None
        elif record and record.get("akc_lookup_status") in unresolved_statuses:
            log_step(f"Skipping prior unresolved record ({record['akc_lookup_status']})")
            expanded.add(name_key)
            continue
        elif record and record.get("akc_lookup_status") == "external_only":
            log_step("Using existing DobeQuest-only record; no AKC request needed")
            expanded.add(name_key)
            if levels is None or level < levels:
                for parent in (record["sire"], record["dam"]):
                    if parent and parent.casefold() not in expanded:
                        pending.append((parent, level + 1))
            continue
        from_local_pdf = False
        if not record and prefer_local_pdfs and level > 1:
            pdf = local_pdf_match(pdf_name_index, requested_name)
            if pdf:
                log_step(f"Reusing local PDF by normalized name: {pdf.name}")
                record = extract_pdf(pdf)
                record.update(
                    {
                        "breed_variety": "",
                        "color": "",
                        "markings": "",
                        "medical_screens": {},
                        "ofa_number": "",
                    }
                )
                record["akc_lookup_status"] = "resolved_from_pdf_cache"
                record["pdf_id"] = pdf.stem
                record["aliases"] = aliases_for(requested_name, record["name"])
                record["collected_at"] = datetime.now(timezone.utc).isoformat()
                apply_pedigree_override(record, pedigree_override_map)
                key = record["akc_number"] or record["name"]
                records[key] = record
                records_by_name[name_key] = record
                for alias in record["aliases"]:
                    records_by_name[alias.casefold()] = record
                apply_schema(record, record["akc_lookup_status"])
                if record["id"] != key:
                    records[record["id"]] = records.pop(key)
                if replacement_key and replacement_key in records:
                    records.pop(replacement_key)
                persist([record["id"]], [replacement_key] if replacement_key else [])
                from_local_pdf = True
        if record:
            log_step(f"Using existing checkpoint ({record.get('akc_lookup_status', 'legacy')})")
        if not record:
            dobequest = None
            resolution = "resolved_by_name"
            akc_override = akc_number_override_map.get(registered_name(requested_name).casefold())
            try:
                if akc_override:
                    log_step(f"Using verified AKC override: {akc_override['akc_number']}", 1)
                    canonical_name, selected_akc_number = choose_dog_by_number(page, akc_override["akc_number"])
                    resolution = "resolved_by_manual_akc_override"
                else:
                    log_step("Searching AKC by name", 1)
                    if http_optimization:
                        try:
                            canonical_name, selected_akc_number = direct_name_lookup_add(page, requested_name, breed)
                        except AkcLookupError as http_error:
                            log_step(f"Direct name lookup requires browser fallback: {http_error}", 2)
                            canonical_name, selected_akc_number = choose_dog(page, requested_name, breed)
                    else:
                        canonical_name, selected_akc_number = choose_dog(page, requested_name, breed)
            except AkcLookupError as error:
                log_step(f"AKC name lookup: {error.status}", 1)
                log_step("Checking DobeQuest identity index", 1)
                dobequest = dobequest_match(
                    dobequest_index, requested_name, dobequest_override_map
                )
                if dobequest and dobequest["akc_number"]:
                    log_step(
                        f"DobeQuest match {dobequest['did']} → AKC {dobequest['akc_number']}", 2
                    )
                    try:
                        canonical_name, selected_akc_number = choose_dog_by_number(
                            page, dobequest["akc_number"]
                        )
                        resolution = "resolved_by_dobequest_akc_number"
                    except AkcLookupError as number_error:
                        error = number_error
                        dobequest = {**dobequest, "akc_number": ""}
                if dobequest and not dobequest["akc_number"]:
                    log_step(f"DobeQuest-only profile {dobequest['did']}; skipping AKC PDF", 2)
                    record = unresolved_record(requested_name, breed, "external_only")
                    apply_dobequest_parents(record, dobequest)
                    apply_schema(record, "external_only", dobequest)
                    key = record["id"]
                    records[key] = record
                    records_by_name[name_key] = record
                    if replacement_key and replacement_key in records:
                        records.pop(replacement_key)
                    persist([record["id"]], [replacement_key] if replacement_key else [])
                    expanded.add(name_key)
                    if levels is None or level < levels:
                        for parent in (record["sire"], record["dam"]):
                            if parent and parent.casefold() not in expanded:
                                pending.append((parent, level + 1))
                    continue
                if not dobequest:
                    log_step(f"No DobeQuest match; saving unresolved status ({error.status})", 2)
                    record = unresolved_record(requested_name, breed, error.status)
                    apply_schema(record, error.status, dobequest)
                    key = f"unresolved:{re.sub(r'[^a-z0-9]+', '-', name_key).strip('-')}"
                    records[key] = record
                    records_by_name[name_key] = record
                    persist([record["id"]], [replacement_key] if replacement_key else [])
                    expanded.add(name_key)
                    continue
            log_step(f"Resolved AKC identity: {canonical_name} ({selected_akc_number})", 1)
            log_step("Reading MyAKC Dog Information", 1)
            details = dog_details(page, canonical_name, selected_akc_number)
            myakc_breed = str(details.pop("_myakc_breed", ""))
            if resolution == "resolved_by_dobequest_akc_number" and (
                not pdf_identity_matches(requested_name, canonical_name, dobequest)
                or not same_breed(breed, myakc_breed)
            ):
                log_step(
                    f"DobeQuest AKC mismatch: MyAKC returned {canonical_name} ({myakc_breed})",
                    1,
                )
                remove_dog_of_interest(page, canonical_name, selected_akc_number)
                record = unresolved_record(requested_name, breed, "external_only")
                apply_dobequest_parents(record, dobequest)
                apply_schema(record, "external_only", dobequest)
                records[record["id"]] = record
                records_by_name[name_key] = record
                if replacement_key and replacement_key in records:
                    records.pop(replacement_key)
                persist([record["id"]], [replacement_key] if replacement_key else [])
                expanded.add(name_key)
                if levels is None or level < levels:
                    for parent in (record["sire"], record["dam"]):
                        if parent and parent.casefold() not in expanded:
                            pending.append((parent, level + 1))
                continue
            pdf = reusable_pdf(downloads_dir, selected_akc_number, mapped_pdf_ids(records), pdf_number_index)
            if pdf:
                log_step(f"Reusing saved PDF: {pdf.name}", 1)
            else:
                log_step("Generating compact Herding Test/Trial PDF", 1)
                try:
                    pdf = download_entry_form(page, canonical_name, selected_akc_number, downloads_dir)
                except (AkcFormError, PlaywrightTimeoutError) as error:
                    log_step(f"AKC form generation failed after retries: {error}", 1)
                    record = unresolved_record(requested_name, breed, "form_timeout")
                    apply_schema(record, "form_timeout", dobequest)
                    records[record["id"]] = record
                    records_by_name[name_key] = record
                    if remove_after_processing:
                        remove_dog_of_interest(page, canonical_name, selected_akc_number)
                    persist([record["id"]], [replacement_key] if replacement_key else [])
                    expanded.add(name_key)
                    continue
            record = extract_pdf(pdf)
            verified_dobequest_number = (
                dobequest
                and dobequest.get("akc_number") == selected_akc_number == record.get("akc_number")
                and resolution == "resolved_by_dobequest_akc_number"
            )
            if not verified_dobequest_number and not pdf_identity_matches(requested_name, record["name"], dobequest):
                log_step(f"PDF identity mismatch: {record['name']}; rejecting AKC result", 1)
                dobequest = dobequest_match(dobequest_index, requested_name, dobequest_override_map)
                if dobequest and dobequest["akc_number"] and dobequest["akc_number"] != selected_akc_number:
                    log_step(f"Retrying verified DobeQuest AKC number: {dobequest['akc_number']}", 2)
                    try:
                        canonical_name, selected_akc_number = choose_dog_by_number(page, dobequest["akc_number"])
                        details = dog_details(page, canonical_name, selected_akc_number)
                        pdf = reusable_pdf(downloads_dir, selected_akc_number, mapped_pdf_ids(records), pdf_number_index)
                        if not pdf:
                            pdf = download_entry_form(page, canonical_name, selected_akc_number, downloads_dir)
                        record = extract_pdf(pdf)
                        resolution = "resolved_by_dobequest_akc_number"
                    except AkcLookupError:
                        dobequest = None
                verified_dobequest_number = (
                dobequest
                and dobequest.get("akc_number") == selected_akc_number == record.get("akc_number")
                and resolution == "resolved_by_dobequest_akc_number"
            )
            if not verified_dobequest_number and not pdf_identity_matches(requested_name, record["name"], dobequest):
                    record = unresolved_record(requested_name, breed, "name_mismatch")
                    apply_schema(record, "name_mismatch", dobequest)
                    key = record["id"]
                    records[key] = record
                    records_by_name[name_key] = record
                    if remove_after_processing:
                        remove_dog_of_interest(page, canonical_name, selected_akc_number)
                    persist([record["id"]], [replacement_key] if replacement_key else [])
                    expanded.add(name_key)
                    continue
            record.update(details)
            apply_pedigree_override(record, pedigree_override_map)
            record["akc_lookup_status"] = resolution
            record["pdf_id"] = pdf.stem
            record["aliases"] = aliases_for(
                requested_name if verified_dobequest_number else canonical_name,
                canonical_name,
                record["name"],
            )
            record["collected_at"] = datetime.now(timezone.utc).isoformat()
            if not record["name"]:
                raise RuntimeError(f"No dog information was found in {pdf.name}.")
            key = record["akc_number"] or record["name"]
            records[key] = record
            records_by_name[name_key] = record
            for alias in record["aliases"]:
                records_by_name[alias.casefold()] = record
            apply_schema(record, resolution, dobequest)
            if record["id"] != key:
                records[record["id"]] = records.pop(key)
            if replacement_key and replacement_key in records:
                records.pop(replacement_key)
            persist([record["id"]], [replacement_key] if replacement_key else [])
            log_step(f"Saved record as {record['id']}", 1)
        elif (
            "breed_variety" not in record
            or "color" not in record
            or "markings" not in record
            or "ofa_number" not in record
            or "medical_screens" not in record
            or ":" in str(record.get("breed_variety", ""))
            or ":" in str(record.get("markings", ""))
        ):
            print(f"Updating MyAKC details: {requested_name}", flush=True)
            record.update(dog_details(page, requested_name, record["akc_number"]))
            persist([record["id"]], [replacement_key] if replacement_key else [])
        if remove_after_processing and not from_local_pdf:
            log_step("Removing processed Dog of Interest from MyAKC", 1)
            remove_dog_of_interest(page, record["name"], record["akc_number"])
        expanded.add(name_key)
        if levels is None or level < levels:
            parents = [
                parent
                for parent in (record["sire"], record["dam"])
                if parent and parent.casefold() not in expanded
            ]
            if parents:
                log_step(f"Queueing {len(parents)} parent record(s) for generation {level + 1}", 1)
            for parent in parents:
                pending.append((parent, level + 1))
    if journal.journal_path.exists() and journal.journal_path.stat().st_size:
        refresh_relationship_schema(records)
        journal.compact(lambda: save_records(records, output))
        log_step("Compacted final dataset journal into dogs.json")
    context.close()
    print(f"Saved {len(records)} dogs to {output}")


def main() -> int:
    """Parse CLI options, launch Chromium, and run the collection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="Starting dog's registered name")
    parser.add_argument("--breed", default="Doberman Pinschers")
    parser.add_argument("--output", type=Path, default=Path("dogs.json"))
    parser.add_argument("--downloads-dir", type=Path, default=Path("downloads"))
    parser.add_argument(
        "--levels",
        type=int,
        help="Number of pedigree levels to collect; 1 collects only the named dog",
    )
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=300,
        help="Seconds to allow for login and MFA completion (default: 300)",
    )
    parser.add_argument(
        "--auth-state",
        type=Path,
        default=Path(".akc-auth-state.json"),
        help="Local browser session cache (default: .akc-auth-state.json)",
    )
    parser.add_argument(
        "--dobequest-data",
        type=Path,
        default=Path("dobe-quest_2026.json"),
        help="Optional DobeQuest JSON for exact-name AKC-number fallback",
    )
    parser.add_argument(
        "--dobequest-overrides",
        type=Path,
        default=Path("dobequest_overrides.json"),
        help="Verified DobeQuest name-to-profile override JSON",
    )
    parser.add_argument(
        "--akc-number-overrides",
        type=Path,
        default=Path("akc_number_overrides.json"),
        help="Verified name-to-AKC-number override JSON",
    )
    parser.add_argument(
        "--pedigree-overrides",
        type=Path,
        default=Path("pedigree_overrides.json"),
        help="Verified AKC-number-to-parent override JSON",
    )
    parser.add_argument(
        "--remove-after-processing",
        action="store_true",
        help="Remove each processed Dog of Interest after its record is saved",
    )
    parser.add_argument(
        "--retry-not-found",
        action="store_true",
        help="Retry records previously marked not_found",
    )
    parser.add_argument(
        "--retry-unresolved",
        action="store_true",
        help="Retry all unresolved records, including historical name mismatches",
    )
    parser.add_argument(
        "--prefer-local-pdfs",
        action="store_true",
        help="Reuse uniquely name-matched local PDFs before MyAKC lookup",
    )
    parser.add_argument("--journal", type=Path, help="Append-only dataset journal path (default: output with .jsonl suffix).")
    parser.add_argument("--checkpoint-every", type=int, default=100, help="Compact journal into --output after this many record updates.")
    parser.add_argument("--http-optimization", action="store_true", help="Use direct authenticated HTTP where response validation is available, with browser fallback.")
    parser.add_argument("--record-har", type=Path, help="Record all browser-context page and popup traffic to this HAR file.")
    parser.add_argument("--headed", action="store_true", help="Show the browser while it runs")
    args = parser.parse_args()
    if args.levels is not None and args.levels < 1:
        parser.error("--levels must be at least 1")
    if args.login_timeout < 1 or args.checkpoint_every < 1:
        parser.error("--login-timeout and --checkpoint-every must be at least 1")
    username = os.environ.get("AKC_USERNAME")
    password = os.environ.get("AKC_PASSWORD")
    if not username or not password:
        parser.error("set AKC_USERNAME and AKC_PASSWORD environment variables")
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
        try:
            collect(
                browser,
                username,
                password,
                args.name,
                args.breed,
                args.output,
                args.downloads_dir,
                args.levels,
                args.login_timeout,
                args.auth_state,
                args.remove_after_processing,
                args.dobequest_data,
                args.dobequest_overrides,
                args.akc_number_overrides,
                args.pedigree_overrides,
                args.retry_not_found,
                args.retry_unresolved,
                args.prefer_local_pdfs,
                args.journal,
                args.checkpoint_every,
                args.http_optimization,
                args.record_har,
            )
        except PlaywrightError as error:
            if not stop_requested["value"] and "ERR_ABORTED" not in str(error):
                raise
            interrupted = True
            print("Browser navigation aborted during requested shutdown.", flush=True)
        except KeyboardInterrupt:
            interrupted = True
            print("Forced interrupt received; journal entries are durable and will be replayed on the next run.", flush=True)
        finally:
            browser.close()
    signal.signal(signal.SIGINT, previous_sigint)
    return 130 if interrupted or stop_requested["value"] else 0


if __name__ == "__main__":
    sys.exit(main())