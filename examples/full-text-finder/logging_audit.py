"""Timestamped audit log for full-text finder (DSpace mutations and outcomes)."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional, TextIO


LOG_ENV_DIR = "FULLTEXT_FINDER_LOG_DIR"
LOG_FILENAME_PREFIX = "full_text_finder_"


def open_audit_log() -> tuple[Optional[TextIO], Optional[str]]:
    """
    Open a new timestamped log file for writing UTF-8 lines.

    Returns (file handle or None, path or None).
    """
    log_dir = os.environ.get(LOG_ENV_DIR, ".").strip() or "."
    log_filename = datetime.now().strftime(f"{LOG_FILENAME_PREFIX}%Y-%m-%d_%H-%M-%S.log")
    log_path = os.path.join(log_dir, log_filename)
    try:
        f = open(log_path, "w", encoding="utf-8")
    except OSError:
        return None, None
    return f, log_path


def log_line(log_file: Optional[TextIO], line: str) -> None:
    """Write one line with ISO-like local timestamp and flush."""
    if log_file is None:
        return
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    log_file.write(f"{ts} {line}\n")
    log_file.flush()


__all__ = ["LOG_ENV_DIR", "LOG_FILENAME_PREFIX", "open_audit_log", "log_line"]
