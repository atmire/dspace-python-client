"""
results.jsonl: every check result, append-only, one JSON object per line.

The file is the contract between detection and remediation, and it is the cache that lets
a re-run skip unchanged files. The latest line for a bitstream wins. A result is reused only
when the bitstream's checksum, the outloud version and the policy hash all match, so a new
file version, an outloud upgrade or a policy change all trigger a fresh check.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_REPORT_DIR = Path("pdf-a11y-reports")

# Never reused from the cache: the check did not finish, or the outcome depends on a run
# option (--max-size-mb) rather than on the file.
RETRY_VERDICTS = frozenset({"CHECK_FAILED", "DOWNLOAD_FAILED", "SKIPPED_TOO_LARGE"})


def host_slug(base_url: str) -> str:
    """'https://repo.example.org:8443/server' -> 'repo.example.org-8443'."""
    netloc = urlparse(base_url if "://" in base_url else f"https://{base_url}").netloc
    return re.sub(r"[^A-Za-z0-9.-]+", "-", netloc).strip("-") or "dspace"


def timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def results_path(report_dir: Path, base_url: str) -> Path:
    return report_dir / f"{host_slug(base_url)}-results.jsonl"


class ResultStore:
    """Read and append results.jsonl; keeps the latest record per bitstream in memory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.latest: dict[str, dict[str, Any]] = {}
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        # A run killed mid-write leaves a partial last line; skip it.
                        print(f"Skipping unreadable line {line_no} in {path}")
                        continue
                    if record.get("bitstream_uuid"):
                        self.latest[record["bitstream_uuid"]] = record

    def cached(
        self,
        bitstream_uuid: str,
        checksum: str | None,
        engine_version: str,
        policy_hash: str,
    ) -> dict[str, Any] | None:
        """The stored record if it is still valid for this file, engine and policy."""
        record = self.latest.get(bitstream_uuid)
        if not record or not checksum:
            return None
        if (
            record.get("checksum") == checksum
            and record.get("engine_version") == engine_version
            and record.get("policy_hash") == policy_hash
            and record.get("verdict") not in RETRY_VERDICTS
        ):
            return record
        return None

    def append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        if record.get("bitstream_uuid"):
            self.latest[record["bitstream_uuid"]] = record

    def records(self) -> Iterable[dict[str, Any]]:
        return self.latest.values()
