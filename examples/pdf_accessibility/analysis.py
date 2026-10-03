"""
Turn a raw check result (outloud + OCR-001) into findings and a verdict under a policy.

Verdicts:
    PASS               no effective error or warning
    REVIEW             warnings only
    FAIL               at least one effective error
    INCOMPLETE         no errors found, but at least one rule crashed, so a pass cannot be claimed
    UNREADABLE         the file could not be opened (corrupt, password-protected, not a PDF)
    CHECK_FAILED       the check itself did not finish (timeout, worker crash)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .policy import Policy, RuleInfo, severity_at_least

OCR_RULE = RuleInfo(
    id="OCR-001",
    title="Pages are images with no text layer",
    layer="conformance",
    severity="error",
    matterhorn="01",
    wcag=("1.1.1", "1.4.5"),
    fix="Run OCR (for example OCRmyPDF) to add a text layer, then tag the document.",
)

# Rules whose advice ("tag the document") cannot be followed before OCR has been done.
_NEEDS_OCR_FIRST = frozenset({"DOC-001", "DOC-002", "TAG-001"})

# A file is "mostly scanned" when at least this share of its pages is image-only.
MOSTLY_SCANNED_SHARE = 0.5


def load_rule_catalogue() -> dict[str, RuleInfo]:
    """outloud's rule catalogue as RuleInfo objects, plus our OCR-001."""
    from outloud.registry import load_catalogue

    rules = {
        meta.id: RuleInfo(
            id=meta.id,
            title=meta.title,
            layer=meta.layer,
            severity=meta.severity,
            matterhorn=meta.matterhorn,
            wcag=tuple(meta.wcag),
            fix=meta.fix,
        )
        for meta in load_catalogue().values()
    }
    rules[OCR_RULE.id] = OCR_RULE
    return rules


@dataclass
class Finding:
    rule: str
    title: str
    severity: str  # effective, after the policy
    engine_severity: str  # what outloud (or OCR-001) said
    layer: str
    message: str
    pages: list[int] = field(default_factory=list)
    count: int = 1
    evidence: str | None = None
    wcag: list[str] = field(default_factory=list)
    matterhorn: str | None = None
    fix: str | None = None


@dataclass
class Analysis:
    verdict: str
    errors: int = 0
    warnings: int = 0
    infos: int = 0
    pages: int = 0
    findings: list[Finding] = field(default_factory=list)
    manual_criteria: list[str] = field(default_factory=list)
    crashed_rules: list[str] = field(default_factory=list)
    error: str | None = None
    engine: dict[str, str] = field(default_factory=dict)
    mostly_scanned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Analysis:
        findings = [Finding(**f) for f in data.get("findings", [])]
        rest = {k: v for k, v in data.items() if k != "findings" and k in cls.__dataclass_fields__}
        return cls(findings=findings, **rest)


def _finding_pages(raw_finding: dict[str, Any]) -> list[int]:
    pages = {b["page"] for b in raw_finding.get("boxes", []) if b.get("page")}
    if raw_finding.get("page"):
        pages.add(raw_finding["page"])
    return sorted(pages)


def _sort_key(f: Finding) -> tuple[int, int, int, str]:
    rank = {"error": 0, "warning": 1, "info": 2}
    return (
        0 if f.rule == OCR_RULE.id else 1,
        rank.get(f.severity, 3),
        f.pages[0] if f.pages else 0,
        f.rule,
    )


def failed_check(status: str, error: str | None) -> Analysis:
    """The Analysis for a check that did not produce a result (timeout or worker crash)."""
    return Analysis(verdict="CHECK_FAILED", error=f"{status}: {error}" if error else status)


def analyse(raw: dict[str, Any], policy: Policy, catalogue: dict[str, RuleInfo]) -> Analysis:
    """Apply the policy to one raw check result."""
    engine = dict(raw.get("engine") or {})
    pages = int((raw.get("stats") or {}).get("pages") or 0)
    if raw.get("error") and not pages:
        return Analysis(verdict="UNREADABLE", error=raw["error"], engine=engine, pages=pages)
    # outloud opened the file but stopped part-way (a bug in outloud, not a broken file).
    # The findings it made before stopping still count; the file can no longer PASS.
    engine_error = f"outloud stopped part-way: {raw['error']}" if raw.get("error") else None

    findings: list[Finding] = []

    ocr = raw.get("ocr") or {}
    ocr_pages = list(ocr.get("pages") or [])
    page_count = int(ocr.get("page_count") or pages or 0)
    mostly_scanned = (
        bool(ocr_pages) and page_count > 0 and len(ocr_pages) / page_count >= MOSTLY_SCANNED_SHARE
    )
    if ocr_pages:
        severity = policy.effective_severity(OCR_RULE)
        if severity != "ignore":
            findings.append(
                Finding(
                    rule=OCR_RULE.id,
                    title=OCR_RULE.title,
                    severity=severity,
                    engine_severity=OCR_RULE.severity,
                    layer=OCR_RULE.layer,
                    message=f"{len(ocr_pages)} of {page_count} page(s) are images with no text a reader can get at",
                    pages=ocr_pages,
                    count=len(ocr_pages),
                    wcag=list(OCR_RULE.wcag),
                    matterhorn=OCR_RULE.matterhorn,
                    fix=OCR_RULE.fix,
                )
            )

    for raw_finding in raw.get("findings") or []:
        rule_id = raw_finding["rule"]
        info = catalogue.get(rule_id) or RuleInfo(
            id=rule_id,
            title=rule_id,
            layer="conformance",
            severity=raw_finding.get("severity", "warning"),
        )
        # outloud can lower a single finding below its rule's catalogue severity (e.g. a
        # warning for untagged drawn paths); keep the lower of the two as the starting point.
        engine_severity = raw_finding.get("severity", info.severity)
        if engine_severity != info.severity:
            info = RuleInfo(
                id=info.id,
                title=info.title,
                layer=info.layer,
                severity=engine_severity,
                matterhorn=info.matterhorn,
                wcag=info.wcag,
                fix=info.fix,
            )
        severity = policy.effective_severity(info)
        if severity == "ignore":
            continue
        fix = info.fix
        if mostly_scanned and rule_id in _NEEDS_OCR_FIRST:
            fix = (
                f"First run OCR (see OCR-001). Then: {fix}"
                if fix
                else "First run OCR (see OCR-001)."
            )
        findings.append(
            Finding(
                rule=rule_id,
                title=info.title,
                severity=severity,
                engine_severity=engine_severity,
                layer=info.layer,
                message=raw_finding.get("message", ""),
                pages=_finding_pages(raw_finding),
                count=int(raw_finding.get("count", 1)),
                evidence=raw_finding.get("evidence"),
                wcag=list(info.wcag),
                matterhorn=info.matterhorn,
                fix=fix,
            )
        )

    findings.sort(key=_sort_key)
    errors = sum(1 for f in findings if f.severity == "error")
    warnings = sum(1 for f in findings if f.severity == "warning")
    infos = sum(1 for f in findings if f.severity == "info")
    crashed = list(raw.get("crashed_rules") or [])
    if engine_error:
        crashed.append(engine_error)

    if any(severity_at_least(f.severity, policy.fail_on) for f in findings):
        verdict = "FAIL"
    elif crashed:
        verdict = "INCOMPLETE"
    elif any(severity_at_least(f.severity, policy.review_on) for f in findings):
        verdict = "REVIEW"
    else:
        verdict = "PASS"

    manual = [
        row["id"]
        for row in (raw.get("criteria") or {}).get("wcag22", [])
        if row.get("status") == "manual" and policy.criterion_counts(row["id"])
    ]

    return Analysis(
        verdict=verdict,
        errors=errors,
        warnings=warnings,
        infos=infos,
        pages=pages,
        findings=findings,
        manual_criteria=manual,
        crashed_rules=crashed,
        engine=engine,
        mostly_scanned=mostly_scanned,
    )
