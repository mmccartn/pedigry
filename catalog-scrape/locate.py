#!/usr/bin/env python3
"""Find the Doberman pages in downloaded catalogs and extract their scans.

For every downloaded PDF this scores each page using the PDF's embedded text
layer (only as a cheap *locator*; transcription is done later by Surya):

* ``dobe``  – mentions of "Doberman" (tolerant of OCR spacing)
* ``entry`` – entry-like signals: dates, "By <sire>—<dam>", registration
  numbers, "Listed"

A page is a candidate when it mentions Dobermans *and* looks like entries
(this rejects exhibitor indexes, judging programs and prize lists). The page
after each run of candidates is added too, because sections continue across
pages; the parser stops at the next breed header.

Each candidate page's embedded scan is saved at native resolution to
``data/pages/<year>/<catalog id>/p<NNN>.png`` and the summary goes to
``data/pages/<year>/<catalog id>/locate.json``.

    python locate.py --year 1934
    python locate.py --ids 41990,131949
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pypdfium2 as pdfium

from akc_library import HERE, load_manifest
from breeds import club_relevance

PAGES = HERE / "data" / "pages"

DOBE_RE = re.compile(r"d\s?o\s?b\s?e\s?r\s?m\s?a\s?n", re.I)
DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|june?|july?|aug|sept?|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+(18|19|20)\d\d\b",
    re.I,
)
BY_RE = re.compile(r"\bBy\s+[A-Z]")
REG_RE = re.compile(r"\b(?:[A-Z]{1,2}[- ]?\d{3}[,.]\d{3}|\d{6,7}|[A-Z]\d{5,6})\b")
LISTED_RE = re.compile(r"\blisted\b", re.I)

ENTRY_MIN = 4


def page_scores(text: str) -> dict[str, int]:
    return {
        "chars": len(text.strip()),
        "dobe": len(DOBE_RE.findall(text)),
        "entry": len(DATE_RE.findall(text)) + len(BY_RE.findall(text)) + len(REG_RE.findall(text)) + len(LISTED_RE.findall(text)),
    }


def choose_pages(scores: list[dict[str, int]], specialty: bool = False) -> list[int]:
    """Return 0-based page indexes to OCR.

    In a Doberman specialty catalog the breed name is often never printed on
    the entry pages, so every entry-like page is a candidate.
    """
    n = len(scores)
    core = [i for i, s in enumerate(scores) if (specialty or s["dobe"]) and s["entry"] >= ENTRY_MIN]
    chosen = set(core)
    for i in core:
        # continuation page(s): the next page if it still looks like entries
        j = i + 1
        if j < n and (scores[j]["entry"] >= ENTRY_MIN or scores[j]["chars"] < 50):
            chosen.add(j)
    # bridge single gaps (a page whose text layer missed the breed name)
    for i in range(1, n - 1):
        if i - 1 in chosen and i + 1 in chosen:
            chosen.add(i)
    return sorted(chosen)


def save_scan(page: pdfium.PdfPage, target: Path) -> tuple[int, int]:
    images = [o for o in page.get_objects() if o.type == 3]  # FPDF_PAGEOBJ_IMAGE
    if images:
        biggest = max(images, key=lambda o: o.get_px_size()[0] * o.get_px_size()[1])
        try:
            pil = biggest.get_bitmap(render=False).to_pil()
        except Exception:
            pil = biggest.get_bitmap(render=True).to_pil()
    else:
        pil = page.render(scale=300 / 72).to_pil()
    pil = pil.convert("L")
    pil.save(target, optimize=True)
    return pil.size


def locate(entry: dict, force: bool = False) -> dict | None:
    pdf_rel = entry.get("download", {}).get("path")
    if not pdf_rel:
        return None
    out_dir = PAGES / str(entry["year"]) / str(entry["id"])
    summary_path = out_dir / "locate.json"
    if summary_path.exists() and not force:
        return json.loads(summary_path.read_text(encoding="utf-8"))
    doc = pdfium.PdfDocument(str(HERE / pdf_rel))
    scores = [page_scores(doc[i].get_textpage().get_text_bounded()) for i in range(len(doc))]
    specialty = club_relevance(entry.get("club")) == "target"
    chosen = choose_pages(scores, specialty=specialty)
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for i in chosen:
        target = out_dir / f"p{i + 1:03d}.png"
        size = save_scan(doc[i], target) if not target.exists() or force else None
        pages.append({"page": i + 1, "image": target.name, "size": size, **scores[i]})
    no_text = sum(1 for s in scores if s["chars"] < 50)
    summary = {
        "id": entry["id"],
        "year": entry["year"],
        "club": entry.get("club"),
        "title": entry.get("title"),
        "specialty": specialty,
        "page_count": len(scores),
        "pages_without_text": no_text,
        "needs_full_ocr": no_text > 0.8 * len(scores),
        "candidate_pages": pages,
    }
    summary_path.write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--year", type=int)
    parser.add_argument("--ids")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    manifest = load_manifest()
    ids = set(args.ids.split(",")) if args.ids else None
    for pid, entry in sorted(manifest["catalogs"].items()):
        if ids and pid not in ids:
            continue
        if args.year and entry.get("year") != args.year:
            continue
        if entry.get("download", {}).get("status") != "complete":
            continue
        try:
            summary = locate(entry, force=args.force)
        except Exception as err:
            print(f"{pid}: locate failed: {err}", file=sys.stderr)
            continue
        if summary:
            pages = [p["page"] for p in summary["candidate_pages"]]
            flag = " NEEDS_FULL_OCR" if summary["needs_full_ocr"] else ""
            print(f"{entry['year']} {pid} {entry.get('club')}: {len(pages)} pages {pages}{flag}", flush=True)


if __name__ == "__main__":
    main()
