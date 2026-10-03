"""
Severity policy: which findings count, and how much.

outloud gives every rule a severity (error, warning, info) and maps it to WCAG 2.2 success
criteria and a Matterhorn checkpoint. The policy sits on top of that, so an institution can be
stricter or more lenient without touching outloud. See policy_default.yml for the format.

Resolution order for one rule:

1. an explicit ``rules:`` entry wins outright;
2. otherwise a ``matterhorn:`` checkpoint entry wins;
3. otherwise start from outloud's severity, cap semantic (heuristic) rules at
   ``layers.semantic``, and downgrade to ``no_wcag_mapping`` when none of the rule's WCAG
   criteria belong to the chosen standard.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SEVERITIES = ("ignore", "info", "warning", "error")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}

STANDARDS = ("wcag21-aa", "wcag22-aa", "pdfua1")

# Level A/AA success criteria that are new in WCAG 2.2 (AAA ones are listed too for safety;
# outloud only maps A/AA). These do not count under the wcag21-aa standard.
WCAG22_ONLY = frozenset(
    {"2.4.11", "2.4.12", "2.4.13", "2.5.7", "2.5.8", "3.2.6", "3.3.7", "3.3.8", "3.3.9"}
)

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "policy_default.yml"


class PolicyError(ValueError):
    """The policy file is malformed."""


@dataclass(frozen=True)
class RuleInfo:
    """What the policy needs to know about one rule (from outloud's catalogue, or ours)."""

    id: str
    title: str
    layer: str
    severity: str
    matterhorn: str | None = None
    wcag: tuple[str, ...] = ()
    fix: str | None = None


@dataclass
class Policy:
    standard: str = "wcag21-aa"
    no_wcag_mapping: str = "info"
    layers: dict[str, str] = field(default_factory=dict)
    matterhorn: dict[str, str] = field(default_factory=dict)
    rules: dict[str, str] = field(default_factory=dict)
    fail_on: str = "error"
    review_on: str = "warning"
    hash: str = ""

    def criterion_counts(self, criterion: str) -> bool:
        """Does this WCAG success criterion belong to the chosen standard?"""
        if self.standard == "wcag21-aa":
            return criterion not in WCAG22_ONLY
        return True

    def effective_severity(self, rule: RuleInfo) -> str:
        """The severity this policy gives a rule: error, warning, info or ignore."""
        if rule.id in self.rules:
            return self.rules[rule.id]
        if rule.matterhorn and rule.matterhorn in self.matterhorn:
            return self.matterhorn[rule.matterhorn]
        severity = rule.severity if rule.severity in _RANK else "warning"
        cap = self.layers.get(rule.layer)
        if cap is not None:
            severity = _lower(severity, cap)
        if self.standard != "pdfua1" and not any(self.criterion_counts(c) for c in rule.wcag):
            severity = _lower(severity, self.no_wcag_mapping)
        return severity


def _lower(a: str, b: str) -> str:
    return a if _RANK[a] <= _RANK[b] else b


def _check_severity(value: Any, where: str) -> str:
    if value not in _RANK:
        raise PolicyError(f"{where}: {value!r} is not one of {', '.join(SEVERITIES)}")
    return str(value)


def load_policy(path: str | Path | None = None) -> Policy:
    """Load and validate a policy file (defaults to policy_default.yml next to this module)."""
    policy_path = Path(path) if path else DEFAULT_POLICY_PATH
    try:
        data = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"Cannot read policy file {policy_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError(f"{policy_path}: expected a mapping at the top level")

    standard = data.get("standard", "wcag21-aa")
    if standard not in STANDARDS:
        raise PolicyError(f"standard: {standard!r} is not one of {', '.join(STANDARDS)}")
    inherit = data.get("inherit", "outloud")
    if inherit != "outloud":
        raise PolicyError("inherit: only 'outloud' is supported")

    def mapping(key: str) -> dict[str, str]:
        raw = data.get(key) or {}
        if not isinstance(raw, dict):
            raise PolicyError(f"{key}: expected a mapping")
        return {str(k): _check_severity(v, f"{key}.{k}") for k, v in raw.items()}

    layers = mapping("layers")
    for layer in layers:
        if layer not in ("conformance", "semantic"):
            raise PolicyError(f"layers.{layer}: layer must be 'conformance' or 'semantic'")
    # Matterhorn checkpoints are two-digit strings; YAML turns `07:` into the int 7.
    matterhorn = {k.zfill(2): v for k, v in mapping("matterhorn").items()}

    verdict = data.get("verdict") or {}
    policy = Policy(
        standard=standard,
        no_wcag_mapping=_check_severity(data.get("no_wcag_mapping", "info"), "no_wcag_mapping"),
        layers=layers,
        matterhorn=matterhorn,
        rules=mapping("rules"),
        fail_on=_check_severity(verdict.get("fail_on", "error"), "verdict.fail_on"),
        review_on=_check_severity(verdict.get("review_on", "warning"), "verdict.review_on"),
    )
    canonical = json.dumps(
        {
            "standard": policy.standard,
            "no_wcag_mapping": policy.no_wcag_mapping,
            "layers": policy.layers,
            "matterhorn": policy.matterhorn,
            "rules": policy.rules,
            "fail_on": policy.fail_on,
            "review_on": policy.review_on,
        },
        sort_keys=True,
    )
    policy.hash = hashlib.sha256(canonical.encode()).hexdigest()
    return policy


def severity_at_least(severity: str, threshold: str) -> bool:
    return _RANK[severity] >= _RANK[threshold]
