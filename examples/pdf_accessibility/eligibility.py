"""
Which date decides whether an item counts as pre-existing content.

1. dc.date.issued, if present and a valid ISO-8601 date (YYYY, YYYY-MM, YYYY-MM-DD, or a
   timestamp);
2. otherwise dc.date.accessioned.

A partial date is read as the latest day it could mean: "2027" is 2027-12-31 and "2027-04"
is 2027-04-30. An ambiguous date therefore never qualifies for an exception by accident.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

_DATE_RE = re.compile(
    r"^(?P<y>\d{4})(?:-(?P<m>\d{2})(?:-(?P<d>\d{2})(?P<time>[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)?)?)?$"
)


def latest_date(value: str) -> date | None:
    """The latest calendar date a DSpace date value can mean, or None if it is not valid."""
    match = _DATE_RE.match(value.strip())
    if not match:
        return None
    year = int(match["y"])
    try:
        if match["time"]:
            text = value.strip().replace(" ", "T").replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(UTC)
            return parsed.date()
        if match["d"]:
            return date(year, int(match["m"]), int(match["d"]))
        if match["m"]:
            month = int(match["m"])
            return date(year, month, calendar.monthrange(year, month)[1])
        return date(year, 12, 31)
    except ValueError:  # month 13, February 30, ...
        return None


@dataclass(frozen=True)
class Basis:
    field: str
    value: str
    date: date


def _values(metadata: dict[str, list[dict[str, Any]]], field: str) -> list[str]:
    return [str(v.get("value") or "") for v in metadata.get(field) or []]


def eligibility_basis(metadata: dict[str, list[dict[str, Any]]]) -> Basis | None:
    """The date (and where it came from) that decides eligibility, or None if there is none."""
    for value in _values(metadata, "dc.date.issued"):
        parsed = latest_date(value)
        if parsed:
            return Basis("dc.date.issued", value, parsed)
    for value in _values(metadata, "dc.date.accessioned"):
        parsed = latest_date(value)
        if parsed:
            return Basis("dc.date.accessioned", value, parsed)
    return None


def parse_cutoff(value: str) -> date:
    """A cutoff entered by the user must be a full YYYY-MM-DD date."""
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a date in YYYY-MM-DD form") from exc
