#!/usr/bin/env python3
"""Review GUI for Flagged AKC Stud Book Doberman Registrations.

A focused, keyboard-first web interface to inspect, verify, and correct
flagged dog entries side-by-side with physical scan snippets.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
WORK_DIR = DATA_DIR / "auto_work"


def find_target_y_from_ocr_text(col_ocr_txt: Path, img_h: int, dog_name: str, reg_num: str) -> int | None:
    """Pure-Python fallback: estimate vertical pixel position from existing OCR text lines."""
    if not col_ocr_txt.is_file():
        return None
    lines = col_ocr_txt.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return None

    first_words = [w for w in re.split(r"[^\w]", dog_name) if len(w) >= 3]
    first_word = first_words[0].upper() if first_words else ""
    second_word = first_words[1].upper() if len(first_words) > 1 else ""

    target_line = None
    for idx, line in enumerate(lines):
        upper_line = line.upper()
        if first_word and first_word in upper_line:
            if not second_word or second_word in upper_line:
                target_line = idx
                break

    if target_line is None and reg_num and len(reg_num) >= 5:
        for idx, line in enumerate(lines):
            if reg_num in line or f"{reg_num[:3]},{reg_num[3:]}" in line:
                target_line = idx
                break

    if target_line is not None:
        # Map line index fraction to image pixel height
        ratio = target_line / max(1, len(lines))
        return int(ratio * img_h)
    return None


def crop_dog_snippet(ident: str, leaf: int, col: int, dog_name: str, reg_num: str) -> Path | None:
    """Find dog entry in column scan image and return path to cropped image snippet."""
    col_png = WORK_DIR / f"{ident}_leaf_{leaf}_col{col}.png"
    if not col_png.is_file():
        return None

    snippet_dir = WORK_DIR / "snippets"
    snippet_dir.mkdir(parents=True, exist_ok=True)
    clean_name = re.sub(r"[^\w]", "_", dog_name)
    snippet_path = snippet_dir / f"{ident}_{leaf}_{col}_{clean_name}.png"
    if snippet_path.is_file():
        return snippet_path

    img = Image.open(col_png)
    w, h = img.size

    # Try TSV bounding boxes first if pre-computed
    tsv_path = col_png.with_suffix(".tsv")
    target_y = None

    if tsv_path.is_file():
        with tsv_path.open(encoding="utf-8", errors="replace") as f:
            boxes = list(csv.DictReader(f, delimiter="\t"))
        first_words = [w for w in re.split(r"[^\w]", dog_name) if len(w) >= 3]
        first_word = first_words[0].upper() if first_words else ""
        second_word = first_words[1].upper() if len(first_words) > 1 else ""

        for idx, r in enumerate(boxes):
            text = r.get("text", "").upper()
            if first_word and first_word in text:
                if second_word:
                    next_texts = [boxes[j].get("text", "").upper() for j in range(idx, min(len(boxes), idx + 8))]
                    if any(second_word in nt for nt in next_texts):
                        target_y = int(r.get("top", 0))
                        break
                else:
                    target_y = int(r.get("top", 0))
                    break

        if target_y is None and reg_num and len(reg_num) >= 5:
            for r in boxes:
                if reg_num in r.get("text", ""):
                    target_y = int(r.get("top", 0))
                    break

    # Pure-Python fallback using already extracted OCR text file (ZERO external subprocess calls)
    if target_y is None:
        col_ocr_txt = WORK_DIR / f"{ident}_leaf_{leaf}_col{col}_ocr.txt"
        target_y = find_target_y_from_ocr_text(col_ocr_txt, h, dog_name, reg_num)

    if target_y is None:
        return None

    y1 = max(0, target_y - 45)
    y2 = min(h, target_y + 480)
    snippet = img.crop((0, y1, w, y2))
    snippet.save(snippet_path)
    return snippet_path


class ReviewHandler(SimpleHTTPRequestHandler):
    """HTTP request handler for the Review GUI."""

    records_file: Path
    flags_file: Path
    records_data: dict
    flags_data: dict
    default_filter: str = "flagged"

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = HTML_TEMPLATE.replace('let currentFilter = \'flagged\';', f'let currentFilter = \'{self.default_filter}\';')
            self.wfile.write(html.encode("utf-8"))
            return

        if path == "/api/items":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

            qs = parse_qs(parsed.query)
            filter_mode = qs.get("filter", ["flagged"])[0]  # "flagged" or "all"

            flagged = self.flags_data.get("flagged_entries", [])
            flags_map = {}
            for f_idx, f in enumerate(flagged):
                src = f.get("source", {})
                key = (src.get("identifier"), src.get("leaf"), src.get("column"), f.get("name"))
                flags_map[key] = (f_idx, f)

            merged = []
            records = self.records_data.get("records", [])

            if filter_mode == "all":
                # Return every single dog in the dataset
                for r_idx, rec in enumerate(records):
                    src = rec.get("source", {})
                    key = (src.get("identifier"), src.get("leaf"), src.get("column"), rec.get("name"))
                    f_info = flags_map.get(key)
                    f_idx = f_info[0] if f_info else None
                    f_entry = f_info[1] if f_info else {}

                    merged.append({
                        "flag_index": f_idx,
                        "record_index": r_idx,
                        "resolved": f_entry.get("resolved", False),
                        "flags": f_entry.get("flags", []),
                        "name": rec.get("name", ""),
                        "registration_number": rec.get("registration_number", ""),
                        "sex": rec.get("sex", "Male"),
                        "color": rec.get("color") or "",
                        "birth_date": rec.get("birth_date") or "",
                        "owner": rec.get("owner") or "",
                        "owner_location": rec.get("owner_location") or "",
                        "breeder": rec.get("breeder") or "",
                        "breeder_location": rec.get("breeder_location") or "",
                        "sire": rec.get("sire") or "",
                        "sire_registration_number": rec.get("sire_registration_number") or "",
                        "dam": rec.get("dam") or "",
                        "dam_registration_number": rec.get("dam_registration_number") or "",
                        "extended_ancestry": rec.get("extended_ancestry") or "",
                        "source": src,
                    })
            else:
                # Flagged only
                records_map = {}
                for idx, r in enumerate(records):
                    src = r.get("source", {})
                    key = (src.get("identifier"), src.get("leaf"), src.get("column"), r.get("name"))
                    records_map[key] = (idx, r)

                for f_idx, f in enumerate(flagged):
                    src = f.get("source", {})
                    key = (src.get("identifier"), src.get("leaf"), src.get("column"), f.get("name"))
                    rec_info = records_map.get(key)
                    rec = rec_info[1] if rec_info else {}
                    r_idx = rec_info[0] if rec_info else None

                    merged.append({
                        "flag_index": f_idx,
                        "record_index": r_idx,
                        "resolved": f.get("resolved", False),
                        "flags": f.get("flags", []),
                        "name": rec.get("name") or f.get("name", ""),
                        "registration_number": rec.get("registration_number") or f.get("registration_number", ""),
                        "sex": rec.get("sex", "Male"),
                        "color": rec.get("color") or "",
                        "birth_date": rec.get("birth_date") or "",
                        "owner": rec.get("owner") or "",
                        "owner_location": rec.get("owner_location") or "",
                        "breeder": rec.get("breeder") or "",
                        "breeder_location": rec.get("breeder_location") or "",
                        "sire": rec.get("sire") or "",
                        "sire_registration_number": rec.get("sire_registration_number") or "",
                        "dam": rec.get("dam") or "",
                        "dam_registration_number": rec.get("dam_registration_number") or "",
                        "extended_ancestry": rec.get("extended_ancestry") or "",
                        "source": src,
                    })

            self.wfile.write(json.dumps({
                "filter_mode": filter_mode,
                "total_items": len(merged),
                "total_records_in_dataset": len(records),
                "total_flagged_in_dataset": len(flagged),
                "items": merged
            }).encode("utf-8"))
            return

        if path == "/api/snippet":
            qs = parse_qs(parsed.query)
            ident = qs.get("ident", [""])[0]
            leaf = int(qs.get("leaf", [0])[0])
            col = int(qs.get("col", [1])[0])
            name = qs.get("name", [""])[0]
            reg = qs.get("reg", [""])[0]

            snippet_file = crop_dog_snippet(ident, leaf, col, name, reg)
            if not snippet_file or not snippet_file.is_file():
                # Fallback to serving the full column image if available
                full_col = WORK_DIR / f"{ident}_leaf_{leaf}_col{col}.png"
                if full_col.is_file():
                    snippet_file = full_col
                else:
                    self.send_error(404, "Snippet not found")
                    return

            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(snippet_file.read_bytes())
            return

        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/save":
            content_length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))

            flag_idx = payload.get("flag_index")
            r_idx = payload.get("record_index")
            updated = payload.get("record", {})

            # 1. Update record in records data
            if r_idx is not None and 0 <= r_idx < len(self.records_data["records"]):
                for k in ["name", "registration_number", "sex", "color", "birth_date",
                          "owner", "owner_location", "breeder", "breeder_location",
                          "sire", "sire_registration_number", "dam", "dam_registration_number"]:
                    if k in updated:
                        self.records_data["records"][r_idx][k] = updated[k]

                # Save updated records file
                self.records_file.write_text(json.dumps(self.records_data, indent=2), encoding="utf-8")

            # 2. Mark flag as resolved
            if flag_idx is not None and 0 <= flag_idx < len(self.flags_data.get("flagged_entries", [])):
                self.flags_data["flagged_entries"][flag_idx]["resolved"] = True
                self.flags_file.write_text(json.dumps(self.flags_data, indent=2), encoding="utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            return

        self.send_error(404)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Stud Book Review GUI</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  body { background: #1a1e24; color: #e1e7ed; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
  
  header { background: #121519; border-bottom: 1px solid #2d333b; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; }
  header h1 { font-size: 1.1rem; font-weight: 600; color: #58a6ff; }
  .status-badge { background: #21262d; border: 1px solid #30363d; padding: 4px 12px; border-radius: 12px; font-size: 0.85rem; }
  .shortcuts-hint { color: #8b949e; font-size: 0.8rem; }
  .shortcuts-hint kbd { background: #30363d; color: #c9d1d9; padding: 2px 5px; border-radius: 3px; font-size: 0.75rem; border: 1px solid #484f58; }

  main { flex: 1; display: grid; grid-template-columns: 1fr 1fr; overflow: hidden; }
  
  .scan-pane { background: #0d1117; border-right: 1px solid #2d333b; display: flex; flex-direction: column; overflow: hidden; }
  .scan-toolbar { padding: 8px 16px; background: #161b22; border-bottom: 1px solid #2d333b; display: flex; gap: 10px; align-items: center; }
  .scan-toolbar button { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 0.8rem; }
  .scan-toolbar button:hover { background: #30363d; }
  .image-container { flex: 1; overflow: auto; padding: 20px; display: flex; justify-content: center; align-items: flex-start; background: #000; }
  .image-container img { max-width: 100%; border: 1px solid #30363d; border-radius: 4px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); transform-origin: top center; transition: transform 0.1s; }

  .form-pane { background: #161b22; display: flex; flex-direction: column; overflow-y: auto; padding: 24px 32px; gap: 16px; }
  .flag-banner { background: #3d1f24; border: 1px solid #8e2531; color: #ff7b72; padding: 10px 16px; border-radius: 6px; font-size: 0.85rem; font-weight: 500; }
  
  .form-group { display: flex; flex-direction: column; gap: 6px; }
  .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  label { font-size: 0.8rem; font-weight: 600; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
  input[type="text"], select { background: #0d1117; border: 1px solid #30363d; color: #c9d1d9; padding: 8px 12px; border-radius: 6px; font-size: 0.95rem; }
  input[type="text"]:focus, select:focus { border-color: #58a6ff; outline: none; box-shadow: 0 0 0 3px rgba(88,166,255,0.2); }
  
  .nav-bar { background: #121519; border-top: 1px solid #2d333b; padding: 12px 24px; display: flex; justify-content: space-between; align-items: center; }
  .btn { padding: 8px 18px; border-radius: 6px; border: 1px solid transparent; font-size: 0.9rem; font-weight: 600; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; }
  .btn-prev { background: #21262d; border-color: #30363d; color: #c9d1d9; }
  .btn-next { background: #238636; color: #fff; }
  .btn-save { background: #1f6feb; color: #fff; }
  .btn:hover { filter: brightness(1.15); }
</style>
</head>
<body>

<header>
  <div style="display:flex; align-items:center; gap:16px;">
    <h1>AKC Stud Book Review Tool</h1>
    <select id="filterMode" onchange="switchFilter(this.value)" style="background:#21262d; border:1px solid #30363d; color:#c9d1d9; padding:4px 10px; border-radius:6px; font-size:0.85rem; cursor:pointer;">
      <option value="flagged">Flagged Only</option>
      <option value="all">All Dogs (Full Registry)</option>
    </select>
  </div>
  <div class="status-badge" id="progressText">Loading entries...</div>
  <div class="shortcuts-hint">
    <kbd>PageUp</kbd> Prev &bull; <kbd>PageDown</kbd> Save & Next &bull; <kbd>Tab</kbd> Next Field
  </div>
</header>

<main>
  <div class="scan-pane">
    <div class="scan-toolbar">
      <span style="font-size:0.8rem; color:#8b949e;">SCAN ZOOM:</span>
      <button onclick="changeZoom(0.2)">[+] Zoom In</button>
      <button onclick="changeZoom(-0.2)">[-] Zoom Out</button>
      <button onclick="resetZoom()">[R] Reset</button>
      <span id="sourceInfo" style="margin-left:auto; font-size:0.8rem; color:#8b949e;"></span>
    </div>
    <div class="image-container">
      <img id="scanImage" src="" alt="Scan snippet loading...">
    </div>
  </div>

  <div class="form-pane" id="formPane">
    <div class="flag-banner" id="flagBanner">Review Reason: Loading...</div>

    <div class="form-row">
      <div class="form-group">
        <label>AKC Registration #</label>
        <input type="text" id="field_reg" autofocus>
      </div>
      <div class="form-group">
        <label>Sex</label>
        <select id="field_sex">
          <option value="Male">Male (D)</option>
          <option value="Female">Female (B)</option>
        </select>
      </div>
    </div>

    <div class="form-group">
      <label>Registered Dog Name</label>
      <input type="text" id="field_name">
    </div>

    <div class="form-row">
      <div class="form-group">
        <label>Birth Date (Whelped)</label>
        <input type="text" id="field_birth">
      </div>
      <div class="form-group">
        <label>Color & Markings</label>
        <input type="text" id="field_color">
      </div>
    </div>

    <div class="form-row">
      <div class="form-group">
        <label>Owner Name</label>
        <input type="text" id="field_owner">
      </div>
      <div class="form-group">
        <label>Owner Location</label>
        <input type="text" id="field_owner_loc">
      </div>
    </div>

    <div class="form-row">
      <div class="form-group">
        <label>Breeder Name</label>
        <input type="text" id="field_breeder">
      </div>
      <div class="form-group">
        <label>Breeder Location</label>
        <input type="text" id="field_breeder_loc">
      </div>
    </div>

    <div class="form-row">
      <div class="form-group">
        <label>Sire Name</label>
        <input type="text" id="field_sire">
      </div>
      <div class="form-group">
        <label>Sire AKC #</label>
        <input type="text" id="field_sire_reg">
      </div>
    </div>

    <div class="form-row">
      <div class="form-group">
        <label>Dam Name</label>
        <input type="text" id="field_dam">
      </div>
      <div class="form-group">
        <label>Dam AKC #</label>
        <input type="text" id="field_dam_reg">
      </div>
    </div>
  </div>
</main>

<div class="nav-bar">
  <button class="btn btn-prev" onclick="navigate(-1)">&larr; Previous (PageUp)</button>
  <button class="btn btn-save" onclick="saveCurrent(false)">Save Changes</button>
  <button class="btn btn-next" onclick="saveCurrent(true)">Save & Next (PageDown) &rarr;</button>
</div>

<script>
  let items = [];
  let currentIndex = 0;
  let currentZoom = 1.0;

  let currentFilter = 'flagged';

  async function loadItems(filter = 'flagged') {
    currentFilter = filter;
    document.getElementById('filterMode').value = filter;
    document.getElementById('progressText').innerText = "Loading entries...";
    const res = await fetch(`/api/items?filter=${encodeURIComponent(filter)}`);
    const data = await res.json();
    items = data.items;
    if (items.length > 0) {
      showItem(0);
    } else {
      document.getElementById('progressText').innerText = "No entries in this view!";
      clearForm();
    }
  }

  function switchFilter(mode) {
    loadItems(mode);
  }

  function clearForm() {
    document.getElementById('flagBanner').style.display = 'none';
    document.getElementById('field_reg').value = '';
    document.getElementById('field_name').value = '';
    document.getElementById('field_birth').value = '';
    document.getElementById('field_color').value = '';
    document.getElementById('field_owner').value = '';
    document.getElementById('field_owner_loc').value = '';
    document.getElementById('field_breeder').value = '';
    document.getElementById('field_breeder_loc').value = '';
    document.getElementById('field_sire').value = '';
    document.getElementById('field_sire_reg').value = '';
    document.getElementById('field_dam').value = '';
    document.getElementById('field_dam_reg').value = '';
    document.getElementById('scanImage').src = '';
  }

  function showItem(idx) {
    if (idx < 0 || idx >= items.length) return;
    currentIndex = idx;
    const item = items[currentIndex];

    const label = currentFilter === 'all' ? 'Dog' : 'Flagged Dog';
    const status = item.flags.length === 0 ? 'CLEAN' : (item.resolved ? 'RESOLVED' : 'FLAGGED');

    document.getElementById('progressText').innerText =
      `${label} ${currentIndex + 1} of ${items.length} (${status})`;

    const flagBanner = document.getElementById('flagBanner');
    if (item.flags && item.flags.length > 0) {
      flagBanner.style.display = 'block';
      flagBanner.innerText = "Flag Reason: " + item.flags.join(", ");
    } else {
      flagBanner.style.display = 'none';
    }
    document.getElementById('field_reg').value = item.registration_number || '';
    document.getElementById('field_sex').value = item.sex || 'Male';
    document.getElementById('field_name').value = item.name || '';
    document.getElementById('field_birth').value = item.birth_date || '';
    document.getElementById('field_color').value = item.color || '';
    document.getElementById('field_owner').value = item.owner || '';
    document.getElementById('field_owner_loc').value = item.owner_location || '';
    document.getElementById('field_breeder').value = item.breeder || '';
    document.getElementById('field_breeder_loc').value = item.breeder_location || '';
    document.getElementById('field_sire').value = item.sire || '';
    document.getElementById('field_sire_reg').value = item.sire_registration_number || '';
    document.getElementById('field_dam').value = item.dam || '';
    document.getElementById('field_dam_reg').value = item.dam_registration_number || '';

    // Load snippet image
    const src = item.source || {};
    const imgUrl = `/api/snippet?ident=${encodeURIComponent(src.identifier || '')}&leaf=${src.leaf || 0}&col=${src.column || 1}&name=${encodeURIComponent(item.name || '')}&reg=${encodeURIComponent(item.registration_number || '')}`;
    document.getElementById('scanImage').src = imgUrl;
    document.getElementById('sourceInfo').innerText = `${src.identifier || ''} (Leaf ${src.leaf}, Col ${src.column})`;

    resetZoom();
  }

  async function saveCurrent(advance = false) {
    if (items.length === 0) return;
    const item = items[currentIndex];

    const updated = {
      registration_number: document.getElementById('field_reg').value.trim(),
      sex: document.getElementById('field_sex').value,
      name: document.getElementById('field_name').value.trim(),
      birth_date: document.getElementById('field_birth').value.trim(),
      color: document.getElementById('field_color').value.trim(),
      owner: document.getElementById('field_owner').value.trim(),
      owner_location: document.getElementById('field_owner_loc').value.trim() || null,
      breeder: document.getElementById('field_breeder').value.trim(),
      breeder_location: document.getElementById('field_breeder_loc').value.trim() || null,
      sire: document.getElementById('field_sire').value.trim(),
      sire_registration_number: document.getElementById('field_sire_reg').value.trim() || null,
      dam: document.getElementById('field_dam').value.trim(),
      dam_registration_number: document.getElementById('field_dam_reg').value.trim() || null
    };

    await fetch('/api/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        flag_index: item.flag_index,
        record_index: item.record_index,
        record: updated
      })
    });

    item.resolved = true;
    Object.assign(item, updated);

    if (advance && currentIndex < items.length - 1) {
      showItem(currentIndex + 1);
    } else {
      document.getElementById('progressText').innerText =
        `Flagged Dog ${currentIndex + 1} of ${items.length} (SAVED)`;
    }
  }

  function navigate(delta) {
    showItem(currentIndex + delta);
  }

  function changeZoom(factor) {
    currentZoom = Math.max(0.4, Math.min(3.0, currentZoom + factor));
    document.getElementById('scanImage').style.transform = `scale(${currentZoom})`;
  }

  function resetZoom() {
    currentZoom = 1.0;
    document.getElementById('scanImage').style.transform = `scale(1.0)`;
  }

  // Keyboard navigation
  document.addEventListener('keydown', (e) => {
    if (e.key === 'PageDown' || (e.altKey && e.key === 'ArrowRight')) {
      e.preventDefault();
      saveCurrent(true);
    } else if (e.key === 'PageUp' || (e.altKey && e.key === 'ArrowLeft')) {
      e.preventDefault();
      navigate(-1);
    }
  });

  loadItems();
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=1926)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--all", action="store_true", help="Start in 'All Dogs' mode instead of 'Flagged Only'")
    args = parser.parse_args()

    records_file = DATA_DIR / f"autonomous_dobermans_{args.year}.json"
    flags_file = DATA_DIR / f"review_flags_{args.year}.json"

    if not records_file.is_file() or not flags_file.is_file():
        # Fallback to default unsuffixed files
        records_file = DATA_DIR / "autonomous_dobermans.json"
        flags_file = DATA_DIR / "review_flags.json"

    if not records_file.is_file() or not flags_file.is_file():
        print(f"Error: could not locate records or flags files for year {args.year}", file=sys.stderr)
        return 1

    ReviewHandler.records_file = records_file
    ReviewHandler.flags_file = flags_file
    ReviewHandler.records_data = json.loads(records_file.read_text(encoding="utf-8"))
    ReviewHandler.flags_data = json.loads(flags_file.read_text(encoding="utf-8"))
    ReviewHandler.default_filter = "all" if args.all else "flagged"

    server = HTTPServer(("127.0.0.1", args.port), ReviewHandler)
    print(f"Starting Review GUI for {args.year} at http://127.0.0.1:{args.port}/")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping review server.")
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
