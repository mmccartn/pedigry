#!/usr/bin/env python3
"""
Generate a breakdown table of extracted dogs per volume and compare against template.yml expectations.
Usage:
    python report_counts.py [--year 1929]
    python report_counts.py --all
"""

import argparse
import json
import re
from pathlib import Path
from collections import Counter


def parse_template(template_path: Path) -> dict[str, int]:
    """Parse expected volume counts from template.yml."""
    if not template_path.exists():
        return {}

    lines = template_path.read_text(encoding="utf-8").splitlines()
    expectations: dict[str, int] = {}
    current_vol = None

    for line in lines:
        line_clean = line.strip()
        m_vol = re.search(r"Volume:\s*(\d{4}-\d{2})", line_clean)
        if m_vol:
            current_vol = m_vol.group(1)
            continue
        m_exp = re.search(r"Expected Total:\s*(\d+)", line_clean)
        if m_exp and current_vol:
            expectations[current_vol] = int(m_exp.group(1))

    return expectations


def print_table(year: int, data_file: Path, flags_file: Path, expectations: dict[str, int]):
    if not data_file.exists():
        print(f"Data file not found for year {year}: {data_file}")
        return

    data = json.loads(data_file.read_text(encoding="utf-8"))
    flags_data = json.loads(flags_file.read_text(encoding="utf-8")) if flags_file.exists() else {}

    records = data.get("records", [])
    flagged_entries = flags_data.get("flagged_entries", [])

    counts = Counter(r["source"]["identifier"] for r in records)
    flags_count = Counter(f["source"]["identifier"] for f in flagged_entries)

    print(f"\n=========================================================================")
    print(f"                EXTRACTION BREAKDOWN TABLE - YEAR {year}                 ")
    print(f"=========================================================================")
    print(f"{'Issue / Month':<14} | {'Volume & Issue':<16} | {'Found':>6} | {'Exp':>6} | {'Flags':>6} | {'Status':<14}")
    print(f"-------------------------------------------------------------------------")

    total_found = 0
    total_exp = 0
    total_flags = 0

    all_keys = sorted(counts.keys())
    if not all_keys:
        print("No records found in dataset.")
        return

    for k in all_keys:
        meta_parts = k.split("_")
        # e.g. sim_american-kennel-club-stud-book-register_1929-01-01_46_1
        # parts: [0:sim, 1:american-kennel-club-stud-book-register, 2:1929-01-01, 3:46, 4:1]
        date_str = ""
        vol_num = ""
        iss_num = ""
        for p in meta_parts:
            if re.match(r"^\d{4}-\d{2}", p):
                date_str = p
            elif date_str and not vol_num and p.isdigit():
                vol_num = p
            elif vol_num and not iss_num and p.isdigit():
                iss_num = p

        m_match = re.search(r"(\d{4}-\d{2})", date_str)
        ym = m_match.group(1) if m_match else date_str[:7]

        found = counts[k]
        exp = expectations.get(ym)
        flg = flags_count.get(k, 0)

        total_found += found
        total_flags += flg

        exp_str = str(exp) if exp is not None else "-"
        if exp is not None:
            total_exp += exp
            diff = found - exp
            status = "100% MATCH" if diff == 0 else f"{diff:+d}"
        else:
            status = "UNCHECKED"

        vol_iss = f"Vol {vol_num}, Iss {iss_num}" if vol_num and iss_num else "-"
        print(f"{ym:<14} | {vol_iss:<16} | {found:>6d} | {exp_str:>6} | {flg:>6d} | {status:<14}")

    print(f"-------------------------------------------------------------------------")
    exp_summary = str(total_exp) if total_exp > 0 else "-"
    tot_diff = total_found - total_exp if total_exp > 0 else 0
    tot_status = "100% MATCH" if (total_exp > 0 and tot_diff == 0) else (f"{tot_diff:+d}" if total_exp > 0 else "-")
    print(f"{'TOTAL':<14} | {'':<16} | {total_found:>6d} | {exp_summary:>6} | {total_flags:>6d} | {tot_status:<14}")
    print(f"=========================================================================\n")


def main():
    parser = argparse.ArgumentParser(description="Report breakdown table of dogs extracted per volume.")
    parser.add_argument("--year", type=int, default=1929, help="Year to report (e.g. 1929)")
    parser.add_argument("--all", action="store_true", help="Report all available years")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    parser.add_argument("--template", type=Path, default=Path(__file__).resolve().parent / "template.yml")
    args = parser.parse_args()

    expectations = parse_template(args.template)

    if args.all:
        for json_file in sorted(args.data_dir.glob("autonomous_dobermans_*.json")):
            m = re.search(r"(\d{4})", json_file.name)
            if m:
                yr = int(m.group(1))
                flags_file = args.data_dir / f"review_flags_{yr}.json"
                print_table(yr, json_file, flags_file, expectations)
    else:
        json_file = args.data_dir / f"autonomous_dobermans_{args.year}.json"
        flags_file = args.data_dir / f"review_flags_{args.year}.json"
        print_table(args.year, json_file, flags_file, expectations)


if __name__ == "__main__":
    main()
