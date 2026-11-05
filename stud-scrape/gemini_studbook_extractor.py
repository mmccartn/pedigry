#!/usr/bin/env python3
"""
Autonomous Multi-Breed Stud Book Extractor powered by Google Gemini Vision.
Extracts clean, structured pedigree and registration records across all dog breeds
directly from scan images, including extended ancestry and handwritten annotations.

Usage:
    python gemini_studbook_extractor.py --image path/to/scan.png
    python gemini_studbook_extractor.py --year 1929 --issue 1
"""

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from PIL import Image


def normalize_breed_name(breed_raw: str) -> str:
    """Normalize breed names into standard title-cased canonical forms."""
    if not breed_raw:
        return "Unknown"
    b = re.sub(r"\s+", " ", breed_raw).strip()
    # If uppercase or mixed, convert to Title Case while preserving special designations
    b_title = b.title()
    # Fix specific parenthesized designations e.g. "Pinschers (Doberman)", "St. Bernards (Rough-Coated)"
    b_title = re.sub(r"\(\s*Doberman\s*\)", "(Doberman)", b_title, flags=re.IGNORECASE)
    b_title = re.sub(r"\(\s*Wire\s*\)", "(Wire)", b_title, flags=re.IGNORECASE)
    b_title = re.sub(r"\(\s*Smooth\s*\)", "(Smooth)", b_title, flags=re.IGNORECASE)
    b_title = re.sub(r"\(\s*Brussels\s*\)", "(Brussels)", b_title, flags=re.IGNORECASE)
    return b_title


def load_env_file():
    """Load GEMINI_API_KEY from .env file if present."""
    search_paths = [
        Path(".env"),
        Path(__file__).parent / ".env",
        Path(__file__).parent.parent / ".env",
    ]
    for p in search_paths:
        if p.is_file():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k not in os.environ:
                            os.environ[k] = v
                break
            except Exception:
                pass


load_env_file()

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
DEFAULT_MODEL = "gemini-3.8-flash"

PROMPT = """You are an expert canine genealogist and archivist transcribing official American Kennel Club Stud Book Register pages.

Transcribe all dog registration entries shown in this image across ALL breeds.
Also transcribe any handwritten annotations, pencil notes, or supplementary sections (Changes of Ownership / Transfers, Corrections, Cancellations).

IMPORTANT BREED NORMALIZATION RULE:
Always normalize breed names to standard Title Case (e.g. "Pinschers (Doberman)", "Pomeranians", "Pointers", "Pekingese", "Airedale Terriers", "Beagles", "Collies").
Even if a section heading is printed in all uppercase like "PINSCHERS (Doberman)" or "POMERANIANS", or if a dog begins under a column heading and wraps across columns or pages, record the exact same canonical Title Case breed name for every dog in that breed.

Return a JSON object with this exact structure:
{
  "printed_page_number": "string or null",
  "breeds_found": ["Canonical Title Case Breed 1", "Canonical Title Case Breed 2"],
  "records": [
    {
      "breed": "Standard Title Case Breed name (e.g. 'Pinschers (Doberman)', 'Pomeranians', 'Pointers')",
      "name": "Registered dog name in UPPERCASE",
      "sex": "Male or Female",
      "registration_number": "Clean numeric string without commas (e.g. '678672')",
      "birth_date": "Whelped date (e.g. 'June 19, 1926') or null",
      "color": "Color/markings description or null",
      "owner": "Owner name",
      "owner_location": "Owner city/state or null",
      "breeder": "Breeder name",
      "breeder_location": "Breeder city/state or null",
      "sire": "Sire registered name",
      "sire_registration_number": "Sire numeric registration or null",
      "dam": "Dam registered name",
      "dam_registration_number": "Dam numeric registration or null",
      "extended_ancestry": [
        {"role": "sire_of_sire", "name": "...", "registration_number": "..."}
      ],
      "handwritten_annotation": "Any handwritten note next to this specific entry, or null"
    }
  ],
  "handwritten_annotations": [
    {
      "text": "Exact handwritten text",
      "location": "e.g. bottom left margin",
      "associated_registration": "Registration number if identifiable, else null"
    }
  ],
  "supplementary_sections": [
    {
      "section_type": "Changes of Ownership, Corrections, or Cancellations",
      "breed": "Breed name if applicable, or null",
      "entries": [
        {
          "dog_name": "Dog name",
          "registration_number": "Registration number or null",
          "detail": "Description of transfer or correction"
        }
      ]
    }
  ]
}

Ensure all fields are accurate. Output ONLY the JSON object without markdown fences."""


def call_gemini_vision(images: list[Image.Image] | Image.Image, api_key: str, model: str = DEFAULT_MODEL, lossless: bool = False) -> dict[str, Any]:
    """Send one or more continuous page scan images to Google Gemini Vision and return structured JSON."""
    if isinstance(images, Image.Image):
        images = [images]

    t_prep_start = time.time()
    parts = []
    for img in images:
        if lossless:
            # Full lossless original PNG transmission
            buffered = io.BytesIO()
            img.save(buffered, format="PNG")
            img_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
            parts.append({"inline_data": {"mime_type": "image/png", "data": img_b64}})
        else:
            # Optimized transmission: max 2200px width with quality 85 JPEG
            w, h = img.size
            if w > 2200:
                scale = 2200 / w
                img_to_send = img.resize((2200, int(h * scale)), Image.Resampling.LANCZOS)
            else:
                img_to_send = img

            buffered = io.BytesIO()
            img_to_send.convert("RGB").save(buffered, format="JPEG", quality=85, optimize=True)
            img_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": img_b64}})

    prompt_text = PROMPT
    if len(images) > 1:
        prompt_text += "\nNote: Multiple consecutive pages are provided. Seamlessly stitch and complete any entries that begin at the bottom of one page and continue onto the next page."

    parts.append({"text": prompt_text})
    prep_time = time.time() - t_prep_start

    payload = {
        "contents": [
            {
                "parts": parts
            }
        ],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.1,
            "thinkingConfig": {
                "thinkingBudget": 0
            }
        }
    }

    url = GEMINI_API_URL.format(model=model, api_key=api_key)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    max_retries = 3
    for attempt in range(max_retries):
        t_net_start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                net_time = time.time() - t_net_start
                resp_data = json.loads(resp.read().decode("utf-8"))
                usage = resp_data.get("usageMetadata", {})
                prompt_tokens = usage.get("promptTokenCount", 0)
                cand_tokens = usage.get("candidatesTokenCount", 0)
                thought_tokens = usage.get("thoughtsTokenCount", 0) or 0
                total_tokens = usage.get("totalTokenCount", 0)
                
                # Cost calculation based on official Google Gemini Flash billing rates:
                # Input: $0.10 / 1M tokens ($0.0000001 per token)
                # Output: $0.40 / 1M tokens ($0.0000004 per token)
                call_cost = (prompt_tokens * 0.0000001) + (cand_tokens * 0.0000004)
                print(f"[Prep: {prep_time:.2f}s | Net/Model: {net_time:.2f}s | Tokens: {total_tokens:,} (in: {prompt_tokens:,}, out: {cand_tokens:,}) | Billed: ${call_cost:.5f}]", flush=True)

                text_content = resp_data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_content)
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", errors="replace")
            if err.code == 429:
                wait_time = (attempt + 1) * 5
                print(f"Rate limited by Gemini API, waiting {wait_time}s...", file=sys.stderr)
                time.sleep(wait_time)
                continue
            raise RuntimeError(f"Gemini API Error (HTTP {err.code}): {err_body}") from err
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            time.sleep(2)

    raise RuntimeError("Failed to obtain response from Gemini Vision after retries.")


def deskew_image(img: Image.Image) -> Image.Image:
    """Detect scan rotation and deskew image to ensure vertical column gutters."""
    try:
        import cv2
        import numpy as np
        gray = np.array(img.convert("L"))
        h, w = gray.shape
        small = cv2.resize(gray, (w // 4, h // 4))
        edges = cv2.Canny(small, 50, 150, apertureSize=3)
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180, 100, minLineLength=100, maxLineGap=10)
        angles = []
        if lines is not None:
            for line in lines:
                x1, y1, x2, y2 = line[0]
                if x2 - x1 != 0:
                    ang = np.degrees(np.arctan((y2 - y1) / (x2 - x1)))
                    if 0.1 < abs(ang) < 8.0:
                        angles.append(ang)
        if angles:
            median_angle = float(np.median(angles))
            if abs(median_angle) >= 0.2:
                # Rotate around center
                return img.rotate(median_angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=(255, 255, 255))
    except Exception:
        pass
    return img


def find_dynamic_gutters(img: Image.Image, num_cols: int = 3) -> list[tuple[int, int, int, int]]:
    """Find vertical gutters by density projection with generous safety overlap."""
    import numpy as np
    w, h = img.size
    gray = np.array(img.convert("L"))
    dark = (255 - gray) > 75
    # Analyze central body (skip header/footer)
    body = dark[int(h * 0.12):int(h * 0.88), :]
    profile = np.sum(body, axis=0, dtype=np.float32)
    k = 30
    smoothed = np.convolve(profile, np.ones(k) / k, mode='same')

    # Expected column boundary zones
    gutters = []
    col_w = w / num_cols
    for c in range(1, num_cols):
        center_x = int(c * col_w)
        search_min = max(0, int(center_x - col_w * 0.18))
        search_max = min(w, int(center_x + col_w * 0.18))
        gutter_x = search_min + int(np.argmin(smoothed[search_min:search_max]))
        gutters.append(gutter_x)

    # Build boxes with 75px safety overlap on each side so text is never clipped
    overlap = 75
    boxes = []
    x_prev = 0
    for g in gutters:
        boxes.append((max(0, x_prev - (overlap if x_prev > 0 else 0)), 0, min(w, g + overlap), h))
        x_prev = g
    boxes.append((max(0, x_prev - overlap), 0, w, h))
    return boxes


def process_page_scan(
    page_img: Image.Image,
    api_key: str,
    model: str = DEFAULT_MODEL,
    layout_cols: int = 3,
    lossless: bool = False
) -> dict[str, Any]:
    """Process a full page scan by deskewing and sending generous overlapping columns to Gemini Vision."""
    # 1. Deskew scan if rotated
    deskewed_img = deskew_image(page_img)

    # 2. Find gutters with generous 75px overlap
    try:
        boxes = find_dynamic_gutters(deskewed_img, num_cols=layout_cols)
    except Exception:
        # Fallback to evenly spaced boxes with 75px overlap
        w, h = deskewed_img.size
        col_w = w / layout_cols
        overlap = 75
        boxes = []
        for c in range(layout_cols):
            x1 = max(0, int(c * col_w - (overlap if c > 0 else 0)))
            x2 = min(w, int((c + 1) * col_w + (overlap if c < layout_cols - 1 else 0)))
            boxes.append((x1, 0, x2, h))

    col_crops = [deskewed_img.crop(box) for box in boxes]

    all_records = []
    all_breeds = set()
    all_annotations = []
    all_supplementary = []
    printed_page = None

    for idx, crop in enumerate(col_crops, 1):
        res = call_gemini_vision(crop, api_key, model=model, lossless=lossless)
        if not printed_page and res.get("printed_page_number"):
            printed_page = res.get("printed_page_number")
        for b in res.get("breeds_found", []):
            all_breeds.add(normalize_breed_name(b))
        for r in res.get("records", []):
            if r.get("breed"):
                r["breed"] = normalize_breed_name(r["breed"])
            all_records.extend([r])
        all_annotations.extend(res.get("handwritten_annotations", []))
        all_supplementary.extend(res.get("supplementary_sections", []))

    # Deduplicate records across overlap margins
    seen_regs = set()
    seen_names = set()
    deduped_records = []
    for r in all_records:
        reg = r.get("registration_number", "").strip()
        name = r.get("name", "").strip()
        if reg and reg in seen_regs:
            continue
        if name and name in seen_names:
            continue
        if reg:
            seen_regs.add(reg)
        if name:
            seen_names.add(name)
        deduped_records.append(r)

    return {
        "printed_page_number": printed_page,
        "breeds_found": sorted(list(all_breeds)),
        "records": deduped_records,
        "handwritten_annotations": all_annotations,
        "supplementary_sections": all_supplementary
    }


def main():
    parser = argparse.ArgumentParser(description="Extract all dog breeds, handwritten annotations, and transfers using Gemini Vision.")
    parser.add_argument("--api-key", type=str, default=os.environ.get("GEMINI_API_KEY"), help="Google AI / Gemini API Key (or set in .env)")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL, help="Gemini model name")
    parser.add_argument("--image", type=Path, help="Path to a scan image file to extract")
    parser.add_argument("--year", type=int, help="Year of volume to extract (e.g. 1929)")
    parser.add_argument("--issue", type=int, default=1, help="Issue number of volume to extract (e.g. 1)")
    parser.add_argument("--leaf-start", type=int, default=3, help="Starting leaf index for full-volume processing")
    parser.add_argument("--leaf-end", type=int, default=69, help="Ending leaf index for full-volume processing")
    parser.add_argument("--full-page", action="store_true", help="Send complete full pages in single requests instead of splitting into columns")
    parser.add_argument("--lossless", action="store_true", help="Send original uncompressed PNG images without JPEG downsampling")
    parser.add_argument("--output", type=Path, help="Output JSON path")
    args = parser.parse_args()

    if not args.api_key:
        print("Error: No Gemini API Key provided. Set GEMINI_API_KEY in your .env file or pass --api-key.", file=sys.stderr)
        return 1

    if args.image:
        if not args.image.exists():
            print(f"Error: Image file not found: {args.image}", file=sys.stderr)
            return 1
        img = Image.open(args.image)
        print(f"Sending image to Gemini Vision ({args.model}): {args.image.name} ({img.width}x{img.height})...")
        t0 = time.time()
        # Process as single full page if --full-page requested or narrow crop, else default to column passes
        if args.full_page or img.width <= 2000:
            result = call_gemini_vision(img, args.api_key, model=args.model, lossless=args.lossless)
        else:
            result = process_page_scan(img, args.api_key, model=args.model, lossless=args.lossless)
        elapsed = time.time() - t0

        records = result.get("records", [])
        breeds = result.get("breeds_found", [])
        annotations = result.get("handwritten_annotations", [])
        supplementary = result.get("supplementary_sections", [])

        print(f"\n==================================================")
        print(f"Extraction completed in {elapsed:.2f}s!")
        print(f"Printed Page Number: {result.get('printed_page_number')}")
        print(f"Breeds Found ({len(breeds)}): {', '.join(breeds)}")
        print(f"Primary Dog Registrations Extracted: {len(records)}")
        print(f"Handwritten Annotations Found: {len(annotations)}")
        if annotations:
            for a in annotations:
                print(f"  * [{a.get('location')}]: {repr(a.get('text'))}")
        print(f"Supplementary Sections: {len(supplementary)}")
        if supplementary:
            for s in supplementary:
                print(f"  * {s.get('section_type')} ({s.get('breed') or 'All'}): {len(s.get('entries', []))} entries")
        print(f"==================================================")

        out_path = args.output or (Path(__file__).parent / "data" / f"gemini_extracted_{args.image.stem}.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nSaved structured JSON to: {out_path}")
        return 0

    if args.year:
        from download_jp2_pages import RemoteArchiveSession
        ident = f"sim_american-kennel-club-stud-book-register_{args.year}-{args.issue:02d}-01_46_{args.issue}"
        print(f"Initializing full-volume extraction for {ident} (leaves {args.leaf_start} to {args.leaf_end})...")
        session = RemoteArchiveSession(ident)
        jp2_dir = Path(__file__).parent / "data" / "jp2"

        vol_records = []
        vol_breeds = set()
        vol_annotations = []
        vol_supplementary = []
        vol_total_cost = 0.0
        vol_total_tokens = 0

        leaves_cache_dir = Path(__file__).parent / "data" / "gemini_pages" / ident
        leaves_cache_dir.mkdir(parents=True, exist_ok=True)

        for leaf_idx in range(args.leaf_start, args.leaf_end + 1):
            leaf_json_p = leaves_cache_dir / f"leaf_{leaf_idx:04d}.json"
            if leaf_json_p.exists():
                print(f"[Leaf {leaf_idx:02d}/{args.leaf_end}] Loading already cached JSON...")
                page_data = json.loads(leaf_json_p.read_text(encoding="utf-8"))
            else:
                p = session.download_leaf(leaf_idx, jp2_dir)
                if not p or not p.is_file():
                    continue
                img = Image.open(p)
                print(f"\n[Leaf {leaf_idx:02d}/{args.leaf_end}] Processing {p.name} ({img.width}x{img.height})...")
                try:
                    if args.full_page:
                        page_data = call_gemini_vision(img, args.api_key, model=args.model, lossless=args.lossless)
                    else:
                        page_data = process_page_scan(img, args.api_key, model=args.model, lossless=args.lossless)
                    leaf_json_p.write_text(json.dumps(page_data, indent=2), encoding="utf-8")
                except Exception as err:
                    print(f"  Error on leaf {leaf_idx}: {err}", file=sys.stderr)
                    continue

            p_recs = page_data.get("records", [])
            p_breeds = page_data.get("breeds_found", [])
            page_cost = page_data.get("page_billed_cost", 0.0)
            vol_total_cost += page_cost
            print(f"  Extracted {len(p_recs)} dogs across: {', '.join(p_breeds)}")
            for r in p_recs:
                r["source_leaf"] = leaf_idx
            vol_records.extend(p_recs)
            for b in p_breeds:
                vol_breeds.add(b)
            vol_annotations.extend(page_data.get("handwritten_annotations", []))
            vol_supplementary.extend(page_data.get("supplementary_sections", []))

        final_vol_data = {
            "volume_identifier": ident,
            "year": args.year,
            "issue": args.issue,
            "total_dogs": len(vol_records),
            "breeds_count": len(vol_breeds),
            "breeds": sorted(list(vol_breeds)),
            "billed_cost_usd": round(vol_total_cost, 5),
            "records": vol_records,
            "handwritten_annotations": vol_annotations,
            "supplementary_sections": vol_supplementary
        }

        out_path = args.output or (Path(__file__).parent / "data" / f"gemini_volume_{ident}.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(final_vol_data, indent=2), encoding="utf-8")
        print(f"\n==================================================")
        print(f"VOLUME EXTRACTION COMPLETE: {ident}")
        print(f"Total Dogs Extracted: {len(vol_records)}")
        print(f"Total Breeds: {len(vol_breeds)}")
        if vol_total_cost > 0:
            print(f"Total Billed Cost: ${vol_total_cost:.4f} USD")
        print(f"Saved full volume JSON to: {out_path}")
        print(f"==================================================")
        return 0

    print("Please provide --image <path> or --year <year> --issue <issue>")
    return 1


if __name__ == "__main__":
    sys.exit(main())
