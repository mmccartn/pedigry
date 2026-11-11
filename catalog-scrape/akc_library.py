#!/usr/bin/env python3
"""Index and download AKC Library show catalogs, one request at a time.

The AKC Library (library.akc.org) is a WordPress/WooCommerce site. Show
catalogs are "products" filed under per-year categories
(``show-catalogs-<year>``) and per-club categories. The public WordPress REST
API lists them; each product page embeds an S3-hosted PDF.

All network access goes through :class:`PoliteSession`, which keeps every
request sequential and spaced by at least ``--delay`` seconds (default 1 s).

Examples::

    python akc_library.py index --year-from 1930 --year-to 1939
    python akc_library.py download --year 1934 --limit 5
    python akc_library.py download --year 1934 --club "westminster|morris"
    python akc_library.py status
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from breeds import club_relevance
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
MANIFEST_PATH = DATA / "manifest.json"
PDF_DIR = DATA / "pdfs"

SITE = "https://library.akc.org"
REST = SITE + "/index.php"
USER_AGENT = "pedigry-research/0.1 (Doberman Pinscher pedigree research; sequential, rate-limited)"
EMBED_PATTERN = re.compile(r"""<embed[^>]+src=["']([^"']+\.pdf)["']""", re.I)
ANY_PDF_PATTERN = re.compile(r"""["'](https?://[^"']+\.pdf)["']""", re.I)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(message, flush=True)


class PoliteSession:
    """A sequential HTTP session with a minimum interval between requests."""

    def __init__(self, delay: float = 1.0, retries: int = 4, timeout: float = 120.0) -> None:
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._last = 0.0

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def get(self, url: str, *, params: dict[str, Any] | None = None, stream: bool = False) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait()
            try:
                response = self.session.get(url, params=params, timeout=self.timeout, stream=stream)
            except requests.RequestException as err:
                last_error = err
                response = None
            finally:
                self._last = time.monotonic()
            if response is not None:
                if response.status_code < 500 and response.status_code != 429:
                    return response
                last_error = RuntimeError(f"HTTP {response.status_code} for {response.url}")
                response.close()
            backoff = min(120.0, 5.0 * (2 ** attempt))
            log(f"    retry {attempt + 1}/{self.retries} in {backoff:.0f}s: {last_error}")
            time.sleep(backoff)
        raise RuntimeError(f"giving up on {url}: {last_error}")

    def rest(self, route: str, **params: Any) -> requests.Response:
        params = {"rest_route": route, **params}
        response = self.get(REST, params=params)
        response.raise_for_status()
        return response


# ---------------------------------------------------------------------------
# Manifest


def load_manifest() -> dict[str, Any]:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {"schema_version": 1, "year_categories": {}, "clubs": {}, "catalogs": {}}


def save_manifest(manifest: dict[str, Any]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, MANIFEST_PATH)


# ---------------------------------------------------------------------------
# Index


def year_category(session: PoliteSession, manifest: dict[str, Any], year: int) -> dict[str, Any] | None:
    key = str(year)
    if key in manifest["year_categories"]:
        return manifest["year_categories"][key]
    data = session.rest("/wp/v2/product_cat", slug=f"show-catalogs-{year}", _fields="id,name,slug,count").json()
    if not data:
        return None
    category = {"id": data[0]["id"], "slug": data[0]["slug"], "count": data[0]["count"]}
    manifest["year_categories"][key] = category
    return category


def paged(session: PoliteSession, route: str, **params: Any) -> Iterable[dict[str, Any]]:
    page = 1
    while True:
        response = session.rest(route, per_page=100, page=page, **params)
        items = response.json()
        yield from items
        total_pages = int(response.headers.get("X-WP-TotalPages", "1") or 1)
        if page >= total_pages or not items:
            return
        page += 1


def resolve_clubs(session: PoliteSession, manifest: dict[str, Any], category_ids: set[int]) -> None:
    known = {int(k) for k in manifest["clubs"]}
    year_ids = {c["id"] for c in manifest["year_categories"].values()}
    missing = sorted(category_ids - known - year_ids)
    for start in range(0, len(missing), 100):
        chunk = missing[start:start + 100]
        items = session.rest(
            "/wp/v2/product_cat",
            include=",".join(map(str, chunk)),
            per_page=100,
            _fields="id,name,slug,parent",
        ).json()
        for item in items:
            manifest["clubs"][str(item["id"])] = {
                "name": html.unescape(item["name"]),
                "slug": item["slug"],
                "parent": item["parent"],
            }


def cmd_index(args: argparse.Namespace) -> None:
    session = PoliteSession(delay=args.delay)
    manifest = load_manifest()
    for year in range(args.year_from, args.year_to + 1):
        category = year_category(session, manifest, year)
        if category is None:
            log(f"{year}: no show-catalog category")
            continue
        log(f"{year}: category {category['id']} ({category['count']} catalogs)")
        seen_cats: set[int] = set()
        added = 0
        for product in paged(session, "/wp/v2/product", product_cat=category["id"], _fields="id,slug,title,link,product_cat"):
            pid = str(product["id"])
            cats = [c for c in product.get("product_cat", []) if c != category["id"]]
            seen_cats.update(cats)
            entry = manifest["catalogs"].setdefault(pid, {"download": {"status": "pending"}})
            if "title" not in entry:
                added += 1
            entry.update(
                {
                    "id": product["id"],
                    "year": year,
                    "title": html.unescape(product["title"]["rendered"]),
                    "slug": product["slug"],
                    "product_url": product["link"],
                    "category_ids": cats,
                }
            )
        resolve_clubs(session, manifest, seen_cats)
        for entry in manifest["catalogs"].values():
            if entry.get("year") == year:
                clubs = [manifest["clubs"][str(c)]["name"] for c in entry.get("category_ids", []) if str(c) in manifest["clubs"]]
                entry["club"] = clubs[0] if clubs else None
        save_manifest(manifest)
        log(f"{year}: {added} new catalogs indexed")


# ---------------------------------------------------------------------------
# Download


def select(manifest: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    club_re = re.compile(args.club, re.I) if getattr(args, "club", None) else None
    ids = set(args.ids.split(",")) if getattr(args, "ids", None) else None
    chosen = []
    for pid, entry in sorted(manifest["catalogs"].items(), key=lambda kv: (kv[1].get("year", 0), kv[1].get("club") or "", kv[0])):
        if ids and pid not in ids:
            continue
        if args.year and entry.get("year") != args.year:
            continue
        if club_re and not club_re.search(entry.get("club") or ""):
            continue
        if getattr(args, "all_clubs", False) is False and club_relevance(entry.get("club")) == "skip":
            continue
        chosen.append(entry)
    return chosen


def resolve_pdf_url(session: PoliteSession, entry: dict[str, Any]) -> str | None:
    response = session.get(entry["product_url"])
    if response.status_code != 200:
        return None
    match = EMBED_PATTERN.search(response.text) or ANY_PDF_PATTERN.search(response.text)
    return html.unescape(match.group(1)) if match else None


def pdf_path(entry: dict[str, Any]) -> Path:
    return PDF_DIR / str(entry["year"]) / f"{entry['id']}.pdf"


def download_one(session: PoliteSession, entry: dict[str, Any]) -> None:
    state = entry.setdefault("download", {"status": "pending"})
    if not entry.get("pdf_url"):
        entry["pdf_url"] = resolve_pdf_url(session, entry)
        if not entry["pdf_url"]:
            state.update(status="no_pdf_url", checked_at=now())
            return
    target = pdf_path(entry)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".pdf.part")
    response = session.get(entry["pdf_url"], stream=True)
    if response.status_code != 200:
        state.update(status=f"http_{response.status_code}", checked_at=now())
        response.close()
        return
    expected = int(response.headers.get("Content-Length") or 0)
    digest = hashlib.sha256()
    size = 0
    with part.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1 << 20):
            handle.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    response.close()
    if expected and size != expected:
        part.unlink(missing_ok=True)
        state.update(status="size_mismatch", expected=expected, received=size, checked_at=now())
        return
    os.replace(part, target)
    state.update(
        status="complete",
        path=str(target.relative_to(HERE)),
        size=size,
        sha256=digest.hexdigest(),
        downloaded_at=now(),
    )


def cmd_download(args: argparse.Namespace) -> None:
    session = PoliteSession(delay=args.delay)
    manifest = load_manifest()
    todo = [e for e in select(manifest, args) if args.force or e.get("download", {}).get("status") != "complete"]
    if args.limit:
        todo = todo[: args.limit]
    log(f"{len(todo)} catalogs to download")
    try:
        for n, entry in enumerate(todo, 1):
            log(f"[{n}/{len(todo)}] {entry['year']} {entry.get('club')} — {entry['title']} ({entry['id']})")
            try:
                download_one(session, entry)
            except Exception as err:  # keep going; record the failure
                entry.setdefault("download", {}).update(status="error", error=str(err), checked_at=now())
            state = entry["download"]
            log(f"    {state['status']} {state.get('size', '')}")
            save_manifest(manifest)
    except KeyboardInterrupt:
        save_manifest(manifest)
        log("interrupted; manifest saved")
        sys.exit(130)


def cmd_status(args: argparse.Namespace) -> None:
    manifest = load_manifest()
    by_year: dict[int, dict[str, int]] = {}
    for entry in manifest["catalogs"].values():
        counts = by_year.setdefault(entry.get("year", 0), {})
        status = entry.get("download", {}).get("status", "pending")
        counts[status] = counts.get(status, 0) + 1
    for year in sorted(by_year):
        log(f"{year}: {by_year[year]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--delay", type=float, default=1.0, help="minimum seconds between requests (default 1)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", help="list catalogs for a year range via the WordPress REST API")
    p.add_argument("--year-from", type=int, required=True)
    p.add_argument("--year-to", type=int, required=True)
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("download", help="download catalog PDFs sequentially")
    p.add_argument("--year", type=int)
    p.add_argument("--club", help="regex matched against the club name")
    p.add_argument("--ids", help="comma-separated product ids")
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true", help="re-download completed files")
    p.add_argument("--all-clubs", action="store_true", help="include specialty clubs for other breeds (skipped by default)")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("status", help="summarize download status by year")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

