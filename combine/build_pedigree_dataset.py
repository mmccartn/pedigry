#!/usr/bin/env python3
"""Compile a compact root-agnostic AKC/DobeQuest pedigree dataset."""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRAPER_DIR = ROOT / "akc-scrape"
sys.path.insert(0, str(SCRAPER_DIR))

from akc_titles import PREFIX_TITLES, SUFFIX_TITLES


def load_akc_records(path: Path) -> dict[str, dict[str, Any]]:
    """Load versioned or legacy scraper records."""
    with path.open(encoding="utf-8") as source:
        data = json.load(source)
    return data.get("records", data)


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
        "birth_date": record.get("birth_date") or None,
        "registration_type": record.get("registration_type") or None,
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
    return {
        "name": profile.get("registeredName") or None,
        "titles": titles,
        "registration_number": registration,
        "birth_date": None,
        "registration_type": registration_type,
        "sex": profile.get("sex") or None,
        "sire": None,
        "dam": None,
        "primary_source": "dobequest",
    }


def build_dataset(akc_records: dict[str, dict[str, Any]], dobequest: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Expand every AKC seed record through available DobeQuest parent trees."""
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

    def dq_node_id(did: str) -> str | None:
        profile = dobequest.get(did)
        if not profile:
            return None
        akc_number = str(profile.get("akcNum") or "")
        return akc_by_number.get(akc_number) or akc_by_dobequest.get(did) or f"dobequest:{did}"

    nodes: dict[str, dict[str, Any]] = {}
    queue = deque(akc_records)
    visited: set[str] = set()

    while queue:
        node_id = queue.popleft()
        if node_id in visited:
            continue
        visited.add(node_id)
        akc_record = akc_records.get(node_id)

        if akc_record:
            node = compact_akc_node(node_id, akc_record)
            profile = dobequest.get(str(akc_record.get("dobequest_id") or ""))
            if not profile and akc_record.get("akc_number"):
                profile = next(
                    (candidate for candidate in dobequest.values() if candidate.get("akcNum") == akc_record["akc_number"]),
                    None,
                )
            for role in ("sire", "dam"):
                parent = akc_record.get("parents", {}).get(role, {})
                target = parent.get("id")
                if target and target in akc_records:
                    node[role] = target
                    queue.append(target)
                    continue
                did = str(parent.get("dobequest_id") or "")
                if not did and profile:
                    did = str((profile.get(role) or {}).get("did") or "")
                target = dq_node_id(did) if did else None
                if target:
                    node[role] = target
                    queue.append(target)
            nodes[node_id] = node
            continue

        if not node_id.startswith("dobequest:"):
            continue
        did = node_id.partition(":")[2]
        profile = dobequest.get(did)
        if not profile:
            continue
        node = compact_dobequest_node(profile)
        for role in ("sire", "dam"):
            parent_did = str((profile.get(role) or {}).get("did") or "")
            target = dq_node_id(parent_did) if parent_did else None
            if target:
                node[role] = target
                queue.append(target)
        nodes[node_id] = node

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "nodes": nodes,
        "stats": {
            "akc_seed_records": len(akc_records),
            "total_nodes": len(nodes),
            "dobequest_only_nodes": sum(node_id.startswith("dobequest:") for node_id in nodes),
        },
    }


def main() -> int:
    """Compile the site-oriented pedigree dataset without changing source files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--akc-data", type=Path, default=SCRAPER_DIR / "dogs copy.json")
    parser.add_argument("--dobequest-data", type=Path, default=SCRAPER_DIR / "dobe-quest_2026.json")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("pedigree_dataset.json"))
    args = parser.parse_args()

    akc_records = load_akc_records(args.akc_data)
    with args.dobequest_data.open(encoding="utf-8") as source:
        dobequest = json.load(source)
    dataset = build_dataset(akc_records, dobequest)
    args.output.write_text(json.dumps(dataset, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(dataset["stats"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
