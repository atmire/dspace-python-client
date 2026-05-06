"""Unit tests for the pure helpers in examples/realtime_REF.py.

These tests avoid all I/O — they cover the bucketing logic and date parsing
that drive the REF posture report. The example script is loaded by file path
because it lives under ``examples/`` (not on the package import path).
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest


def _load_realtime_ref():
    path = Path(__file__).resolve().parents[1] / "examples" / "realtime_REF.py"
    spec = importlib.util.spec_from_file_location("realtime_REF", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def rr():
    return _load_realtime_ref()


# --- get_metadata_value ----------------------------------------------------


def test_get_metadata_value_single_and_multivalued(rr):
    md = {
        "dc.title": [{"value": "Hello"}],
        "dc.subject": [{"value": "Open Access"}, {"value": "REF"}],
        "dc.empty": [],
    }
    assert rr.get_metadata_value(md, "dc.title") == "Hello"
    assert rr.get_metadata_value(md, "dc.subject") == "Open Access || REF"
    assert rr.get_metadata_value(md, "dc.empty") == ""
    assert rr.get_metadata_value(md, "dc.missing") == ""


# --- parse_issued_year / parse_issued_date ---------------------------------


def test_parse_issued_year_yyyy_yyyymm_yyyymmdd_invalid(rr):
    assert rr.parse_issued_year("2024") == 2024
    assert rr.parse_issued_year("2024-05") == 2024
    assert rr.parse_issued_year("2024-05-12") == 2024
    assert rr.parse_issued_year("not-a-date") is None
    assert rr.parse_issued_year("") is None
    assert rr.parse_issued_year(None) is None  # type: ignore[arg-type]


def test_parse_issued_date_year_only_falls_back_to_jan_1(rr):
    assert rr.parse_issued_date("2024") == date(2024, 1, 1)
    assert rr.parse_issued_date("2024-07") == date(2024, 7, 1)
    assert rr.parse_issued_date("2024-07-15") == date(2024, 7, 15)
    assert rr.parse_issued_date("garbage") is None
    assert rr.parse_issued_date("") is None


# --- regime_for_year -------------------------------------------------------


def test_regime_for_year_boundaries(rr):
    assert rr.regime_for_year(2020) is None
    assert rr.regime_for_year(2021) == "2021-2025"
    assert rr.regime_for_year(2025) == "2021-2025"
    assert rr.regime_for_year(2026) == "2026-2028"
    assert rr.regime_for_year(2028) == "2026-2028"
    assert rr.regime_for_year(2029) is None
    assert rr.regime_for_year(None) is None


# --- embargo_months_between ------------------------------------------------


def test_embargo_months_between_simple(rr):
    # Roughly 6 months -> ~6.0 ± 0.1
    issued = date(2024, 1, 1)
    end = date(2024, 7, 1)
    assert abs(rr.embargo_months_between(issued, end) - 6.0) < 0.2

    # Roughly 12 months -> ~12.0 ± 0.2
    end_year = date(2025, 1, 1)
    assert abs(rr.embargo_months_between(issued, end_year) - 12.0) < 0.2


# --- classify_item ---------------------------------------------------------


def _common(**overrides):
    """Build a classify_item kwargs dict with sensible defaults; override per test."""
    base = {
        "issued": date(2024, 1, 1),
        "issued_year": 2024,
        "has_original_deposit": True,
        "access_status": "open.access",
        "embargo_end": None,
    }
    base.update(overrides)
    return base


def test_classify_open_access_eligible_all(rr):
    bucket, notes = rr.classify_item(**_common(access_status="open.access"))
    assert bucket == "eligible_all_panels"
    assert "open access" in notes


def test_classify_no_deposit(rr):
    bucket, _ = rr.classify_item(**_common(has_original_deposit=False))
    assert bucket == "not_eligible_no_deposit"


def test_classify_metadata_only_no_open_access(rr):
    bucket, _ = rr.classify_item(**_common(access_status="metadata.only"))
    assert bucket == "not_eligible_no_open_access"
    bucket, _ = rr.classify_item(**_common(access_status="restricted"))
    assert bucket == "not_eligible_no_open_access"


def test_classify_2024_embargo_10mo_eligible_all(rr):
    # 2021-2025 regime: A/B max = 12m. 10m -> all panels.
    bucket, _ = rr.classify_item(
        **_common(
            issued=date(2024, 1, 1),
            issued_year=2024,
            access_status="embargo",
            embargo_end=date(2024, 11, 1),
        )
    )
    assert bucket == "eligible_all_panels"


def test_classify_2024_embargo_15mo_eligible_cd_only(rr):
    # 2021-2025: > 12m A/B but ≤ 24m C/D.
    bucket, _ = rr.classify_item(
        **_common(
            issued=date(2024, 1, 1),
            issued_year=2024,
            access_status="embargo",
            embargo_end=date(2025, 4, 1),  # ~15 months
        )
    )
    assert bucket == "eligible_cd_only"


def test_classify_2024_embargo_30mo_not_eligible(rr):
    bucket, _ = rr.classify_item(
        **_common(
            issued=date(2024, 1, 1),
            issued_year=2024,
            access_status="embargo",
            embargo_end=date(2026, 7, 1),  # ~30 months
        )
    )
    assert bucket == "not_eligible_embargo_too_long"


def test_classify_2026_embargo_10mo_eligible_cd_only(rr):
    # 2026-2028 regime: A/B = 6m, C/D = 12m. 10m -> C/D only.
    bucket, _ = rr.classify_item(
        **_common(
            issued=date(2026, 1, 1),
            issued_year=2026,
            access_status="embargo",
            embargo_end=date(2026, 11, 1),
        )
    )
    assert bucket == "eligible_cd_only"


def test_classify_2026_embargo_4mo_eligible_all(rr):
    # 2026-2028 regime: 4m fits within A/B 6m.
    bucket, _ = rr.classify_item(
        **_common(
            issued=date(2026, 1, 1),
            issued_year=2026,
            access_status="embargo",
            embargo_end=date(2026, 5, 1),
        )
    )
    assert bucket == "eligible_all_panels"


def test_classify_unknown_year(rr):
    bucket, _ = rr.classify_item(
        **_common(issued=None, issued_year=None, has_original_deposit=True)
    )
    assert bucket == "unknown_no_issued_date"


def test_classify_year_outside_regime_2030(rr):
    bucket, notes = rr.classify_item(
        **_common(
            issued=date(2030, 1, 1),
            issued_year=2030,
            access_status="embargo",
            embargo_end=date(2030, 7, 1),
        )
    )
    assert bucket == "unknown_other"
    assert "2030" in notes


# --- derive_access_from_conditions / extract_conditions_from_bitstream -----


def test_derive_open_access_condition(rr):
    conditions = [{"name": "openAccess", "startDate": None, "endDate": None}]
    assert rr.derive_access_from_conditions(conditions) == ("open.access", None)


def test_derive_embargo_condition_uses_start_date_as_lift(rr):
    # The user's wlv example: name=embargo, startDate=2027-02-09 is the lift date.
    conditions = [
        {"name": "embargo", "startDate": "2027-02-09", "endDate": None},
    ]
    status, lift = rr.derive_access_from_conditions(conditions)
    assert status == "embargo"
    assert lift == date(2027, 2, 9)


def test_derive_picks_earliest_embargo_lift(rr):
    # Two embargo policies: pick the earliest lift date (most permissive).
    conditions = [
        {"name": "embargo", "startDate": "2028-06-01"},
        {"name": "embargo", "startDate": "2027-02-09"},
        {"name": "embargo", "startDate": "2030-12-31"},
    ]
    status, lift = rr.derive_access_from_conditions(conditions)
    assert status == "embargo"
    assert lift == date(2027, 2, 9)


def test_derive_open_access_wins_over_embargo(rr):
    # If openAccess is present, ignore any embargo policy.
    conditions = [
        {"name": "openAccess", "startDate": None},
        {"name": "embargo", "startDate": "2030-01-01"},
    ]
    assert rr.derive_access_from_conditions(conditions) == ("open.access", None)


def test_derive_administrator_only_is_restricted(rr):
    conditions = [{"name": "administrator", "startDate": None, "endDate": None}]
    assert rr.derive_access_from_conditions(conditions) == ("restricted", None)


def test_derive_embargo_without_start_date_is_restricted(rr):
    # Defensive: an embargo policy with no startDate gives us no lift to use,
    # and there's no openAccess fallback, so treat as restricted.
    conditions = [{"name": "embargo", "startDate": None}]
    assert rr.derive_access_from_conditions(conditions) == ("restricted", None)


def test_derive_empty_or_missing_returns_unknown(rr):
    # Empty list -> unknown so the caller can fall back to /accessStatus.
    assert rr.derive_access_from_conditions([]) == (None, None)


def test_extract_conditions_from_bitstream_full_path(rr):
    # Mirrors the wlv response shape verbatim.
    bs = {
        "_embedded": {
            "accessConditions": {
                "_embedded": {
                    "accessConditions": [
                        {
                            "name": "embargo",
                            "startDate": "2027-02-09",
                            "endDate": None,
                            "type": "accessCondition",
                        }
                    ]
                },
            },
        },
    }
    conditions = rr.extract_conditions_from_bitstream(bs)
    assert len(conditions) == 1
    assert conditions[0]["name"] == "embargo"
    assert conditions[0]["startDate"] == "2027-02-09"


def test_extract_conditions_from_bitstream_missing_embed(rr):
    # No _embedded.accessConditions at all (e.g. embed wasn't requested).
    assert rr.extract_conditions_from_bitstream({"uuid": "abc"}) == []
    assert rr.extract_conditions_from_bitstream({"_embedded": {}}) == []
    assert rr.extract_conditions_from_bitstream({"_embedded": {"accessConditions": {}}}) == []
