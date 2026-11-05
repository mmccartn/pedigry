#!/usr/bin/env python3
"""
Standalone On-Demand JP2 Page Scraper for AKC Stud Books.
Downloads specific single pages or full page ranges from Internet Archive _jp2.zip
archives using fast HTTP Range requests without downloading entire multi-hundred MB volume zips.

Usage:
    # Download single page / leaf:
    python download_jp2_pages.py --identifier sim_american-kennel-club-stud-book-register_1929-01-01_46_1 --leaf 40

    # Download range of leaves:
    python download_jp2_pages.py --identifier sim_american-kennel-club-stud-book-register_1929-01-01_46_1 --range 3 65

    # Download all primary registration pages for a volume:
    python download_jp2_pages.py --year 1929 --issue 1 --all-registrations

    # Download specific breed pages across all issues in a year:
    python download_jp2_pages.py --year 1929 --breed "Doberman"
"""

import argparse
import io
import json
import re
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


class RemoteZipFile(io.RawIOBase):
    """File-like stream enabling zipfile.ZipFile random-access over HTTP Range requests."""

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
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                self.pos += len(data)
                return data
            except Exception as e:
                if attempt == 3:
                    raise e
                time.sleep(1 + attempt)
        return b""


def get_remote_zip_size(zip_url: str) -> Optional[int]:
    """Inspect remote zip file size via Range: bytes=0-0 probe."""
    for attempt in range(3):
        try:
            req = urllib.request.Request(zip_url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                cr = resp.headers.get("Content-Range")
                if cr:
                    return int(cr.split("/")[-1])
        except Exception as e:
            if attempt == 2:
                print(f"Error accessing remote zip {zip_url}: {e}", file=sys.stderr)
            time.sleep(1)
    return None


class RemoteArchiveSession:
    """Maintains cached zip file central directory index to download multiple leaves rapidly."""

    def __init__(self, identifier: str):
        self.identifier = identifier
        self.zip_url = f"https://archive.org/download/{identifier}/{identifier}_jp2.zip"
        self.total_size = get_remote_zip_size(self.zip_url)
        self.zf = None
        self.file_map = {}

        if self.total_size:
            rz = RemoteZipFile(self.zip_url, self.total_size)
            self.zf = zipfile.ZipFile(rz)
            for info in self.zf.infolist():
                # Map leaf index e.g. "0040" -> filename
                m = re.search(r"_(\d{4})\.jp2$", info.filename)
                if m:
                    self.file_map[int(m.group(1))] = info.filename

    def download_leaf(self, leaf_idx: int, out_dir: Path) -> Optional[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{self.identifier}_{leaf_idx:04d}.jp2"
        if out_path.is_file() and out_path.stat().st_size > 10000:
            return out_path

        if not self.zf or leaf_idx not in self.file_map:
            print(f"Leaf {leaf_idx} not found in remote archive manifest for {self.identifier}", file=sys.stderr)
            return None

        target_file = self.file_map[leaf_idx]
        for attempt in range(3):
            try:
                data = self.zf.read(target_file)
                out_path.write_bytes(data)
                return out_path
            except Exception as e:
                if attempt == 2:
                    print(f"Failed to fetch leaf {leaf_idx} for {self.identifier}: {e}", file=sys.stderr)
                time.sleep(1.5)
        return None


def find_breed_leaves(vol_dir: Path, breed_query: str) -> list[int]:
    """Scan DjVu XML for a specific breed and return leaf indices."""
    xml_files = list(vol_dir.glob("*_djvu.xml"))
    if not xml_files:
        return []
    import xml.etree.ElementTree as ET
    tree = ET.parse(xml_files[0])
    objects = tree.getroot().findall(".//OBJECT")
    matched = []
    q = breed_query.upper()
    for idx, obj in enumerate(objects):
        words = [w.text or "" for w in obj.findall(".//WORD")]
        txt = " ".join(words).upper()
        if q in txt:
            matched.append(idx)
    return matched


def find_registration_leaves(vol_dir: Path) -> list[int]:
    """Find all leaves containing primary dog registrations (page 2 up to Changes of Ownership)."""
    pnums_p = list(vol_dir.glob("*_page_numbers.json"))
    if not pnums_p:
        return []
    pnums = json.loads(pnums_p[0].read_text(encoding="utf-8"))
    reg_leaves = []
    for p in pnums.get("pages", []):
        pnum = p.get("pageNumber", "").strip()
        leaf = p.get("leafNum")
        if pnum.isdigit() and int(pnum) >= 2:
            reg_leaves.append(leaf)
    return reg_leaves


def main():
    parser = argparse.ArgumentParser(description="Download on-demand JP2 scan pages from Archive.org via HTTP Range requests.")
    parser.add_argument("--identifier", type=str, help="Internet Archive volume identifier")
    parser.add_argument("--year", type=int, help="Year (e.g. 1929)")
    parser.add_argument("--issue", type=int, help="Issue number (e.g. 1)")
    parser.add_argument("--leaf", type=int, help="Single leaf index to download (e.g. 40)")
    parser.add_argument("--range", type=int, nargs=2, metavar=("START", "END"), help="Range of leaf indices (e.g. --range 3 65)")
    parser.add_argument("--breed", type=str, help="Download only leaves for a specific breed (e.g. 'Doberman' or 'Beagle')")
    parser.add_argument("--all-registrations", action="store_true", help="Download all primary registration pages in the volume")
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent / "data" / "jp2")
    parser.add_argument("--manifest", type=Path, default=Path(__file__).resolve().parent / "data" / "manifest.json")
    parser.add_argument("--volumes-dir", type=Path, default=Path(__file__).resolve().parent / "data" / "volumes")
    args = parser.parse_args()

    # Resolve identifier if given year/issue
    ident = args.identifier
    if not ident and args.year:
        manifest_data = json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest.exists() else {}
        items = manifest_data.get("items", {})
        for k, v in items.items():
            if str(args.year) in k:
                if args.issue is None or str(args.issue) == str(v.get("issue")):
                    ident = k
                    break

    if not ident:
        print("Error: Could not resolve volume identifier. Provide --identifier or --year [--issue].", file=sys.stderr)
        return 1

    print(f"Connecting to remote archive: {ident}...")
    session = RemoteArchiveSession(ident)
    if not session.total_size:
        print(f"Error: Could not inspect remote zip file for {ident}.", file=sys.stderr)
        return 1

    print(f"Remote zip archive size: {session.total_size / (1024 * 1024):.1f} MB (Indexed {len(session.file_map)} pages)")

    leaves_to_download = []
    if args.leaf is not None:
        leaves_to_download.append(args.leaf)
    elif args.range:
        leaves_to_download.extend(range(args.range[0], args.range[1] + 1))
    elif args.breed:
        vol_dir = args.volumes_dir / ident
        leaves_to_download = find_breed_leaves(vol_dir, args.breed)
        print(f"Found {len(leaves_to_download)} leaves mentioning '{args.breed}': {leaves_to_download}")
    elif args.all_registrations:
        vol_dir = args.volumes_dir / ident
        leaves_to_download = find_registration_leaves(vol_dir)
        print(f"Found {len(leaves_to_download)} primary registration leaves: {leaves_to_download[0]} to {leaves_to_download[-1]}")
    else:
        print("Error: Specify pages to download using --leaf, --range, --breed, or --all-registrations.", file=sys.stderr)
        return 1

    if not leaves_to_download:
        print("No matching leaves found.")
        return 0

    print(f"\nStarting download of {len(leaves_to_download)} pages to {args.out_dir}...")
    t0 = time.time()
    success = 0

    for idx, leaf in enumerate(leaves_to_download, 1):
        target_path = args.out_dir / f"{ident}_{leaf:04d}.jp2"
        already = target_path.exists()
        res = session.download_leaf(leaf, args.out_dir)
        if res:
            success += 1
            status = "cached" if already else "downloaded"
            size_kb = res.stat().st_size / 1024
            print(f"[{idx}/{len(leaves_to_download)}] Leaf {leaf:04d} ({size_kb:.0f} KB) - {status}")
        else:
            print(f"[{idx}/{len(leaves_to_download)}] Leaf {leaf:04d} - FAILED", file=sys.stderr)

    elapsed = time.time() - t0
    print(f"\nFinished in {elapsed:.2f}s ({success}/{len(leaves_to_download)} pages ready in {args.out_dir}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
