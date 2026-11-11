#!/usr/bin/env python3
"""Run Surya OCR (local llama.cpp backend on the GPU) over located pages.

Reads ``data/pages/<year>/<id>/locate.json`` and writes one JSON per page to
``data/ocr/<year>/<id>/p<NNN>.json`` containing Surya's layout blocks
(label, bbox, html). Already-processed pages are skipped, so the command can
be stopped and restarted at any time.

The Surya model files and llama.cpp binaries live in ``<repo>/bin``.

    python ocr.py --year 1934
    python ocr.py --ids 41990
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from akc_library import HERE

REPO = HERE.parent
BIN = REPO / "bin"
PAGES = HERE / "data" / "pages"
OCR = HERE / "data" / "ocr"


def _stop_process(pid: int, name: str) -> None:
    """Windows-safe replacement for Surya's backend shutdown."""
    if not pid:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def load_predictor():
    os.environ["LLAMA_CPP_BINARY"] = str(BIN / "llama-server.exe")
    os.environ["PATH"] = str(BIN) + os.pathsep + os.environ.get("PATH", "")
    os.environ["SURYA_INFERENCE_BACKEND"] = "llamacpp"
    from surya.settings import settings

    settings.SURYA_INFERENCE_BACKEND = "llamacpp"
    settings.SURYA_GGUF_LOCAL_MODEL_PATH = str(BIN / "surya-2.5.gguf")
    settings.SURYA_GGUF_LOCAL_MMPROJ_PATH = str(BIN / "surya-2.5-mmproj.gguf")
    try:
        import surya.inference.backends.spawn as spawn

        spawn._stop_process = _stop_process
    except Exception:
        pass
    from surya.recognition import RecognitionPredictor

    return RecognitionPredictor()


def pending_pages(year: int | None, ids: set[str] | None) -> list[tuple[Path, Path]]:
    work = []
    for locate_path in sorted(PAGES.glob("*/*/locate.json")):
        cat_dir = locate_path.parent
        if year and cat_dir.parent.name != str(year):
            continue
        if ids and cat_dir.name not in ids:
            continue
        summary = json.loads(locate_path.read_text(encoding="utf-8"))
        out_dir = OCR / cat_dir.parent.name / cat_dir.name
        for page in summary["candidate_pages"]:
            image = cat_dir / page["image"]
            target = out_dir / (Path(page["image"]).stem + ".json")
            if image.exists() and not target.exists():
                work.append((image, target))
    return work


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--year", type=int)
    parser.add_argument("--ids")
    args = parser.parse_args()
    work = pending_pages(args.year, set(args.ids.split(",")) if args.ids else None)
    print(f"{len(work)} pages to OCR", flush=True)
    if not work:
        return
    from PIL import Image

    predictor = load_predictor()
    started = time.time()
    for n, (image_path, target) in enumerate(work, 1):
        t0 = time.time()
        try:
            image = Image.open(image_path).convert("RGB")
            result = predictor([image], full_page=True)[0]
        except Exception as err:
            print(f"[{n}/{len(work)}] {image_path}: OCR failed: {err}", file=sys.stderr, flush=True)
            continue
        blocks = [
            {
                "label": getattr(b, "label", None),
                "bbox": [round(v, 1) for v in (getattr(b, "bbox", None) or [])],
                "html": getattr(b, "html", None) or "",
            }
            for b in result.blocks
        ]
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"image": str(image_path.relative_to(HERE)), "size": image.size, "blocks": blocks}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(tmp, target)
        elapsed = time.time() - started
        eta = elapsed / n * (len(work) - n)
        print(f"[{n}/{len(work)}] {image_path.parent.name}/{image_path.name} {time.time() - t0:.1f}s  eta {eta / 60:.0f} min", flush=True)


if __name__ == "__main__":
    main()

