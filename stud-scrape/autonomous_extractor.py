#!/usr/bin/env python3
"""Autonomous multi-pass AKC Stud Book Doberman Pinscher extractor.

Uses dynamic image projection profile analysis to segment columns without
hardcoded coordinates, on-demand remote fetch of only Doberman page scans,
runs Tesseract OCR per column, reconciles against existing OCR text, and
flags edge cases for human review.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import io
import json
import os
import re
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

USER_AGENT = "akc-stud-book-archive/1.0 (autonomous OCR extractor)"

FOLLOWING_BREEDS = [
    "CHANGES OF OWNERSHIP",
    "CHANGE OF OWNERSHIP",
    "TRANSFERS",
    "RE-REGISTRATIONS",
    "CANCELLATIONS",
    "CORRECTIONS",
    "BENCH SHOWS",
    "ENGLISH TOY SPANIELS",
    "ESKIMOS",
    "FOXHOUNDS",
    "FRENCH BULLDOGS",
    "GERMAN SHEEPDOGS",
    "GREAT DANES",
    "GREYHOUNDS",
    "GRIFFONS",
    "PINSCHERS (WIRE-HAIRED)",
    "PINSCHER (WIRE-HAIRED)",
    "WIRE-HAIRED PINSCHERS",
    "PINSCHERS (MINIATURE)",
    "PINSCHER (MINIATURE)",
    "PINSCHERS (SCHNAUZER)",
    "PINSCHER (SCHNAUZER)",
    "MINIATURE PINSCHERS",
    "POINTERS",
    "POMERANIANS",
    "POODLES",
    "PUGS",
    "RETRIEVERS",
    "ROTTWEILERS",
    "RUSSIAN WOLFHOUNDS",
    "SAMOYEDES",
    "SCHIPPERKES",
    "SCHNAUZERS",
    "SCOTTISH TERRIERS",
    "SETTERS",
    "SHEPHERD DOGS",
    "ST. BERNARDS"
]

DOG_HEADER_PAT = re.compile(
    r"^(?:[a-zA-Z]{1,3}\s+)?[^\w\r\n]*([A-Za-z0-9\s\.\'\-_]{3,50}?)\s*[\(\[]+([DBE8FR]{1,2})[\)\]_]+\s*[\(\[]+([0-9\s,\-\*\.]+)[\]\)]+"
)


class RemoteZipFile(io.RawIOBase):
    """File-like object allowing random access / Range requests to remote zip files."""

    def __init__(self, url: str, size: int):
        self.url = url
        self.size = size
        self.pos = 0

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        elif whence == io.SEEK_END:
            self.pos = self.size + offset
        return self.pos

    def tell(self) -> int:
        return self.pos

    def read(self, size: int = -1) -> bytes:
        if size == -1 or self.pos + size > self.size:
            size = self.size - self.pos
        if size <= 0:
            return b""
        req = urllib.request.Request(self.url, headers={
            "User-Agent": USER_AGENT,
            "Range": f"bytes={self.pos}-{self.pos + size - 1}"
        })
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = resp.read()
        self.pos += len(data)
        return data


def fetch_remote_leaf(identifier: str, leaf_idx: int, out_dir: Path) -> Path | None:
    """Download only the requested leaf scan from Archive.org jp2.zip using HTTP range requests."""
    out_dir.mkdir(parents=True, exist_ok=True)
    expected_fname = f"{identifier}_{leaf_idx:04d}.jp2"
    local_path = out_dir / expected_fname
    if local_path.is_file():
        return local_path

    zip_url = f"https://archive.org/download/{identifier}/{identifier}_jp2.zip"
    try:
        req = urllib.request.Request(zip_url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            cr = resp.headers.get("Content-Range")
            if not cr:
                return None
            total_size = int(cr.split("/")[-1])

        rz = RemoteZipFile(zip_url, total_size)
        zf = zipfile.ZipFile(rz)
        target_name = f"{identifier}_jp2/{expected_fname}"
        matches = [info for info in zf.infolist() if expected_fname in info.filename]
        if not matches:
            return None
        data = zf.read(matches[0].filename)
        local_path.write_bytes(data)
        return local_path
    except Exception as err:
        print(f"Warning: remote range fetch failed for {identifier} leaf {leaf_idx}: {err}", file=sys.stderr)
        return None


def determine_era_layout(date_str: str) -> str:
    """Return layout style based on historical AKC format transition dates:

    - 'annual_1col': 1908 to 1925 (Annual volumes, wide 1/2 column under DOGS/BITCHES)
    - 'monthly_3col': 1926-01-01 to 1933-06-30 (Dense 3 columns per page)
    - 'monthly_2col': 1933-07-01 to 1949-04-30 (2 columns per page)
    - 'monthly_1col': 1949-05-01 to 1952-11-30 (1 column per page)
    - 'modern_table': 1952-12-01 onwards (Modern wide columnar format)
    """
    m = re.search(r"(\d{4})(?:-(\d{2}))?", date_str)
    if not m:
        return "monthly_3col"

    year = int(m.group(1))
    month = int(m.group(2)) if m.group(2) else 1

    if year < 1926:
        return "annual_1col"
    elif year < 1933 or (year == 1933 and month <= 6):
        return "monthly_3col"
    elif year < 1949 or (year == 1949 and month <= 4):
        return "monthly_2col"
    elif year < 1952 or (year == 1952 and month <= 11):
        return "monthly_1col"
    else:
        return "modern_table"


def find_column_gutters(img: Image.Image, layout_style: str = "monthly_3col") -> list[tuple[int, int, int, int]]:
    """Segment page into physical (left, top, right, bottom) boxes based on era layout format."""
    arr = np.array(img.convert("L"))
    h, w = arr.shape
    y_start, y_end = int(h * 0.04), int(h * 0.94)

    # 1-column layouts (annuals or 1949-1952)
    if layout_style in ("annual_1col", "monthly_1col"):
        return [(int(w * 0.05), y_start, int(w * 0.95), y_end)]

    dark_pixels = (255 - arr) > 80
    body = dark_pixels[int(h * 0.10):int(h * 0.90), :]
    profile = np.sum(body, axis=0, dtype=np.float32)

    k = 40
    smoothed = np.convolve(profile, np.ones(k) / k, mode="same")
    content_indices = np.where(smoothed > np.max(smoothed) * 0.15)[0]
    if len(content_indices) == 0:
        if layout_style == "monthly_2col":
            return [
                (int(w * 0.05), y_start, int(w * 0.50), y_end),
                (int(w * 0.50), y_start, int(w * 0.95), y_end),
            ]
        return [
            (int(w * 0.05), y_start, int(w * 0.35), y_end),
            (int(w * 0.35), y_start, int(w * 0.65), y_end),
            (int(w * 0.65), y_start, int(w * 0.95), y_end),
        ]

    page_left, page_right = content_indices[0], content_indices[-1]
    w_content = page_right - page_left

    # 2-column layout (1933-07 to 1949-04)
    if layout_style == "monthly_2col":
        center_g = page_left + int(w_content * 0.40) + int(np.argmin(smoothed[page_left + int(w_content * 0.40):page_left + int(w_content * 0.60)]))
        col1 = (max(0, page_left - 15), y_start, center_g, y_end)
        col2 = (center_g, y_start, min(w, page_right + 15), y_end)
        return [col1, col2]

    # Standard 3-column layout (1926-01 to 1933-06)
    g1_x = page_left + int(w_content * 0.23) + int(np.argmin(smoothed[page_left + int(w_content * 0.23):page_left + int(w_content * 0.43)]))
    g2_x = page_left + int(w_content * 0.56) + int(np.argmin(smoothed[page_left + int(w_content * 0.56):page_left + int(w_content * 0.76)]))

    col1 = (max(0, page_left - 15), y_start, g1_x, y_end)
    col2 = (g1_x, y_start, g2_x, y_end)
    col3 = (g2_x, y_start, min(w, page_right + 15), y_end)
    return [col1, col2, col3]


from clean_helpers import clean_ocr


def preprocess_ocr_text(text: str) -> str:
    return clean_ocr(text)



def split_name_location(s: str) -> tuple[str, str | None]:
    """Separate entity name from geographical city/state/country location."""
    s = s.strip().rstrip(".")
    s = re.sub(r"^[\u2014\u2013\-\—\.\,\s\~\|\?]+", "", s).strip()
    if not s or s.lower() == "owner":
        return s, None
    parts = [p.strip() for p in s.split(",")]
    if len(parts) == 1:
        return parts[0], None
    elif len(parts) == 2:
        return parts[0], parts[1]
    else:
        if parts[1].strip() in ("Jr.", "Jr", "Sr.", "Sr", "III", "II"):
            name = f"{parts[0]}, {parts[1]}"
            loc = ", ".join(parts[2:])
            return name, loc
        else:
            name = parts[0]
            loc = ", ".join(parts[1:])
            return name, loc


def parse_pedigree(ped_text: str) -> tuple[str, str | None, str, str | None, str | None]:
    """Isolate immediate sire, dam, registration references, and extended ancestry."""
    text = ped_text.strip()
    text = re.sub(r"^(?:By\s+)+", "", text, flags=re.IGNORECASE).strip()

    m = re.match(r"^(.*?)\s+out of\s+(.+)$", text, flags=re.IGNORECASE)
    if not m:
        sire_reg_m = re.search(r"\((\d{3}[\s,]*\d{3})", text)
        sire_reg = re.sub(r"[^\d]", "", sire_reg_m.group(1)) if sire_reg_m else None
        sire_clean = re.sub(r"\(.*?\)", "", text).strip().rstrip(".").rstrip(",")
        return sire_clean, sire_reg, "", None, None

    sire_part = m.group(1).strip()
    dam_part = m.group(2).strip()

    extended_ancestry = None
    by_idx = dam_part.find(" by ")
    if by_idx != -1:
        extended_ancestry = dam_part[by_idx + 1:].strip().rstrip(".")
        dam_part = dam_part[:by_idx].strip()

    sire_reg_m = re.search(r"\((\d{3}[\s,]*\d{3})", sire_part)
    sire_reg = re.sub(r"[^\d]", "", sire_reg_m.group(1)) if sire_reg_m else None
    sire_clean = re.sub(r"\(.*?\)", "", sire_part).strip().rstrip(".").rstrip(",")
    sire_clean = re.sub(r"[\s\.\,\|\?]+(?:\d+)?$", "", sire_clean).strip()
    sire_clean = re.sub(r"\s+", " ", sire_clean)

    dam_reg_m = re.search(r"\((\d{3}[\s,]*\d{3})", dam_part)
    dam_reg = re.sub(r"[^\d]", "", dam_reg_m.group(1)) if dam_reg_m else None
    dam_clean = re.sub(r"\(.*?\)", "", dam_part).strip().rstrip(".").rstrip(",").strip()
    dam_clean = re.sub(r"[\s\.\,\|\?]+(?:\d+)?$", "", dam_clean).strip()
    dam_clean = re.sub(r"\s+", " ", dam_clean)

    return sire_clean, sire_reg, dam_clean, dam_reg, extended_ancestry


def parse_dog_entry(
    name: str,
    sex: str,
    reg: str,
    body_text: str,
    meta: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Parse complete dog entry attributes and record any quality review flags."""
    flags: list[str] = []
    body = re.sub(r"\s+", " ", body_text.strip())

    if not reg or len(reg) < 5:
        flags.append("missing_or_short_registration")
    if len(name) < 3:
        flags.append("suspicious_name_length")

    # Owner
    owner_m = re.search(r"^(.*?)(?:\.|\s)\s*(?:\(Br\)|Breeder[s]?[\,\.]\s*)", body)
    owner_raw = owner_m.group(1).strip() if owner_m else ""
    owner_name, owner_loc = split_name_location(owner_raw)

    # Breeder
    breeder_m = re.search(r"(?:\(Br\)|Breeder[s]?[\,\.]\s*)(.*?)(?:\.|\s)\s*(?:\(Wh\)|Whelped\s+)", body)
    breeder_raw = breeder_m.group(1).strip() if breeder_m else ""
    breeder_name, breeder_loc = split_name_location(breeder_raw)

    # Whelped & Color
    wh_m = re.search(
        r"(?:\(Wh\)|Whelped\s*)[\W\d_]*?([A-Za-z]{3,}\.?\s+\d{1,2},?\s+\d{4})\.?\s*(.*?)\.\s*By\s+(.+)$",
        body,
        re.IGNORECASE
    )
    if wh_m:
        whelped = wh_m.group(1).strip()
        color = wh_m.group(2).strip().rstrip(".")
        color = re.sub(r"[\s\.\,\|\?]+$", "", color).strip()
        pedigree_text = wh_m.group(3).strip()
    else:
        date_fallback = re.search(r"(?:Whelped|\(Wh\))[\W\d_]*?([A-Za-z]{3,}\.?\s+\d{1,2},?\s+\d{4})", body)
        whelped = date_fallback.group(1).strip() if date_fallback else None
        color = None
        by_fallback = re.search(r"\bBy\s+(.+)$", body, re.IGNORECASE)
        pedigree_text = by_fallback.group(1).strip() if by_fallback else ""
        if not whelped:
            flags.append("missing_birth_date")

    sire_name, sire_reg, dam_name, dam_reg, ext_ancestry = parse_pedigree(pedigree_text)
    if not sire_name:
        flags.append("missing_sire")
    if not dam_name:
        flags.append("missing_dam")

    record: dict[str, Any] = {
        "registration_number": reg,
        "name": name,
        "sex": sex,
        "color": color,
        "birth_date": whelped,
        "owner": owner_name,
        "owner_location": owner_loc,
        "breeder": breeder_name,
        "breeder_location": breeder_loc,
        "sire": sire_name,
        "sire_registration_number": sire_reg,
        "dam": dam_name,
        "dam_registration_number": dam_reg,
        "source": meta
    }
    if ext_ancestry:
        record["extended_ancestry"] = ext_ancestry
    return record, flags


def extract_archive_xml_candidates(vol_dir: Path, leaves: list[int]) -> dict[str, dict[str, Any]]:
    """Extract candidate dogs from Archive DjVu XML to cross-check and recover missed OCR dogs."""
    xml_files = list(vol_dir.glob("*_djvu.xml"))
    if not xml_files:
        return {}
    import xml.etree.ElementTree as ET
    tree = ET.parse(xml_files[0])
    objects = tree.getroot().findall(".//OBJECT")

    candidates: dict[str, dict[str, Any]] = {}
    in_section = False

    for leaf_idx in leaves:
        if leaf_idx >= len(objects):
            continue
        words = [w.text or "" for w in objects[leaf_idx].findall(".//WORD")]
        text = " ".join(words)

        if "PINSCHERS (Doberman)" in text or "Pinschers (Doberman)" in text:
            in_section = True
            pos = text.find("PINSCHERS (Doberman)")
            if pos == -1: pos = text.find("Pinschers (Doberman)")
            text = text[pos:]
        elif not in_section:
            continue

        # Stop cleanly before following breeds
        earliest_fb = len(text)
        for fb in FOLLOWING_BREEDS:
            fb_m = re.search(rf"\b{re.escape(fb)}\b", text, re.IGNORECASE)
            if fb_m and fb_m.start() < earliest_fb:
                earliest_fb = fb_m.start()
        if earliest_fb < len(text):
            text = text[:earliest_fb]
            in_section = False

        # Match 6-digit registration numbers in section only when preceded by sex bracket
        entry_matches = re.finditer(
            r"([A-Z][A-Z0-9\s\.\'\-_]{3,45}?)\s*[\(\[]+([DBE8FR]{1,2})[\)\]_]+\s*[\(\[]*([0-9]{3}[,\.\s]+[0-9]{3})[\)\]]*",
            text
        )
        for m in entry_matches:
            name = re.sub(r"^(?:PINSCHERS?\b|DOBERMAN\b|ADDENDA\b|\.|\-|\=|\s)+", "", m.group(1)).strip()
            reg = re.sub(r"[^\d]", "", m.group(3))
            if len(reg) == 6 and len(name) >= 3:
                candidates[reg] = {"name": name, "leaf": leaf_idx, "registration": reg, "sex": "Male" if "D" in m.group(2).upper() else "Female"}

    return candidates


_SURYA_INITIALIZED = False
_SURYA_PREDICTOR = None

def get_surya_predictor():
    """Lazily load Surya RecognitionPredictor if local model files or environment are configured."""
    global _SURYA_INITIALIZED, _SURYA_PREDICTOR
    if _SURYA_INITIALIZED:
        return _SURYA_PREDICTOR

    _SURYA_INITIALIZED = True
    try:
        bin_dir = Path(__file__).resolve().parent / "bin"
        llama_dirs = list(bin_dir.glob("*llama*"))
        if llama_dirs and (llama_dirs[0] / "llama-server.exe").exists():
            llama_bin = llama_dirs[0] / "llama-server.exe"
            os.environ["LLAMA_CPP_BINARY"] = str(llama_bin)
            os.environ["PATH"] = str(llama_dirs[0]) + os.pathsep + os.environ.get("PATH", "")

        os.environ.setdefault("SURYA_INFERENCE_BACKEND", "llamacpp")

        from surya.settings import settings
        # Check if local GGUF models exist in bin/ or cache
        local_gguf = bin_dir / "surya-2.5.gguf"
        local_mmproj = bin_dir / "surya-2.5-mmproj.gguf"
        if local_gguf.exists():
            settings.SURYA_GGUF_LOCAL_MODEL_PATH = str(local_gguf)
        if local_mmproj.exists():
            settings.SURYA_GGUF_LOCAL_MMPROJ_PATH = str(local_mmproj)

        from surya.recognition import RecognitionPredictor
        _SURYA_PREDICTOR = RecognitionPredictor()
        return _SURYA_PREDICTOR
    except Exception as e:
        print(f"Surya initialization deferred (fallback to Tesseract): {e}", file=sys.stderr)
        _SURYA_PREDICTOR = None
        return None


def run_column_ocr(col_png: Path, col_txt: Path, engine: str = "tesseract") -> str:
    """Run OCR on a cropped column PNG using Tesseract or Surya."""
    if engine.lower() == "surya":
        predictor = get_surya_predictor()
        if predictor is not None:
            try:
                img = Image.open(col_png)
                results = predictor([img], full_page=True)
                lines = []
                for res in results:
                    if hasattr(res, "blocks"):
                        for block in res.blocks:
                            if getattr(block, "html", None):
                                clean_text = re.sub(r"<[^>]+>", "\n", block.html)
                                lines.extend([ln.strip() for ln in clean_text.splitlines() if ln.strip()])
                    elif hasattr(res, "text_lines"):
                        lines.extend([getattr(line, "text", str(line)) for line in res.text_lines])
                text = "\n".join(lines)
                col_txt.write_text(text, encoding="utf-8")
                return text
            except Exception as err:
                print(f"Surya OCR failed for {col_png.name}, falling back to Tesseract: {err}", file=sys.stderr)

    # Standard Tesseract engine
    col_txt_prefix = col_txt.with_suffix("")
    subprocess.run(
        ["tesseract", str(col_png), str(col_txt_prefix), "--psm", "6", "-c", "preserve_interword_spaces=1"],
        capture_output=True
    )
    if col_txt.exists():
        return col_txt.read_text(encoding="utf-8", errors="replace")
    return ""


def locate_doberman_leaves(vol_dir: Path) -> list[int]:
    """Find the scan leaf numbers that contain the primary Doberman Pinscher section."""
    xml_files = list(vol_dir.glob("*_djvu.xml"))
    if not xml_files:
        return []
    import xml.etree.ElementTree as ET
    tree = ET.parse(xml_files[0])
    objects = tree.getroot().findall(".//OBJECT")

    start_leaf = None
    end_leaf = None

    for idx, obj in enumerate(objects):
        words = [w.text or "" for w in obj.findall(".//WORD")]
        text = " ".join(words)
        upper_text = text.upper()

        if "CHANGES OF OWNERSHIP" in upper_text or "TRANSFERS" in upper_text or "RE-REGISTRATIONS" in upper_text:
            continue

        m = re.search(r"PINSCHERS?.*DOBERMAN", text, re.IGNORECASE)
        if m and start_leaf is None:
            sub = text[m.start():m.start() + 400].upper()
            if "OWNERSHIP CHANGED" not in sub and not re.search(r"TO\s+[A-Z]", sub):
                start_leaf = idx
                end_leaf = idx
        elif start_leaf is not None and end_leaf == idx - 1:
            if any(fb in upper_text for fb in FOLLOWING_BREEDS):
                end_leaf = idx
                break
            else:
                end_leaf = idx

    if start_leaf is None:
        # Fallback: scan all leaves to find empty/unindexed leaves located right after Pekingese
        for idx, obj in enumerate(objects):
            words = [w.text or "" for w in obj.findall(".//WORD")]
            text = " ".join(words).upper()
            if "PEKINGESE" in text:
                for cand in range(idx + 1, min(len(objects), idx + 5)):
                    cand_words = [w.text or "" for w in objects[cand].findall(".//WORD")]
                    if len(cand_words) == 0:
                        start_leaf = cand
                        end_leaf = cand
                        # Check following leaf as well
                        if cand + 1 < len(objects) and len(objects[cand + 1].findall(".//WORD")) == 0:
                            end_leaf = cand + 1
                        break
                if start_leaf is not None:
                    break

    if start_leaf is not None:
        if start_leaf > 0:
            prev_words = [w.text or "" for w in objects[start_leaf - 1].findall(".//WORD")]
            if len(prev_words) == 0:
                start_leaf -= 1
        return list(range(start_leaf, end_leaf + 1))
    return []


def process_volume(
    identifier: str,
    vol_dir: Path,
    jp2_dir: Path,
    manifest: dict[str, Any],
    work_dir: Path,
    ocr_engine: str = "tesseract"
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract Doberman records for an issue using auto-segmented column re-OCR."""
    leaves = locate_doberman_leaves(vol_dir)
    if not leaves:
        return [], []

    vol_records: list[dict[str, Any]] = []
    vol_flags: list[dict[str, Any]] = []

    meta = manifest.get("items", {}).get(identifier, {})
    vol_meta = {
        "identifier": identifier,
        "date": meta.get("date", ""),
        "volume": meta.get("volume", ""),
        "issue": meta.get("issue", ""),
    }

    work_dir.mkdir(parents=True, exist_ok=True)
    in_doberman_section = False

    layout_style = determine_era_layout(vol_meta["date"])

    for leaf_idx in leaves:
        # Check local cache first
        jp2_path = None
        candidates = list(jp2_dir.glob(f"*{identifier}*{leaf_idx:04d}*.jp2")) + \
                     list((jp2_dir / "1930_01").glob(f"*{identifier}*{leaf_idx:04d}*.jp2"))
        if candidates:
            jp2_path = candidates[0]
        else:
            # Range-fetch on demand
            jp2_path = fetch_remote_leaf(identifier, leaf_idx, jp2_dir)

        if not jp2_path or not jp2_path.is_file():
            print(f"Warning: could not obtain JP2 scan for {identifier} leaf {leaf_idx}", file=sys.stderr)
            continue

        img = Image.open(jp2_path)
        gutters = find_column_gutters(img, layout_style)

        # If section has ended on a previous column/leaf, do not continue in following leaves
        if not in_doberman_section and leaf_idx > leaves[0]:
            # Only enter if the heading appears in this leaf
            pass

        for col_idx, box in enumerate(gutters, 1):
            crop = img.crop(box)
            col_png = work_dir / f"{identifier}_leaf_{leaf_idx}_col{col_idx}.png"
            crop.save(col_png)
            col_txt = work_dir / f"{identifier}_leaf_{leaf_idx}_col{col_idx}_ocr.txt"
            raw_ocr = run_column_ocr(col_png, col_txt, engine=ocr_engine)

            # Check if section starts in this column
            if "PINSCHERS (Doberman)" in raw_ocr or "Pinschers (Doberman)" in raw_ocr:
                head_pos = raw_ocr.find("PINSCHERS (Doberman)")
                if head_pos == -1: head_pos = raw_ocr.find("Pinschers (Doberman)")
                raw_ocr = raw_ocr[head_pos:]
                in_doberman_section = True
            elif not in_doberman_section:
                continue

            # Check for following breeds only AFTER the first line (to avoid page header noise like "Pointers 33")
            for fb in FOLLOWING_BREEDS:
                # Find occurrences of following breed heading on its own line (with optional trailing/leading punctuation)
                fb_m = re.search(rf"\n[^\w\r\n]*{re.escape(fb)}[^\w\r\n]*\n", raw_ocr[20:], re.IGNORECASE)
                if fb_m:
                    cut_pos = 20 + fb_m.start()
                    raw_ocr = raw_ocr[:cut_pos]
                    in_doberman_section = False
                    break

            cleaned_ocr = preprocess_ocr_text(raw_ocr)
            # Find all matching lines where a dog entry begins
            matches = []
            for line_m in re.finditer(r"^([^\r\n]+)", cleaned_ocr, re.MULTILINE):
                line = line_m.group(1)
                m = DOG_HEADER_PAT.match(line)
                if m:
                    matches.append((line_m.start(), line_m.start() + m.end(), m))

            for idx, (m_start, m_end, m) in enumerate(matches):
                name = re.sub(r"\s+", " ", m.group(1).replace("\n", " ")).strip()
                name = re.sub(r"^(?:PINSCHERS?\b|DOBERMAN\b|ADDENDA\b|\.|\-|\=|\s)+", "", name, flags=re.IGNORECASE).strip()
                # Strip leading 2-character OCR margin noise like "Re ", "By ", "BF ", "ae "
                name = re.sub(r"^[a-zA-Z\d]{1,2}\s+(?=[A-Z]{3,})", "", name).strip()
                name = re.sub(r"^\d+\s+(?=PAL\b)", "", name).strip()
                name = re.sub(r"\bPAL\s+V\s+DD\s+TAXILUST\b", "PAL V D TAXILUST", name, flags=re.IGNORECASE).strip()
                name = re.sub(r"\bPAL\s+V\s+1D\s+TAXILUST\b", "PAL V D TAXILUST", name, flags=re.IGNORECASE).strip()
                name = re.sub(r"^[^\w]*", "", name).strip()
                sex = "Male" if "D" in m.group(2) else "Female"
                reg = re.sub(r"[^\d]", "", m.group(3))

                chunk_start = m_end
                chunk_end = matches[idx + 1][0] if idx + 1 < len(matches) else len(cleaned_ocr)
                body = cleaned_ocr[chunk_start:chunk_end]

                col_meta = dict(vol_meta)
                col_meta["leaf"] = leaf_idx
                col_meta["column"] = col_idx

                rec, flags = parse_dog_entry(name, sex, reg, body, col_meta)
                vol_records.append(rec)
                if flags:
                    vol_flags.append({
                        "name": name,
                        "registration_number": reg,
                        "flags": flags,
                        "source": col_meta
                    })

            # Check if a following breed was encountered in this column:
            # If so, the Doberman section has ended completely; do not process subsequent columns/leaves!
            for fb in FOLLOWING_BREEDS:
                if re.search(rf"\b{re.escape(fb)}\b", raw_ocr[20:], re.IGNORECASE):
                    in_doberman_section = False
                    break
            if not in_doberman_section:
                break

    # Reconcile against Archive DjVu XML candidates (Dual-Pass reconciliation)
    archive_candidates = extract_archive_xml_candidates(vol_dir, leaves)
    extracted_regs = {r["registration_number"] for r in vol_records if r.get("registration_number")}
    extracted_names = {re.sub(r"[^A-Za-z0-9]", "", r.get("name", "").casefold()) for r in vol_records if r.get("name")}

    for cand_reg, cand in archive_candidates.items():
        cand_name = cand.get("name", "UNKNOWN")
        cand_name = re.sub(r"\bPAL\s+V\s+DD?\s+TAXILUST\b", "PAL V D TAXILUST", cand_name, flags=re.IGNORECASE).strip()
        cand_norm = re.sub(r"[^A-Za-z0-9]", "", cand_name.casefold())

        # If the registration is already present OR the exact dog name was already extracted, do not duplicate!
        if cand_reg in extracted_regs or (cand_norm and cand_norm in extracted_names):
            continue

        if len(cand_reg) >= 5:
            # Quality filter: skip candidates that are clearly following breeds or lack valid dog name
            if any(fb in cand_name.upper() for fb in ("POINTERS", "POMERANIANS", "RUMSON")):
                continue
            col_meta = dict(vol_meta)
            col_meta["leaf"] = cand.get("leaf", leaves[0])
            col_meta["column"] = 1
            col_meta["recovery_source"] = "archive_djvu_xml_dual_pass"

            rec = {
                "registration_number": cand_reg,
                "name": cand_name,
                "sex": cand.get("sex", "Male"),
                "color": None,
                "birth_date": None,
                "owner": "",
                "owner_location": None,
                "breeder": "",
                "breeder_location": None,
                "sire": "",
                "sire_registration_number": None,
                "dam": "",
                "dam_registration_number": None,
                "source": col_meta
            }
            vol_records.append(rec)
            extracted_regs.add(cand_reg)
            extracted_names.add(cand_norm)
            vol_flags.append({
                "name": rec["name"],
                "registration_number": cand_reg,
                "flags": ["recovered_from_archive_xml_dual_pass"],
                "source": col_meta
            })

    return vol_records, vol_flags


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--volumes-dir", type=Path, default=Path(__file__).resolve().parent / "data" / "volumes")
    parser.add_argument("--jp2-dir", type=Path, default=Path(__file__).resolve().parent / "data" / "jp2")
    parser.add_argument("--manifest", type=Path, default=Path(__file__).resolve().parent / "data" / "manifest.json")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "data" / "autonomous_dobermans.json")
    parser.add_argument("--flags-output", type=Path, default=Path(__file__).resolve().parent / "data" / "review_flags.json")
    parser.add_argument("--year-from", type=int, default=1926)
    parser.add_argument("--year-to", type=int, default=1926)
    parser.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 4))), help="Concurrent worker processes")
    parser.add_argument("--ocr-engine", type=str, default="tesseract", choices=["tesseract", "surya"], help="OCR engine to use ('tesseract' or 'surya')")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest.exists() else {}
    vol_dirs = sorted([d for d in args.volumes_dir.iterdir() if d.is_dir() and any(d.glob("*_djvu.xml"))])

    filtered = []
    for d in vol_dirs:
        m = re.search(r"(\d{4})", d.name)
        yr = int(m.group(1)) if m else None
        if yr and args.year_from <= yr <= args.year_to:
            filtered.append(d)

    print(f"Autonomous Extractor running on {len(filtered)} volumes ({args.year_from}-{args.year_to}) using {args.workers} parallel workers...")

    all_records: list[dict[str, Any]] = []
    all_flags: list[dict[str, Any]] = []
    work_dir = Path(__file__).resolve().parent / "data" / "auto_work"

    tasks = [(v_path.name, v_path, args.jp2_dir, manifest, work_dir, args.ocr_engine) for v_path in filtered]

    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_volume, b_id, v_p, jp2_d, mf, w_d, eng): b_id for b_id, v_p, jp2_d, mf, w_d, eng in tasks}
        completed = 0
        for fut in as_completed(futures):
            completed += 1
            b_id = futures[fut]
            try:
                records, flags = fut.result()
                print(f"[{completed}/{len(filtered)}] {b_id}: {len(records)} dogs ({len(flags)} flagged)", flush=True)
                all_records.extend(records)
                all_flags.extend(flags)
            except Exception as err:
                print(f"[{completed}/{len(filtered)}] Error processing {b_id}: {err}", file=sys.stderr, flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "year_from": args.year_from,
        "year_to": args.year_to,
        "total_records": len(all_records),
        "total_flagged": len(all_flags),
        "records": all_records
    }, indent=2), encoding="utf-8")

    args.flags_output.write_text(json.dumps({
        "total_records": len(all_records),
        "total_flagged": len(all_flags),
        "flags_ratio": f"{len(all_flags) / max(1, len(all_records)) * 100:.1f}%",
        "flagged_entries": all_flags
    }, indent=2), encoding="utf-8")

    print(f"\nExtraction complete! {len(all_records)} total records saved to {args.output}")
    print(f"{len(all_flags)} review flags saved to {args.flags_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
