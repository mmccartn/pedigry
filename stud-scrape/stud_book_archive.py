#!/usr/bin/env python3
"""Inventory and download public AKC Stud Book OCR files from Internet Archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

COLLECTION = "pub_american-kennel-club-stud-book-register"
SIM_PUB_ID = "8212"
IDENTIFIER_PREFIX = "sim_american-kennel-club-stud-book-register"
ADVANCED_SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{identifier}"
DOWNLOAD_URL = "https://archive.org/download/{identifier}/{filename}"
USER_AGENT = "akc-stud-book-archive/1.0 (public OCR downloader)"
FILE_KINDS = {
    "djvu_xml": "_djvu.xml",
    "djvu_txt": "_djvu.txt",
    "item_metadata": "_meta.xml",
    "scan_data": "_scandata.xml",
    "page_numbers": "_page_numbers.json",
    "file_inventory": "_files.xml",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def request_json(url: str, retries: int, delay: float) -> dict[str, Any]:
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            if attempt == retries:
                raise RuntimeError(f"Request failed for {url}: {error}") from error
            time.sleep(delay * (2**attempt))
    raise AssertionError("unreachable")


def fetch_collection_items(retries: int, delay: float) -> list[dict[str, Any]]:
    fields = ["identifier", "title", "date", "volume", "issue"]
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        params: list[tuple[str, str]] = [
            ("q", f"collection:{COLLECTION}"),
            ("rows", "100"),
            ("page", str(page)),
            ("output", "json"),
        ]
        params.extend(("fl[]", field) for field in fields)
        payload = request_json(f"{ADVANCED_SEARCH_URL}?{urlencode(params)}", retries, delay)
        response = payload["response"]
        items.extend(response["docs"])
        if len(items) >= response["numFound"]:
            return sorted(items, key=lambda item: item["identifier"])
        page += 1
        time.sleep(delay)


def metadata_for(identifier: str, retries: int, delay: float) -> dict[str, Any]:
    return request_json(METADATA_URL.format(identifier=quote(identifier, safe="")), retries, delay)


def metadata_if_exists(identifier: str, retries: int, delay: float) -> dict[str, Any] | None:
    url = METADATA_URL.format(identifier=quote(identifier, safe=""))
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 404:
                return None
            if attempt == retries:
                raise RuntimeError(f"Request failed for {url}: {error}") from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            if attempt == retries:
                raise RuntimeError(f"Request failed for {url}: {error}") from error
        time.sleep(delay * (2**attempt))
    raise AssertionError("unreachable")


def matching_files(files: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for kind, suffix in FILE_KINDS.items():
        matches = [file for file in files if str(file.get("name", "")).endswith(suffix)]
        if len(matches) == 1:
            selected[kind] = matches[0]
    return selected


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"collection": COLLECTION, "created_at": utc_now(), "items": {}}
    with path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("collection") != COLLECTION or not isinstance(manifest.get("items"), dict):
        raise ValueError(f"{path} is not a {COLLECTION} manifest")
    return manifest


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def item_year(item: dict[str, Any]) -> int | None:
    value = str(item.get("date", ""))[:4]
    return int(value) if value.isdigit() else None


def selected_items(
    items: list[dict[str, Any]],
    year_from: int | None,
    year_to: int | None,
    identifiers: set[str] | None = None,
) -> list[dict[str, Any]]:
    chosen = []
    for item in items:
        if identifiers and item["identifier"] not in identifiers:
            continue
        year = item_year(item)
        if (year_from is not None and (year is None or year < year_from)) or (
            year_to is not None and (year is None or year > year_to)
        ):
            continue
        chosen.append(item)
    return chosen


def update_inventory(
    manifest: dict[str, Any], manifest_path: Path, items: list[dict[str, Any]], retries: int, delay: float
) -> set[str]:
    next_identifiers: set[str] = set()
    for index, item in enumerate(items, start=1):
        identifier = item["identifier"]
        existing = manifest["items"].get(identifier, {})
        print(f"[{index}/{len(items)}] Inventorying {identifier}", flush=True)
        try:
            payload = item.get("_payload") or metadata_for(identifier, retries, delay)
        except RuntimeError as error:
            manifest["items"][identifier] = {
                **existing,
                "identifier": identifier,
                "title": item.get("title", existing.get("title", "")),
                "date": item.get("date", existing.get("date", "")),
                "volume": item.get("volume", existing.get("volume", "")),
                "issue": item.get("issue", existing.get("issue", "")),
                "inventory_source": item.get("inventory_source", existing.get("inventory_source", "collection_search")),
                "inventory_status": "metadata_request_failed",
                "inventory_error": str(error),
                "inventory_updated_at": utc_now(),
            }
            print(f"Failed: {error}", file=sys.stderr, flush=True)
        else:
            metadata = payload.get("metadata", {})
            next_identifier = str(metadata.get("next_item", "")).strip()
            if next_identifier:
                next_identifiers.add(next_identifier)
            files = matching_files(payload.get("files", []))
            previous_files = existing.get("files", {})
            file_records = {}
            for kind, file in files.items():
                previous = previous_files.get(kind, {})
                file_records[kind] = {
                    **(previous if previous.get("remote_name") == file["name"] else {}),
                    "remote_name": file["name"],
                    "remote_size": int(file.get("size", 0) or 0),
                    "remote_sha1": file.get("sha1", ""),
                    "remote_md5": file.get("md5", ""),
                    "remote_private": str(file.get("private", "")).lower() == "true",
                    "status": previous.get("status", "pending") if previous.get("remote_name") == file["name"] else "pending",
                }
            manifest["items"][identifier] = {
                "identifier": identifier,
                "title": metadata.get("title", item.get("title", "")),
                "date": metadata.get("date", item.get("date", "")),
                "volume": metadata.get("volume", item.get("volume", "")),
                "issue": metadata.get("issue", item.get("issue", "")),
                "previous_item": metadata.get("previous_item", ""),
                "next_item": next_identifier,
                "inventory_source": item.get("inventory_source", existing.get("inventory_source", "collection_search")),
                "inventory_status": "complete" if "djvu_xml" in files else "missing_djvu_xml",
                "inventory_updated_at": utc_now(),
                "files": file_records,
            }
        manifest["updated_at"] = utc_now()
        save_manifest(manifest_path, manifest)
        time.sleep(delay)
    return next_identifiers


def discover_monthly_items(
    manifest: dict[str, Any],
    manifest_path: Path,
    year_from: int,
    year_to: int,
    volume_from: int,
    retries: int,
    delay: float,
) -> tuple[int, int, int]:
    found = absent = invalid = 0
    for year in range(year_from, year_to + 1):
        volume = volume_from + year - year_from
        for month in range(1, 13):
            identifier = f"{IDENTIFIER_PREFIX}_{year}-{month:02d}_{volume}_{month}"
            print(f"Discovering {identifier}", flush=True)
            try:
                payload = metadata_if_exists(identifier, retries, delay)
            except RuntimeError as error:
                print(f"Failed: {error}", file=sys.stderr, flush=True)
                invalid += 1
                continue
            if payload is None:
                absent += 1
                continue
            metadata = payload.get("metadata", {})
            files = matching_files(payload.get("files", []))
            if (
                metadata.get("identifier") != identifier
                or str(metadata.get("sim_pubid", "")) != SIM_PUB_ID
                or str(metadata.get("date", "")) != f"{year}-{month:02d}"
                or str(metadata.get("volume", "")) != str(volume)
                or str(metadata.get("issue", "")) != str(month)
                or "djvu_xml" not in files
            ):
                invalid += 1
                continue
            update_inventory(
                manifest,
                manifest_path,
                [{"identifier": identifier, "_payload": payload, "inventory_source": "identifier_pattern"}],
                retries,
                delay,
            )
            found += 1
    manifest["last_discovery"] = {
        "year_from": year_from,
        "year_to": year_to,
        "volume_from": volume_from,
        "found": found,
        "absent": absent,
        "invalid": invalid,
        "completed_at": utc_now(),
    }
    manifest["updated_at"] = utc_now()
    save_manifest(manifest_path, manifest)
    return found, absent, invalid


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_local_file(record: dict[str, Any], destination: Path) -> bool:
    if not destination.is_file():
        return False
    expected_size = record.get("remote_size", 0)
    if expected_size and destination.stat().st_size != expected_size:
        return False
    return record.get("sha256") == sha256(destination)


def download_file(identifier: str, record: dict[str, Any], destination: Path, retries: int, delay: float) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.part")
    url = DOWNLOAD_URL.format(identifier=quote(identifier, safe=""), filename=quote(record["remote_name"]))
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=120) as response, temporary.open("wb") as handle:
                shutil.copyfileobj(response, handle, length=1024 * 1024)
            expected_size = record.get("remote_size", 0)
            if expected_size and temporary.stat().st_size != expected_size:
                raise RuntimeError(f"Expected {expected_size} bytes, received {temporary.stat().st_size}")
            temporary.replace(destination)
            record["path"] = str(destination)
            record["sha256"] = sha256(destination)
            record["downloaded_at"] = utc_now()
            record["status"] = "complete"
            return
        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError) as error:
            temporary.unlink(missing_ok=True)
            if isinstance(error, HTTPError) and error.code in {401, 403}:
                record["status"] = "unavailable"
                record["error"] = f"HTTP {error.code}: Archive access is restricted"
                raise RuntimeError(record["error"]) from error
            if attempt == retries:
                record["status"] = "failed"
                record["error"] = str(error)
                raise RuntimeError(f"Download failed: {error}") from error
            time.sleep(delay * (2**attempt))
    raise AssertionError("unreachable")


def download_items(
    manifest: dict[str, Any],
    manifest_path: Path,
    output_dir: Path,
    retries: int,
    delay: float,
    include_text: bool,
    year_from: int | None,
    year_to: int | None,
    identifiers: set[str] | None,
) -> int:
    failures = 0
    for identifier, item in manifest["items"].items():
        if not selected_items([item], year_from, year_to, identifiers):
            continue
        files = item.get("files", {})
        requested = ["djvu_xml", "item_metadata", "scan_data", "page_numbers", "file_inventory"]
        if include_text:
            requested.append("djvu_txt")
        for kind in requested:
            record = files.get(kind)
            if not record:
                continue
            if record.get("status") == "unavailable" or record.get("remote_private"):
                record["status"] = "unavailable"
                record.setdefault("error", "Archive marks this derivative as private")
                item["download_status"] = "unavailable"
                manifest["updated_at"] = utc_now()
                save_manifest(manifest_path, manifest)
                break
            destination = output_dir / identifier / record["remote_name"]
            if verified_local_file(record, destination):
                record["path"] = str(destination)
                record["status"] = "complete"
                continue
            print(f"Downloading {identifier}: {record['remote_name']}", flush=True)
            try:
                download_file(identifier, record, destination, retries, delay)
            except RuntimeError as error:
                failures += 1
                print(f"Failed: {error}", file=sys.stderr, flush=True)
            manifest["updated_at"] = utc_now()
            save_manifest(manifest_path, manifest)
            if record.get("status") == "unavailable":
                item["download_status"] = "unavailable"
                break
            time.sleep(delay)
    return failures


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=Path, default=Path("data/manifest.json"))
    parser.add_argument("--year-from", type=int)
    parser.add_argument("--year-to", type=int)
    parser.add_argument("--identifier", action="append", help="Restrict to an Internet Archive identifier; repeatable")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between requests (default: 0.5)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory", help="Build or refresh the collection manifest")
    add_common_options(inventory)
    inventory.add_argument(
        "--follow-next-items",
        action="store_true",
        help="Follow next_item metadata links, including Archive search-hidden issues",
    )
    follow_links = commands.add_parser("follow-links", help="Follow next_item links from every manifest item")
    add_common_options(follow_links)
    follow_links.add_argument("--start-after", help="Resume after this manifest identifier")
    discover = commands.add_parser("discover", help="Probe validated monthly identifiers omitted from Archive search")
    add_common_options(discover)
    discover.set_defaults(year_from=1969, year_to=2004)
    discover.add_argument("--volume-from", type=int, help="Volume for --year-from (defaults to year minus 1883)")
    download = commands.add_parser("download", help="Download files described by the manifest")
    add_common_options(download)
    download.add_argument("--output-dir", type=Path, default=Path("data/volumes"))
    download.add_argument("--without-text", action="store_true", help="Skip flattened DjVu text files")
    download.add_argument("--refresh-inventory", action="store_true", help="Refresh collection metadata before downloading")
    args = parser.parse_args()

    if args.retries < 0 or args.delay < 0:
        parser.error("--retries and --delay must be non-negative")
    manifest = load_manifest(args.manifest)
    if args.command == "follow-links":
        identifiers = sorted(manifest["items"])
        if args.start_after:
            if args.start_after not in manifest["items"]:
                parser.error("--start-after must be an identifier in the manifest")
            identifiers = [identifier for identifier in identifiers if identifier > args.start_after]
        next_identifiers = update_inventory(
            manifest,
            args.manifest,
            [{"identifier": identifier} for identifier in identifiers],
            args.retries,
            args.delay,
        )
        while pending := sorted(identifier for identifier in next_identifiers if identifier not in manifest["items"]):
            next_identifiers = update_inventory(
                manifest,
                args.manifest,
                [{"identifier": identifier, "inventory_source": "next_item_link"} for identifier in pending],
                args.retries,
                args.delay,
            )
        manifest["updated_at"] = utc_now()
        save_manifest(args.manifest, manifest)
        print(f"Saved {len(manifest['items'])} items to {args.manifest}")
        return 0
    if args.command == "discover":
        found, absent, invalid = discover_monthly_items(
            manifest,
            args.manifest,
            args.year_from,
            args.year_to,
            args.volume_from if args.volume_from is not None else args.year_from - 1883,
            args.retries,
            args.delay,
        )
        print(f"Discovery complete: {found} found, {absent} absent, {invalid} invalid or failed")
        return 0
    if args.command == "inventory" or args.refresh_inventory:
        items = selected_items(
            fetch_collection_items(args.retries, args.delay),
            args.year_from,
            args.year_to,
            set(args.identifier) if args.identifier else None,
        )
        next_identifiers = update_inventory(manifest, args.manifest, items, args.retries, args.delay)
        if getattr(args, "follow_next_items", False):
            while pending := sorted(identifier for identifier in next_identifiers if identifier not in manifest["items"]):
                next_identifiers = update_inventory(
                    manifest,
                    args.manifest,
                    [{"identifier": identifier} for identifier in pending],
                    args.retries,
                    args.delay,
                )
        manifest["updated_at"] = utc_now()
        save_manifest(args.manifest, manifest)
    if args.command == "inventory":
        print(f"Saved {len(manifest['items'])} items to {args.manifest}")
        return 0
    if not manifest["items"]:
        parser.error("Manifest is empty; run inventory first or use --refresh-inventory")
    failures = download_items(
        manifest,
        args.manifest,
        args.output_dir,
        args.retries,
        args.delay,
        not args.without_text,
        args.year_from,
        args.year_to,
        set(args.identifier) if args.identifier else None,
    )
    manifest["updated_at"] = utc_now()
    save_manifest(args.manifest, manifest)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
