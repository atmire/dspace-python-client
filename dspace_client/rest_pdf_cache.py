"""Persistent cache for REST-based PDF bitstream count (item UUID -> has_pdf).

Assumes items are immutable after creation: once we have established whether an item
has a PDF, we trust that until a forced rerun. One CSV per repository.
"""

import csv
import hashlib
import re
import urllib.parse
from pathlib import Path


def _normalize_base_url(base_url: str) -> str:
    """Normalize base URL for stable cache key."""
    url = (base_url or "").strip().lower()
    if not url:
        return ""
    return url.rstrip("/")


def _repository_cache_id(base_url: str) -> str:
    """Return a safe filename fragment for the repository (stable per base_url)."""
    normalized = _normalize_base_url(base_url)
    if not normalized:
        return "default"
    parsed = urllib.parse.urlparse(normalized)
    netloc = parsed.netloc or parsed.path or normalized
    safe = re.sub(r"[^a-z0-9.-]", "_", netloc)
    if parsed.path and parsed.path != "/":
        # Not a security primitive: this only disambiguates cache filenames per
        # repository path, so an explicitly non-security digest is correct here.
        path_hash = hashlib.md5(parsed.path.encode(), usedforsecurity=False).hexdigest()[:8]
        safe = f"{safe}_{path_hash}"
    return safe or "default"


class RestPDFCountCache:
    """Persistent cache of item UUID -> has_pdf for REST-based PDF counting.

    One CSV per repository. Items are assumed immutable: cached entries are trusted
    until a forced rerun revisits everything.
    """

    CACHE_FILENAME_PREFIX = "rest_pdf_cache_"

    def __init__(
        self,
        base_url: str,
        cache_dir: Path | None = None,
    ):
        self.base_url = _normalize_base_url(base_url)
        self._repo_id = _repository_cache_id(self.base_url)
        self._cache_dir = (
            Path(cache_dir) if cache_dir else Path.home() / ".cache" / "dspace-rest-pdf"
        )
        self._cache_path = self._cache_dir / f"{self.CACHE_FILENAME_PREFIX}{self._repo_id}.csv"
        self._data: dict[str, bool] = {}  # item_uuid -> has_pdf

    def _ensure_dir(self) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        """Load cache from CSV."""
        self._data = {}
        if self._cache_path.exists():
            try:
                with self._cache_path.open(newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        uid = (row.get("item_uuid") or "").strip()
                        if not uid:
                            continue
                        has_pdf = (row.get("has_pdf") or "0").strip().lower() in (
                            "1",
                            "true",
                            "yes",
                        )
                        self._data[uid] = has_pdf
            except (csv.Error, OSError):
                pass

    def get(self, item_uuid: str) -> bool | None:
        """Return cached has_pdf for item_uuid, or None if not in cache."""
        return self._data.get(item_uuid)

    def update(self, item_uuid: str, has_pdf: bool) -> None:
        """Update or insert cache entry for item_uuid."""
        self._data[item_uuid] = has_pdf

    def save(self) -> None:
        """Write cache to CSV atomically."""
        self._ensure_dir()
        tmp_path = self._cache_path.with_suffix(".csv.tmp")
        with tmp_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["item_uuid", "has_pdf"])
            writer.writeheader()
            for uid, has_pdf in self._data.items():
                writer.writerow({"item_uuid": uid, "has_pdf": "1" if has_pdf else "0"})
        tmp_path.replace(self._cache_path)

    def totals(self) -> tuple[int, int]:
        """Return (total_count, with_pdf_count) from current in-memory cache."""
        total = len(self._data)
        with_pdf = sum(1 for v in self._data.values() if v)
        return total, with_pdf

    @property
    def cache_path(self) -> Path:
        return self._cache_path
