"""Append-only dataset journal with periodic compacted snapshots."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable


class DatasetJournal:
    def __init__(self, snapshot_path: Path, journal_path: Path | None = None) -> None:
        self.snapshot_path = snapshot_path
        self.journal_path = journal_path or snapshot_path.with_suffix(".jsonl")

    def replay(self, records: dict[str, dict[str, Any]]) -> int:
        if not self.journal_path.exists():
            return 0
        events = 0
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            key = str(event["key"])
            if event["op"] == "upsert":
                records[key] = event["record"]
            elif event["op"] == "delete":
                records.pop(key, None)
            events += 1
        return events

    def upsert(self, key: str, record: dict[str, Any]) -> None:
        self._append({"op": "upsert", "key": key, "record": record})

    def delete(self, key: str) -> None:
        self._append({"op": "delete", "key": key})

    def compact(self, save_snapshot: Callable[[], None]) -> None:
        save_snapshot()
        self.journal_path.write_text("", encoding="utf-8")

    def _append(self, event: dict[str, Any]) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as journal:
            journal.write(json.dumps(event, sort_keys=True) + "\n")
            journal.flush()
            os.fsync(journal.fileno())
