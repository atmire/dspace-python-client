"""
Pair a remediated PDF with the original it replaces, within one item, conservatively.

Originals are PDFs that detection found wanting (FAIL/REVIEW). Candidates are PDFs that were
not in the item when detection ran. File names are compared after dropping the extension,
case and common "fixed version" suffixes (_accessible, -remediated, _ua, _v2, (1), ...).

A pair is made only when the match is one-to-one. As a fallback, an item with exactly one
original and one candidate is paired even when the names differ. Everything else is
reported as unpaired; nothing is guessed.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

_SUFFIX_WORDS = (
    "accessible",
    "accessibility",
    "remediated",
    "tagged",
    "pdfua",
    "ua",
    "fixed",
    "final",
    "new",
    "copy",
    r"v\d+",
)
# A suffix word only counts after a separator, so "nicaragua" keeps its "ua".
_SUFFIX_RE = re.compile(
    r"(?:[\s_.-]+(?:" + "|".join(_SUFFIX_WORDS) + r")|\s*\(\d+\))$",
    re.IGNORECASE,
)


def normalize_name(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    stem = stem.strip().lower()
    previous = None
    while previous != stem:
        previous = stem
        stem = _SUFFIX_RE.sub("", stem).strip(" _-.")
    return stem


@dataclass(frozen=True)
class File:
    uuid: str
    name: str


@dataclass
class PairingResult:
    pairs: list[tuple[File, File]]  # (original, replacement)
    unpaired_originals: list[File]
    unpaired_candidates: list[File]


def pair_files(originals: list[File], candidates: list[File]) -> PairingResult:
    by_name_orig: dict[str, list[File]] = defaultdict(list)
    by_name_cand: dict[str, list[File]] = defaultdict(list)
    for f in originals:
        by_name_orig[normalize_name(f.name)].append(f)
    for f in candidates:
        by_name_cand[normalize_name(f.name)].append(f)

    pairs: list[tuple[File, File]] = []
    for key, origs in by_name_orig.items():
        cands = by_name_cand.get(key, [])
        if len(origs) == 1 and len(cands) == 1:
            pairs.append((origs[0], cands[0]))

    if not pairs and len(originals) == 1 and len(candidates) == 1:
        pairs.append((originals[0], candidates[0]))

    paired_o = {o.uuid for o, _ in pairs}
    paired_c = {c.uuid for _, c in pairs}
    return PairingResult(
        pairs=pairs,
        unpaired_originals=[f for f in originals if f.uuid not in paired_o],
        unpaired_candidates=[f for f in candidates if f.uuid not in paired_c],
    )
