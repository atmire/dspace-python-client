"""Open PDF in OS viewer and Rich prompts."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from rich.console import Console


def open_pdf_in_viewer(path: Path, console: Console) -> None:
    """Open a file with the platform default application."""
    p = str(path.resolve())
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", p], check=False)
        elif os.name == "nt":
            os.startfile(p)  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", p], check=False)
    except Exception as e:
        console.print(f"[yellow]Could not open viewer: {e}[/yellow]")


def prompt_upload(console: Console) -> str:
    """
    Returns 'upload', 'skip', or 'quit'.
    """
    s = console.input(
        "[bold cyan]Upload this file to DSpace?[/bold cyan] "
        "[dim]([Y]es / [N]o / [Q]uit):[/dim] "
    ).strip().lower()
    if s in ("q", "quit"):
        return "quit"
    if s in ("n", "no", ""):
        return "skip"
    return "upload"


def write_temp_pdf(data: bytes, prefix: str = "fulltext_") -> Path:
    fd, name = tempfile.mkstemp(suffix=".pdf", prefix=prefix)
    os.close(fd)
    path = Path(name)
    path.write_bytes(data)
    return path


__all__ = ["open_pdf_in_viewer", "prompt_upload", "write_temp_pdf"]
