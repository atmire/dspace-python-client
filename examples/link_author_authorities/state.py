"""Checkpoint and incremental attempt-state persistence."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from urllib.parse import urlparse

STATE_ENV_VAR = "LINK_AUTHOR_STATE_FILE"
DEFAULT_STATE_FILENAME = "link_author_authorities_state.jsonl"
CHECKPOINT_ENV_VAR = "LINK_AUTHOR_REPO_CHECKPOINT_FILE"
DEFAULT_CHECKPOINT_FILENAME = "link_author_authorities_repo_checkpoint.json"
AUTO_CHUNK_THRESHOLD_ENV_VAR = "LINK_AUTHOR_AUTO_CHUNK_THRESHOLD"
DEFAULT_AUTO_CHUNK_THRESHOLD = 2000
DEFAULT_AUTO_CHUNK_SIZE = 1000


def _repo_key_from_base_url(base_url: str) -> str:
    """Build a filesystem-safe repository key from the DSpace base URL host."""
    host = (urlparse(base_url).hostname or "").strip().lower()
    if not host:
        return "unknown-host"
    # Keep simple and deterministic; replace non-safe chars with underscore.
    return re.sub(r"[^a-z0-9.-]+", "_", host)


def _get_state_path(log_dir: str, base_url: str) -> str:
    """Compute path for the incremental state file."""
    override = os.environ.get(STATE_ENV_VAR)
    if override:
        return override
    repo_key = _repo_key_from_base_url(base_url)
    return os.path.join(log_dir, f"link_author_authorities_state_{repo_key}.jsonl")


def _get_checkpoint_path(log_dir: str, base_url: str) -> str:
    """Compute path for repository-mode pagination checkpoint file."""
    override = os.environ.get(CHECKPOINT_ENV_VAR)
    if override:
        return override
    repo_key = _repo_key_from_base_url(base_url)
    return os.path.join(log_dir, f"link_author_authorities_repo_checkpoint_{repo_key}.json")


def _load_repo_checkpoint(path: str) -> dict:
    """Load repository-mode checkpoint from disk (best effort)."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        return {}
    return {}


def _save_repo_checkpoint(path: str, data: dict) -> None:
    """Persist repository-mode checkpoint atomically (best effort)."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        return


def _clear_repo_checkpoint(path: str) -> None:
    """Remove repository-mode checkpoint when a full pass completes."""
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        return


def _load_attempt_state(path: str) -> dict[str, datetime]:
    """Load last-attempt timestamps per item UUID from a JSONL state file."""
    state: dict[str, datetime] = {}
    if not os.path.exists(path):
        return state
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                uuid = rec.get("uuid")
                ts = rec.get("last_attempt")
                if not uuid or not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(ts)
                except Exception:
                    continue
                state[uuid] = dt
    except OSError:
        return state
    return state


def _append_attempt_state(path: str, item_uuid: str, when: datetime) -> None:
    """Append a single attempt record to the state file."""
    rec = {"uuid": item_uuid, "last_attempt": when.isoformat()}
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        # Best-effort only; do not fail the whole run if we can't write state
        return


def _should_process_uuid(
    item_uuid: str,
    mode: str,
    state: dict[str, datetime],
    now: datetime,
    min_age_days: int | None,
) -> bool:
    """
    Decide whether to process a given item UUID based on incremental state.

    Modes:
    - "new": only items never seen before.
    - "since": items never seen OR last attempt at least `min_age_days` ago.
    - "force": always process.
    """
    if mode == "force":
        return True
    last = state.get(item_uuid)
    if last is None:
        return True  # new in both "new" and "since" modes
    if mode == "new":
        return False
    if mode == "since" and min_age_days is not None:
        delta = now - last
        return delta.days >= min_age_days
    return True
