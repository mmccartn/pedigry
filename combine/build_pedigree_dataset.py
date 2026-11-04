#!/usr/bin/env python3
"""Compile a compact root-agnostic AKC/StudBook/DobeQuest pedigree dataset."""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRAPER_DIR = ROOT / "akc-scrape"
STUDBOOK_DIR = ROOT / "stud-scrape" / "data"
sys.path.insert(0, str(SCRAPER_DIR))

from akc_titles import PREFIX_TITLES, SUFFIX_TITLES


def log(message: str) -> None:
    print(message, flush=True)


def load_akc_records(path: Path) -> dict[str, dict[str, Any]]:
    """Load versioned or legacy scraper records."""
    log(f"Loading AKC scraper records from {path}...")
    with path.open(encoding="utf-8") as source:
        data = json.load(source)
    records = data.get("records", data)
    log(f"Loaded {len(records):,} AKC records.")
    return records


def load_studbook_records(patterns: list[str]) -> dict[str, dict[str, Any]]:
    """Load and index all available Stud Book extraction JSON files."""
    matched_files: list[Path] = []
    for pat in patterns:
        matched_files.extend(Path(p) for p in glob.glob(pat))

    matched_files = sorted(set(matched_files))
    if not matched_files:
        log("No Stud Book record files matched.")
        return {}

    log(f"Loading Stud Book records from {len(matched_files)} files...")
    studbook_by_number: dict[str, dict[str, Any]] = {}
    for p in matched_files:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            recs = d.get("records", [])
            for r in recs:
                reg = re.sub(r"[^\d]", "", str(r.get("registration_number") or ""))
                if reg and len(reg) >= 5:
                    studbook_by_number[reg] = r
        except Exception as err:
            log(f"Warning: could not read {p}: {err}")

    log(f"Loaded {len(studbook_by_number):,} authoritative Stud Book records.")
    return studbook_by_number


def split_titles(name: str) -> tuple[str, list[str]]:
    """Separate recognized prefix and suffix titles from a displayed name."""
    words = name.split()
    prefix: list[str] = []
    suffix: list[str] = []
    while len(words) > 1 and words[0].casefold() in PREFIX_TITLES:
        prefix.append(words.pop(0))
    while len(words) > 1 and words[-1].casefold() in SUFFIX_TITLES:
        suffix.insert(0, words.pop())
    return " ".join(words), prefix + suffix


def compact_akc_node(node_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Create a compact site node from an AKC scraper record."""
    name, titles = split_titles(str(record.get("name") or ""))
    return {
        "name": name,
        "titles": titles,
        "registration_number": record.get("akc_number") or None,
        "dobequest_id": record.get("dobequest_id") or None,
        "birth_date": record.get("birth_date") or None,
        "registration_type": record.get("registration_type") or None,
        "sex": record.get("sex") or None,
        "sire": None,
        "dam": None,
        "primary_source": "akc" if record.get("akc_number") else "dobequest" if record.get("dobequest_id") else "akc",
    }


def compact_studbook_node(node_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Create a compact site node from an authoritative AKC Stud Book record."""
    name, titles = split_titles(str(record.get("name") or ""))
    reg = re.sub(r"[^\d]", "", str(record.get("registration_number") or ""))
    return {
        "name": name,
        "titles": titles,
        "registration_number": reg or None,
        "dobequest_id": None,
        "birth_date": record.get("birth_date") or None,
        "registration_type": "AKC",
        "sex": record.get("sex") or None,
        "sire": None,
        "dam": None,
        "primary_source": "akc",
    }


def compact_dobequest_node(profile: dict[str, Any]) -> dict[str, Any]:
    """Create a compact site node from a DobeQuest profile."""
    titles = [part for part in (profile.get("titles"), profile.get("suffix")) if part]
    registration = profile.get("akcNum") or profile.get("otherNum") or profile.get("ckcNum") or None
    registration_type = "AKC" if profile.get("akcNum") else "DobeQuest"
    birth_date = profile.get("dateOfBirth") or profile.get("whelpDate") or None
    return {
        "name": profile.get("registeredName") or None,
        "titles": titles,
        "registration_number": registration,
        "dobequest_id": str(profile.get("did") or "") or None,
        "birth_date": birth_date,
        "registration_type": registration_type,
        "sex": profile.get("sex") or None,
        "sire": None,
        "dam": None,
        "primary_source": "dobequest",
    }


def infer_sex_from_parent_roles(nodes: dict[str, dict[str, Any]]) -> None:
    """Infer a missing sex only when a dog is used in an unambiguous parent role."""
    inferred: dict[str, set[str]] = {}
    for node in nodes.values():
        for role, sex in (("sire", "Male"), ("dam", "Female")):
            parent_id = node.get(role)
            if parent_id and not nodes.get(parent_id, {}).get("sex"):
                inferred.setdefault(parent_id, set()).add(sex)
    for node_id, candidates in inferred.items():
        if len(candidates) == 1:
            nodes[node_id]["sex"] = candidates.pop()


def build_dataset(
    akc_records: dict[str, dict[str, Any]],
    dobequest: dict[str, dict[str, Any]],
    studbook_records: dict[str, dict[str, Any]] | None = None,
    root_dog: str | None = None,
    infer_sex: bool = False,
) -> dict[str, Any]:
    """Expand AKC scraper, Stud Book, and DobeQuest records through available parent trees."""
    log("Building multi-source identity and parent lookup indexes...")
    studbook = studbook_records or {}

    # Fast indexed lookups:
    akc_by_number = {
        str(record["akc_number"]): key
        for key, record in akc_records.items()
        if record.get("akc_number")
    }
    akc_by_dobequest = {
        str(record["dobequest_id"]): key
        for key, record in akc_records.items()
        if record.get("dobequest_id")
    }

    # Pre-index DobeQuest by AKC number for instant O(1) lookups:
    dq_by_akc_number: dict[str, str] = {}
    for did, p in dobequest.items():
        akc_num = re.sub(r"[^\d]", "", str(p.get("akcNum") or ""))
        if akc_num and len(akc_num) >= 5:
            dq_by_akc_number[akc_num] = did

    def dq_node_id(did: str) -> str | None:
        profile = dobequest.get(did)
        if not profile:
            return None
        akc_num = re.sub(r"[^\d]", "", str(profile.get("akcNum") or ""))
        if akc_num and akc_num in akc_by_number:
            return akc_by_number[akc_num]
        if did in akc_by_dobequest:
            return akc_by_dobequest[did]
        if akc_num and akc_num in studbook:
            return f"akc:{akc_num}"
        return f"dobequest:{did}"

    nodes: dict[str, dict[str, Any]] = {}

    if root_dog:
        normalized_root = root_dog.casefold()
        root_matches: list[str] = []
        for key, record in akc_records.items():
            if str(record.get("name") or "").casefold() == normalized_root or \
               normalized_root in {str(a).casefold() for a in record.get("names", {}).get("aliases", [])}:
                root_matches.append(key)

        if not root_matches:
            # Check Stud Book
            for reg, s_rec in studbook.items():
                if str(s_rec.get("name") or "").casefold() == normalized_root:
                    root_matches.append(f"akc:{reg}")

        if not root_matches:
            # Check DobeQuest
            for did, p in dobequest.items():
                if str(p.get("registeredName") or "").casefold() == normalized_root:
                    root_matches.append(dq_node_id(did) or f"dobequest:{did}")

        if len(root_matches) != 1:
            raise ValueError(f"Expected one root match for {root_dog!r}, found {len(root_matches)}.")
        queue = deque(root_matches)
        log(f"Expanding root-only ancestry graph for {root_dog!r}...")
    else:
        # Seed with all AKC scraper records + all Stud Book records
        seed_keys = list(akc_records.keys())
        for reg in studbook:
            sb_key = f"akc:{reg}"
            if reg not in akc_by_number:
                seed_keys.append(sb_key)
        queue = deque(seed_keys)
        log(f"Expanding root-agnostic graph from {len(seed_keys):,} total seed records (AKC + Stud Book)...")

    visited: set[str] = set()

    while queue:
        node_id = queue.popleft()
        if node_id in visited:
            continue
        visited.add(node_id)
        if len(visited) % 5_000 == 0:
            log(f"Expanded {len(visited):,} graph nodes; {len(queue):,} queued; {len(nodes):,} emitted.")

        # 1. Check if node is an AKC scraper record
        akc_record = akc_records.get(node_id)
        if akc_record:
            node = compact_akc_node(node_id, akc_record)
            reg_clean = re.sub(r"[^\d]", "", str(akc_record.get("akc_number") or ""))

            # Cross-reference DobeQuest profile
            profile = None
            did = str(akc_record.get("dobequest_id") or "")
            if did and did in dobequest:
                profile = dobequest[did]
            elif reg_clean and reg_clean in dq_by_akc_number:
                profile = dobequest[dq_by_akc_number[reg_clean]]

            # Expand parents
            for role in ("sire", "dam"):
                parent = akc_record.get("parents", {}).get(role, {})
                target = parent.get("id")
                if target and (target in akc_records or target.startswith("akc:")):
                    node[role] = target
                    queue.append(target)
                    continue

                parent_reg = re.sub(r"[^\d]", "", str(parent.get("akc_number") or ""))
                if parent_reg and parent_reg in studbook:
                    target = f"akc:{parent_reg}"
                    node[role] = target
                    queue.append(target)
                    continue

                parent_did = str(parent.get("dobequest_id") or "")
                if not parent_did and profile:
                    parent_did = str((profile.get(role) or {}).get("did") or "")
                target = dq_node_id(parent_did) if parent_did else None
                if target:
                    node[role] = target
                    queue.append(target)

            nodes[node_id] = node
            continue

        # 2. Check if node is an authoritative Stud Book record
        reg_id = node_id.partition("akc:")[2] if node_id.startswith("akc:") else node_id
        if reg_id in studbook:
            s_rec = studbook[reg_id]
            node = compact_studbook_node(node_id, s_rec)

            # Check if DobeQuest profile matches
            if reg_id in dq_by_akc_number:
                node["dobequest_id"] = dq_by_akc_number[reg_id]

            # Expand sire & dam
            for role, role_reg_key, role_name_key in (("sire", "sire_registration_number", "sire"), ("dam", "dam_registration_number", "dam")):
                p_reg = re.sub(r"[^\d]", "", str(s_rec.get(role_reg_key) or ""))
                target = None
                if p_reg:
                    if p_reg in akc_by_number:
                        target = akc_by_number[p_reg]
                    elif p_reg in studbook:
                        target = f"akc:{p_reg}"
                    elif p_reg in dq_by_akc_number:
                        target = dq_node_id(dq_by_akc_number[p_reg])

                if not target:
                    # Name lookup in DobeQuest if parent registration missing
                    p_name = str(s_rec.get(role_name_key) or "").strip().casefold()
                    if p_name:
                        # Match exact name in DobeQuest
                        for c_did, c_prof in dobequest.items():
                            if str(c_prof.get("registeredName") or "").casefold() == p_name:
                                target = dq_node_id(c_did)
                                break

                if target:
                    node[role] = target
                    queue.append(target)

            nodes[node_id] = node
            continue

        # 3. Check if node is a DobeQuest profile
        if node_id.startswith("dobequest:"):
            did = node_id.partition(":")[2]
            profile = dobequest.get(did)
            if not profile:
                continue
            node = compact_dobequest_node(profile)

            # Check if Stud Book has official registration
            dq_akc = re.sub(r"[^\d]", "", str(profile.get("akcNum") or ""))
            if dq_akc and dq_akc in studbook:
                sb_match = studbook[dq_akc]
                node["registration_number"] = dq_akc
                node["birth_date"] = node["birth_date"] or sb_match.get("birth_date")

            for role in ("sire", "dam"):
                parent_did = str((profile.get(role) or {}).get("did") or "")
                target = dq_node_id(parent_did) if parent_did else None
                if target:
                    node[role] = target
                    queue.append(target)
            nodes[node_id] = node

    log(f"Graph expansion complete: {len(nodes):,} nodes emitted.")
    if infer_sex:
        log("Inferring missing sex values from unambiguous parent roles...")
        infer_sex_from_parent_roles(nodes)

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "nodes": nodes,
        "stats": {
            "akc_seed_records": len(akc_records),
            "studbook_records": len(studbook),
            "total_nodes": len(nodes),
            "dobequest_only_nodes": sum(node_id.startswith("dobequest:") for node_id in nodes),
        },
    }


def main() -> int:
    """Compile the site-oriented pedigree dataset without changing source files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--akc-data", type=Path, default=SCRAPER_DIR / "dogs.json")
    parser.add_argument("--dobequest-data", type=Path, default=SCRAPER_DIR / "dobe-quest_2026.json")
    parser.add_argument("--studbook-data", nargs="*", default=[str(STUDBOOK_DIR / "autonomous_dobermans_*.json")],
                        help="Optional Stud Book JSON files or glob patterns.")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("pedigree_dataset.json"))
    parser.add_argument("--root-dog", help="Compile only this root dog (AKC/StudBook/DobeQuest) and reachable ancestors.")
    parser.add_argument("--infer-sex", action="store_true", help="Infer missing sex from unambiguous sire/dam parent roles.")
    args = parser.parse_args()

    akc_records = load_akc_records(args.akc_data)
    studbook_records = load_studbook_records(args.studbook_data)

    log(f"Loading DobeQuest profiles from {args.dobequest_data}...")
    with args.dobequest_data.open(encoding="utf-8") as source:
        dobequest = json.load(source)
    log(f"Loaded {len(dobequest):,} DobeQuest profiles.")

    dataset = build_dataset(akc_records, dobequest, studbook_records, args.root_dog, args.infer_sex)
    log(f"Writing compact dataset to {args.output}...")
    args.output.write_text(json.dumps(dataset, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
    log("Dataset build complete.")
    print(json.dumps(dataset["stats"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
