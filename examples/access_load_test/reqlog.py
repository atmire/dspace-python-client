"""Bounded JSONL log of every request and action (for deep dives and for AI analysis)."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from access_load_test.metrics import ActionRecord, RequestRecord


class RequestLog:
    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.written_bytes = 0
        self.dropped = 0
        self.lines = 0
        self._fh: TextIO | None = path.open("w", encoding="utf-8")

    def _write(self, obj: dict) -> None:
        if self._fh is None:
            self.dropped += 1
            return
        line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"
        n = len(line.encode("utf-8"))
        if self.written_bytes + n > self.max_bytes:
            self.dropped += 1
            if self.dropped == 1:
                self._fh.write(
                    json.dumps({"type": "truncated", "reason": "requests_log_max_mb reached"})
                    + "\n"
                )
                self._fh.close()
                self._fh = None
            return
        self._fh.write(line)
        self.written_bytes += n
        self.lines += 1

    def write_request(self, rec: RequestRecord) -> None:
        d = asdict(rec)
        d["type"] = "request"
        d["ts_end_utc"] = datetime.fromtimestamp(rec.ts_end, tz=UTC).isoformat()
        self._write(d)

    def write_action(self, rec: ActionRecord) -> None:
        d = asdict(rec)
        d["type"] = "action"
        d["ts_end_utc"] = datetime.fromtimestamp(rec.ts_end, tz=UTC).isoformat()
        self._write(d)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def summary(self) -> dict:
        return {
            "path": str(self.path),
            "lines": self.lines,
            "bytes": self.written_bytes,
            "dropped_after_cap": self.dropped,
        }


__all__ = ["RequestLog"]
