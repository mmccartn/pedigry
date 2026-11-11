#!/usr/bin/env python3
"""Parse Surya OCR output of catalog pages into Doberman entry "sightings".

The parser is deliberately format-tolerant. Catalog layouts differ by era,
superintendent and club, so instead of one positional template it:

1. walks Surya's layout blocks in reading order, tracking the current breed
   section and class (sex) from section headers;
2. splits entries on leading catalog numbers (stripping steward award marks
   such as "Absent", "First", "V. H. C." and tick marks);
3. pulls fields out *by type*: registration number / "Listed", whelping date,
   "By <sire>—<dam>" (or "ex" / "out of"), "Breeder(s), ...", and uses Surya's
   bold markup plus position to tell the dog's name from the owner.

Known layouts handled so far:

* 1930s owner-first:  ``1142. Owner. **Dog Name.** 906922. By Sire—Dam. May 29, 1933. Breeder, X.``
* 1930s caps / "ex":  ``2. Owner. DOG NAME. 940157. By Sire ex Dam. Feb 18, 1934. Breeder, X. In Puppy Dogs.``
* 1970s+ name-first:  ``55 DOG NAME, WE-214,765. Dec. 8, 1978. Breeder, X. By Sire — Dam. **Owner**, address.``

Output: ``data/sightings/<year>/<catalog id>.jsonl`` (one JSON object per entry)

    python parse.py --year 1934
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from akc_library import HERE, load_manifest
from breeds import TARGET_BREED_PATTERN, _OTHER_RE, club_relevance

sys.path.insert(0, str(HERE.parent / "akc-scrape"))
try:
    from akc_titles import SUFFIX_TITLES
except Exception:  # pragma: no cover
    SUFFIX_TITLES = frozenset({"cd", "cdx", "ud", "udx", "td", "tdx"})

OCR = HERE / "data" / "ocr"
SIGHTINGS = HERE / "data" / "sightings"

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
DATE_RE = re.compile(
    r"\b(?P<mon>jan|feb|mar|apr|may|june?|july?|aug|sept?|oct|nov|dec)[a-z]*\.?\s*(?P<day>\d{1,2})[,.]?\s*(?P<year>(?:18|19|20)\d\d)\b",
    re.I,
)
REG_RE = re.compile(
    r"(?<![\w$])(?P<reg>"
    r"ind(?:efinitely)?\.?\s*listed|listed|i\.?\s?l\.?\s?p\.?(?:\s*\d+)?|reg(?:istration)?\.?\s*pending|"
    r"[A-Z]{1,3}[- ]?\d{3}[,.]\d{3}|\d{3}[,.]\d{3}|[A-Z]{1,3}\d{5,8}|\d{5,7}"
    r")(?![\w])",
    re.I,
)
AWARD_WORDS = r"(?:absent|abs\.?|first|second|third|fourth|1st|2nd|3rd|4th|reserve|res\.?|v\.?\s?h\.?\s?c\.?|h\.?\s?c\.?|c\.|winners?|excused|disq\w*|withdrawn|w\.|r\.|b\.?\s?o\.?\s?b\.?|b\.?\s?o\.?\s?w\.?)"
ENTRY_START_RE = re.compile(
    r"^(?P<prefix>(?:" + AWARD_WORDS + r"|[^\w\s]|\s)*?)\s*(?P<num>\d{1,4})\s?[.,:]?\s+(?P<rest>[\"'“‘(]?[A-Z].*)$",
    re.S,
)
SEX_DOG_RE = re.compile(r"\bdogs?\b", re.I)
SEX_BITCH_RE = re.compile(r"\bbitch(?:es)?\b", re.I)
PARENT_SPLIT_RE = re.compile(r"\s*[—–]+\s*|\s+-{1,2}\s+|-{2,}|\s+ex\.?\s+|\s+out\s+of\s+", re.I)
BREEDER_RE = re.compile(r"\bBreeders?[,.:;]?\s+(?P<breeder>.+?)(?=\.\s+By\s|\.\s*$|$)", re.S)
CLASS_TAIL_RE = re.compile(r"\.?\s*In\s+(?P<cls>[A-Z][^.]*(?:\([^)]*\))?[^.]*)\.?\s*$")
TRAILING_JUNK_RE = re.compile(r"\.?\s*(?:For sale|Price\s*\$?[\d,]+|Not for sale)\.?\s*$", re.I)
SPECIALS_SEX_RE = re.compile(r"\.\s*(?P<sex>Dog|Bitch)\.?\s*$")
TITLE_PREFIX_RE = re.compile(r"^(?:(?:Int\.?\s*)?Champion|Ch\.?|CH\.?|Am\.?\s*Ch\.?|Can\.?\s*Ch\.?|Grand\s+Champion)\s+", re.I)
ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "jr", "sr", "st", "ch", "wm", "chas", "jas", "geo", "thos", "jos", "capt",
    "col", "rev", "hon", "ind", "co", "no", "mt", "ft", "vd", "vom", "von", "inc", "bros", "ave", "esq",
    "lt", "maj", "gen", "prof", "messrs", "mme", "mlle",
}


def strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Block -> line units


def block_lines(block: dict[str, Any]) -> list[str]:
    """Split one Surya block's html into logical lines (keeping <b> tags)."""
    h = block.get("html") or ""
    if "<tr" in h:
        lines = []
        for row in re.findall(r"<tr>(.*?)</tr>", h, re.S):
            cells = [c.strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
            lines.append(" ".join(c for c in cells if c))
        return lines
    h = re.sub(r"</?(p|div|h\d|li|ul|ol|span)[^>]*>", "\n", h)
    h = re.sub(r"<br\s*/?>", "\n", h)
    keep = re.sub(r"<(?!/?b>)[^>]+>", "", h)  # keep only <b>/</b>
    return [squash(line) for line in keep.split("\n") if squash(strip_tags(line))]


def page_units(page: dict[str, Any], page_no: int) -> list[dict[str, Any]]:
    height = (page.get("size") or [0, 0])[1] or 1
    units = []
    for block in page["blocks"]:
        label = block.get("label") or ""
        if label in ("PageFooter", "Picture", "Figure"):
            continue
        top = (block.get("bbox") or [0, 0])[1] / height
        is_header = label in ("SectionHeader", "PageHeader") or re.match(r"^\s*<h\d", block.get("html") or "")
        for line in block_lines(block):
            kind = "text"
            if is_header:
                kind = "running" if (label == "PageHeader" or top < 0.13) else "header"
            units.append({"kind": kind, "html": line, "text": squash(strip_tags(line)), "page": page_no})
    return units


def header_like(text: str) -> bool:
    """A text line that is really a class/breed header (no catalog number)."""
    if ENTRY_START_RE.match(text):
        return False
    letters = re.sub(r"[^A-Za-z]", "", text)
    if TARGET_BREED_PATTERN.search(text) and len(text) < 120:
        return True
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7 and len(text) < 90 and _OTHER_RE.search(text):
        return True
    return bool(re.match(r"^\s*(puppy|novice|american[- ]bred|limit|open|winners|bred[- ]by|specials?|best of|junior|senior|veterans?)\b.*\b(dogs?|bitch(es)?|class)\b", text, re.I)) and len(text) < 90


# ---------------------------------------------------------------------------
# Entry field extraction


def iso_date(match: re.Match | None) -> str | None:
    if not match:
        return None
    try:
        return date(int(match.group("year")), MONTHS[match.group("mon")[:3].lower()], int(match.group("day"))).isoformat()
    except (ValueError, KeyError):
        return None


def clean_reg(raw: str | None) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    low = raw.lower()
    if "listed" in low:
        return None, "listed"
    if "pending" in low:
        return None, "pending"
    if re.match(r"i\.?\s?l\.?\s?p", low):
        number = re.sub(r"\D", "", raw)
        return (f"ILP{number}" if number else None), "ilp"
    return re.sub(r"[^A-Za-z0-9]", "", raw).upper(), "registered"


def split_titles(name: str) -> tuple[str, list[str]]:
    titles = []
    name = squash(name).strip(" .,;:")
    while True:
        m = TITLE_PREFIX_RE.match(name)
        if not m:
            break
        titles.append("CH")
        name = name[m.end():]
    # trailing suffix titles such as ", CD" / "C.D." / "UD"
    while True:
        m = re.search(r"[,\s]+([A-Za-z.]{2,8})\.?$", name)
        if not m:
            break
        token = m.group(1).replace(".", "").casefold()
        if token in SUFFIX_TITLES and token not in {"v", "d"} and len(token) >= 2 and m.group(1).upper() == m.group(1):
            titles.append(token.upper())
            name = name[: m.start()].strip(" .,;:")
        else:
            break
    return name.strip(" .,;:\"'“”‘’"), titles


def sentence_boundaries(text: str) -> list[int]:
    """Indexes just after '. ' that look like real sentence ends (not initials/abbrev)."""
    cuts = []
    for m in re.finditer(r"\.\s+(?=[\"'“‘(]?[A-Z0-9])", text):
        before = re.search(r"([A-Za-z]+)$", text[: m.start()])
        word = before.group(1) if before else ""
        if len(word) <= 1 or word.lower() in ABBREVIATIONS:
            continue
        cuts.append(m.end())
    return cuts


def split_parents(text: str) -> tuple[str | None, str | None, list[str]]:
    flags = []
    parts = [p for p in PARENT_SPLIT_RE.split(text) if p.strip()]
    if len(parts) < 2:
        return squash(text) or None, None, ["no_parent_separator"]
    if len(parts) > 2:
        flags.append("ambiguous_parent_split")
        # join so the split falls nearest the middle by character length
        best = min(range(1, len(parts)), key=lambda k: abs(len(" ".join(parts[:k])) - len(" ".join(parts[k:]))))
        sep = text[len(" ".join(parts[:best])):]  # informative only
        del sep
        return "-".join(parts[:best]), "-".join(parts[best:]), flags
    return parts[0], parts[1], flags


def parse_entry(cat_no: str, entry_html: str, ctx: dict[str, Any]) -> dict[str, Any]:
    text = squash(strip_tags(entry_html))
    flags: list[str] = []
    sex = ctx.get("sex")
    sex_source = "class_header" if sex else None
    cls = ctx.get("class")

    m = CLASS_TAIL_RE.search(text)
    if m and re.search(r"\b(dogs?|bitch(es)?|class|puppy|open|limit|novice|bred)\b", m.group("cls"), re.I):
        cls = squash(m.group("cls"))
        if SEX_BITCH_RE.search(cls):
            sex, sex_source = "Female", "entry_class"
        elif SEX_DOG_RE.search(cls):
            sex, sex_source = "Male", "entry_class"
        text = text[: m.start()].rstrip() + "."
    for _ in range(2):
        text = TRAILING_JUNK_RE.sub(".", text).strip()
        m = SPECIALS_SEX_RE.search(text)
        if m:
            sex, sex_source = ("Male" if m.group("sex") == "Dog" else "Female"), "entry_suffix"
            text = text[: m.start()] + "."

    by = re.search(r"\b[Bb]y\s+(?=\S)", text)
    reg_m = None
    for cand in REG_RE.finditer(text):
        if by and cand.start() > by.start():
            break
        if DATE_RE.search(text[max(0, cand.start() - 12): cand.end() + 2]) and re.fullmatch(r"\d{4}", cand.group("reg")):
            continue
        reg_m = cand
        break
    if reg_m is None and not by:
        reg_m = REG_RE.search(text)
    registration_number, reg_status = clean_reg(reg_m.group("reg") if reg_m else None)
    if not reg_m:
        flags.append("no_registration")

    date_m = None
    for dm in DATE_RE.finditer(text):
        date_m = dm
        break

    # --- name and owner (pre-registration text)
    pre = text[: reg_m.start()] if reg_m else (text[: by.start()] if by else "")
    pre = pre.rstrip(" ,.;:")
    owner = None
    name = None
    bolds = [squash(strip_tags(b)) for b in re.findall(r"<b>(.*?)</b>", entry_html, re.S)]
    bold_name = next((b for b in bolds if b and b.strip(" .,") and pre.endswith(b.strip(" .,"))), None)
    if bold_name:
        name = bold_name
        owner = pre[: len(pre) - len(bold_name.strip(" .,"))].strip(" .,;:") or None
    else:
        caps = re.search(r"((?:(?:CH\.|Champion|Ch\.)\s+)?[A-Z0-9'’&.\- ]*[A-Z]{2}[A-Z0-9'’&.\- ]*)$", pre)
        if caps and len(caps.group(1).strip()) >= 4 and caps.start() > 0 and sum(c.islower() for c in caps.group(1)) == 0:
            name = caps.group(1)
            owner = pre[: caps.start()].strip(" .,;:") or None
        else:
            cuts = sentence_boundaries(pre + " ")
            if cuts:
                name = pre[cuts[-1]:]
                owner = pre[: cuts[-1]].strip(" .,;:") or None
            else:
                name = pre
    name, titles = split_titles(name or "")

    # --- parents
    sire = dam = None
    parents_end = len(text)
    owner_tail = None
    if by:
        start = by.end()
        stops = [len(text)]
        if date_m and date_m.start() > start:
            stops.append(date_m.start())
        bm = re.search(r"\.\s+Breeders?\b", text[start:])
        if bm:
            stops.append(start + bm.start())
        post_bold = [b for b in bolds if b and text.find(b.strip(" .,"), start) > start]
        if post_bold and not owner:
            stops.append(text.find(post_bold[0].strip(" .,"), start))
            owner_tail = text[text.find(post_bold[0].strip(" .,"), start):]
        parents_end = min(stops)
        parent_text = text[start:parents_end].strip(" .,;:")
        if parents_end == len(text) and not owner:
            # name-first layout without bold: "By S — D. Owner, address."
            cuts = sentence_boundaries(parent_text + " ")
            dash = PARENT_SPLIT_RE.search(parent_text)
            cuts = [c for c in cuts if dash and c > dash.end()]
            if cuts:
                owner_tail = parent_text[cuts[0]:]
                parent_text = parent_text[: cuts[0]].strip(" .,;:")
        s, d, pflags = split_parents(parent_text)
        flags += pflags
        if s:
            sire, _ = split_titles(s)
        if d:
            dam, _ = split_titles(d)
    else:
        flags.append("no_parents")

    # --- breeder
    breeder = None
    bm = BREEDER_RE.search(text)
    if bm:
        breeder = squash(bm.group("breeder")).strip(" .,;:")
        if date_m and bm.start() < date_m.start() < bm.end():
            breeder = squash(text[bm.start("breeder"): date_m.start()]).strip(" .,;:")
        breeder = re.split(r"\.\s+By\s", breeder)[0]
        if re.fullmatch(r"owners?", breeder, re.I):
            breeder = "Owner"

    # --- owner / address for name-first layouts
    owner_location = None
    agent = None
    if owner_tail and not owner:
        tail = owner_tail.strip(" .")
        am = re.search(r"\.\s+([^.]*?),?\s+Agent\.?$", tail)
        if am:
            agent = am.group(1).strip()
            tail = tail[: am.start()]
        parts = [p.strip() for p in tail.split(",")]
        owner = parts[0] if parts else None
        if len(parts) > 1:
            loc = [p for p in parts[1:] if not re.match(r"^\d|^P\.?\s?O\.?\s*Box", p, re.I)]
            owner_location = ", ".join(loc).strip(" .") or None

    if not date_m:
        flags.append("no_birth_date")
    if not name:
        flags.append("no_name")
    if sex is None:
        flags.append("no_sex")

    return {
        "catalog_number": cat_no,
        "name": name.upper() if name else None,
        "name_as_printed": name or None,
        "titles": titles,
        "registration_number": registration_number,
        "registration_status": reg_status,
        "sex": sex,
        "sex_source": sex_source,
        "birth_date": date_m.group(0) if date_m else None,
        "date_of_birth": iso_date(date_m),
        "sire": sire,
        "dam": dam,
        "breeder": breeder,
        "owner": owner,
        "owner_location": owner_location,
        "agent": agent,
        "class": cls,
        "flags": flags,
        "raw": text,
    }


# ---------------------------------------------------------------------------
# Catalog walk


def apply_header(text: str, state: dict[str, Any]) -> None:
    dobe = bool(TARGET_BREED_PATTERN.search(text))
    other = bool(_OTHER_RE.search(text)) and not dobe
    if dobe:
        state["in_breed"] = True
    elif other:
        state["in_breed"] = False
    if SEX_BITCH_RE.search(text):
        state["sex"] = "Female"
    elif SEX_DOG_RE.search(text) and not re.search(r"working dogs|sporting dogs|toy dogs|non-sporting dogs|hound dogs", text, re.I):
        state["sex"] = "Male"
    elif re.search(r"special|best of|brace|team|stud dog|brood", text, re.I):
        state["sex"] = None
    cls = re.sub(r"(?i)^.*?doberman\)?\s*(pinschers?)?[.,]?\s*", "", text) if dobe else text
    state["class"] = squash(cls).strip(" .") or state.get("class")


def parse_catalog(year: str, cat_id: str, meta: dict[str, Any]) -> list[dict[str, Any]]:
    pages = sorted((OCR / year / cat_id).glob("p*.json"))
    specialty = club_relevance(meta.get("club")) == "target"
    state: dict[str, Any] = {"in_breed": True if specialty else None, "sex": None, "class": None}
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def close() -> None:
        nonlocal current
        if current is None:
            return
        rec = parse_entry(current["num"], current["html"], current["ctx"])
        signals = sum([bool(rec["registration_status"]), bool(rec["date_of_birth"]), bool(rec["sire"]), bool(rec["breeder"])])
        if signals >= 2 and not re.search(r"\boffers?\b", rec["raw"], re.I):
            rec.update(page=current["page"], pages=sorted(current["pages"]))
            entries.append(rec)
        current = None

    for page_path in pages:
        page_no = int(page_path.stem[1:])
        page = json.loads(page_path.read_text(encoding="utf-8"))
        for unit in page_units(page, page_no):
            text = unit["text"]
            if unit["kind"] == "running":
                if state["in_breed"] is None and TARGET_BREED_PATTERN.search(text) and not _OTHER_RE.search(text):
                    state["in_breed"] = True
                continue
            if unit["kind"] == "header" or header_like(text):
                close()
                apply_header(text, state)
                continue
            m = ENTRY_START_RE.match(text)
            prefix_ok = m and not re.search(r"\d", m.group("prefix")) and len(m.group("prefix")) <= 30
            if m and prefix_ok:
                close()
                if state["in_breed"]:
                    # drop the award prefix from the html as well
                    num = m.group("num")
                    idx = unit["html"].find(num)
                    body = unit["html"][idx + len(num):].lstrip(" .,:")
                    current = {"num": num, "html": body, "page": page_no, "pages": {page_no}, "ctx": dict(state)}
                continue
            if current is not None and not re.search(r"\bprize\b|\boffers?\b|^judge", text, re.I):
                current["html"] += " " + unit["html"]
                current["pages"].add(page_no)
        # entries may continue on the next page; keep `current` open
    close()
    for rec in entries:
        rec.update(
            catalog_id=int(cat_id),
            year=int(year),
            club=meta.get("club"),
            catalog_title=meta.get("title"),
        )
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--year", type=int)
    parser.add_argument("--ids")
    args = parser.parse_args()
    manifest = load_manifest()
    ids = set(args.ids.split(",")) if args.ids else None
    for year_dir in sorted(OCR.glob("*")):
        if args.year and year_dir.name != str(args.year):
            continue
        for cat_dir in sorted(year_dir.glob("*")):
            if ids and cat_dir.name not in ids:
                continue
            meta = manifest["catalogs"].get(cat_dir.name, {})
            entries = parse_catalog(year_dir.name, cat_dir.name, meta)
            out = SIGHTINGS / year_dir.name / f"{cat_dir.name}.jsonl"
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("w", encoding="utf-8") as handle:
                for rec in entries:
                    handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
            flagged = sum(1 for e in entries if e["flags"])
            print(f"{year_dir.name} {cat_dir.name} {meta.get('club')}: {len(entries)} entries ({flagged} flagged)")


if __name__ == "__main__":
    main()

