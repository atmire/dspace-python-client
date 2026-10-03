"""Render an Analysis as the plain-text report used in the CSV and in item metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .analysis import Analysis, Finding
from .policy import Policy

DEFAULT_OVERRIDES_PATH = Path(__file__).resolve().parent / "advice_overrides.yml"

MAX_PAGES_SHOWN = 20
MAX_MANUAL_SHOWN = 4


@dataclass(frozen=True)
class Advice:
    fix: str | None
    who: str | None


def load_overrides(path: str | Path | None = None) -> dict[str, Advice]:
    data = yaml.safe_load(Path(path or DEFAULT_OVERRIDES_PATH).read_text(encoding="utf-8")) or {}
    return {
        str(rule): Advice(fix=(entry or {}).get("fix"), who=(entry or {}).get("who"))
        for rule, entry in data.items()
    }


def format_pages(pages: list[int]) -> str:
    """[1, 2, 3, 7, 9, 10] -> 'pages 1-3, 7, 9-10'; capped at MAX_PAGES_SHOWN page numbers."""
    if not pages:
        return "whole document"
    shown = pages[:MAX_PAGES_SHOWN]
    ranges: list[str] = []
    start = prev = shown[0]
    for page in shown[1:]:
        if page == prev + 1:
            prev = page
            continue
        ranges.append(f"{start}-{prev}" if prev != start else str(start))
        start = prev = page
    ranges.append(f"{start}-{prev}" if prev != start else str(start))
    text = ", ".join(ranges)
    if len(pages) > MAX_PAGES_SHOWN:
        text += f", … ({len(pages)} pages in total)"
    return ("page " if len(pages) == 1 else "pages ") + text


def _summarise_crashes(crashed: list[str], shown: int = 5) -> str:
    more = len(crashed) - shown
    return "; ".join(crashed[:shown]) + (f"; and {more} more" if more > 0 else "")


def _finding_lines(finding: Finding, overrides: dict[str, Advice]) -> list[str]:
    override = overrides.get(finding.rule)
    where = format_pages(finding.pages)
    times = f" ({finding.count}×)" if finding.count > 1 else ""
    wcag = f" · WCAG {', '.join(finding.wcag)}" if finding.wcag else ""
    head = f"[{finding.severity.upper()}] {finding.title} ({finding.rule}): {where}{times}{wcag}"
    lines = [head]
    detail = finding.message
    if finding.evidence:
        detail += f' (e.g. "{finding.evidence}")'
    if detail:
        lines.append(f"  Found: {detail}")
    fix = finding.fix
    if override and override.fix:
        # Keep the "First run OCR" prefix analysis.py adds for mostly-scanned files.
        fix = (
            f"First run OCR (see OCR-001). Then: {override.fix}"
            if fix and fix.startswith("First run OCR") and finding.rule != "OCR-001"
            else override.fix
        )
    if fix:
        lines.append(f"  Fix: {fix}")
    if override and override.who:
        lines.append(f"  Who: {override.who}")
    return lines


def render_report(
    analysis: Analysis,
    policy: Policy,
    overrides: dict[str, Advice],
    checked_on: str,
) -> str:
    """The full report for one bitstream."""
    engine = ", ".join(f"{k} {v}" for k, v in sorted(analysis.engine.items())) or "outloud"
    stamp = f"{engine} · policy {policy.standard}/{policy.hash[:6]} · {checked_on}"

    if analysis.verdict == "UNREADABLE":
        return (
            f"UNREADABLE · {stamp}\n"
            f"The file could not be opened: {analysis.error}\n"
            "  Fix: check that the file opens in a PDF reader. If it is password-protected or "
            "encrypted against reading, upload an unprotected copy."
        )
    if analysis.verdict == "CHECK_FAILED":
        return (
            f"CHECK_FAILED · {stamp}\n"
            f"The accessibility check did not finish ({analysis.error}). Nothing is known about "
            "this file yet; re-run the check, possibly with a longer --timeout."
        )

    counts = f"{analysis.errors} error(s), {analysis.warnings} warning(s)"
    if analysis.infos:
        counts += f", {analysis.infos} note(s)"
    lines = [f"{analysis.verdict}: {counts} · {stamp}"]
    if analysis.crashed_rules:
        lines.append(
            f"Some checks did not complete ({_summarise_crashes(analysis.crashed_rules)}), so this "
            "result may be missing problems."
        )
    for finding in analysis.findings:
        lines.extend(_finding_lines(finding, overrides))
    if analysis.manual_criteria:
        shown = ", ".join(analysis.manual_criteria[:MAX_MANUAL_SHOWN])
        more = len(analysis.manual_criteria) - MAX_MANUAL_SHOWN
        tail = f" and {more} more" if more > 0 else ""
        lines.append(
            f"Needs a person: {len(analysis.manual_criteria)} WCAG criteria cannot be checked "
            f"automatically ({shown}{tail}), e.g. colour contrast and a meaningful reading order."
        )
    if analysis.verdict == "PASS":
        lines.append(
            "No machine-detectable problems. This does not make the file fully accessible on its own."
        )
    return "\n".join(lines)


def report_for_record(record: dict[str, Any], policy: Policy, overrides: dict[str, Advice]) -> str:
    """The report for one stored results.jsonl record.

    Re-rendered from the stored analysis each time, so edits to advice_overrides.yml also
    apply to cached results.
    """
    checked_on = str(record.get("checked_at") or "")[:10]
    if record.get("analysis"):
        return render_report(Analysis.from_dict(record["analysis"]), policy, overrides, checked_on)
    return f"{record['verdict']} · {checked_on}\n{record.get('error') or ''}".strip()
