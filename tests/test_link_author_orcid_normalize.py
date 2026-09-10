"""Tests for ORCID input parsing in link_author_authorities example."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LAA = Path(__file__).resolve().parents[1] / "examples" / "link_author_authorities"
if str(_LAA) not in sys.path:
    sys.path.insert(0, str(_LAA))

from orcid import (
    extract_orcid_from_entry,
    normalize_orcid_identifier,
    orcid_hyphenated_from_compact,
)
from scoring import author_search_variants, fuzzy_match_author


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0000-0002-1825-0097", "0000000218250097"),
        ("0000-0002-1825-009X", "000000021825009X"),
        ("0000-0002-1825-009x", "000000021825009X"),
        ("https://orcid.org/0000-0002-1825-0097", "0000000218250097"),
        ("https://www.orcid.org/0000-0002-1825-009X", "000000021825009X"),
        ("http://ORCID.org/0000-0001-2345-6789", "0000000123456789"),
        ("orcid.org/0000-0001-2345-6789", "0000000123456789"),
        ("www.orcid.org/0000-0001-2345-6789", "0000000123456789"),
        ("https://orcid.org/0000-0001-2345-6789?lang=en", "0000000123456789"),
        ("0000000123456789", "0000000123456789"),
        ("See https://www.orcid.org/0000-0001-2345-6789 for profile", "0000000123456789"),
    ],
)
def test_normalize_orcid_success(raw, expected):
    assert normalize_orcid_identifier(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not-an-orcid",
        "0000-0001-2345",  # too short
        "0000-0001-2345-678",  # too short
    ],
)
def test_normalize_orcid_rejects_invalid(raw):
    assert normalize_orcid_identifier(raw) is None


def test_normalize_rejects_bad_checksum_position():
    assert normalize_orcid_identifier("0000-0002-1825-X097") is None


def test_orcid_hyphenated_from_compact():
    assert orcid_hyphenated_from_compact("0000000168849316") == "0000-0001-6884-9316"
    assert orcid_hyphenated_from_compact("000000021825009X") == "0000-0002-1825-009X"
    assert orcid_hyphenated_from_compact("bad") is None


def test_extract_orcid_person_identifier():
    entry = {
        "metadata": {
            "person.identifier.orcid": [{"value": "0000-0001-6884-9316"}],
        }
    }
    url = extract_orcid_from_entry(entry, None)
    assert normalize_orcid_identifier(url or "") == "0000000168849316"


def test_extract_orcid_person_identifier_compact_plain():
    entry = {"metadata": {"person.identifier.orcid": [{"value": "0000000168849316"}]}}
    url = extract_orcid_from_entry(entry, None)
    assert normalize_orcid_identifier(url or "") == "0000000168849316"


def test_extract_orcid_dc_identifier_uri():
    entry = {
        "metadata": {
            "dc.identifier.uri": [
                {"value": "https://orcid.org/0000-0001-6884-9316"},
            ],
        }
    }
    url = extract_orcid_from_entry(entry, None)
    assert normalize_orcid_identifier(url or "") == "0000000168849316"


def test_extract_orcid_detail_other_information_person():
    entry: dict = {"metadata": {}}
    detail = {
        "otherInformation": {
            "person.identifier.orcid": "0000-0001-6884-9316",
        }
    }
    url = extract_orcid_from_entry(entry, detail)
    assert normalize_orcid_identifier(url or "") == "0000000168849316"


@pytest.mark.parametrize(
    "item_author,authority_name,expected",
    [
        # Item metadata "Given, Family" vs vocabulary "Family, Given"
        ("Bert, Bogaerts", "Bogaerts, Bert", True),
        # Symmetric when both use same order
        ("Bogaerts, Bert", "Bogaerts, Bert", True),
        ("Bert, Bogaerts", "Bert, Bogaerts", True),
        # Regression: initials and exact first name (authority = Family, First)
        ("Smith, J.", "Smith, John", True),
        ("Smith, John", "Smith, John", True),
        ("Doe, J. M.", "Doe, Jane Marie", True),
        # Different people
        ("Smith, John", "Doe, Jane", False),
        ("Bert, Bogaerts", "Other, Person", False),
        # Deduped identical comma parts (single variant)
        ("Smith, Smith", "Smith, Smith", True),
        # Compound surname + initials (spacing variant only when matching on initial)
        ("De Eyto, E", "de Eyto, Elvira", True),
        ("de Eyto, E.", "de Eyto, Elvira", True),
        ("DeEyto, E", "de Eyto, Elvira", True),
        ("deEyto, E.", "de Eyto, Elvira", True),
        # Compound surname spacing variant with full first name
        ("DeEyto, Elvira", "de Eyto, Elvira", True),
        # Different initial / person
        ("DeEyto, M", "de Eyto, Elvira", False),
    ],
)
def test_fuzzy_match_author_name_order_and_regression(item_author, authority_name, expected):
    assert fuzzy_match_author(item_author, authority_name) is expected


def test_author_search_variants_de_eyto():
    variants = author_search_variants("de Eyto, Elvira")
    lowered = {v.casefold() for v in variants}
    assert "de eyto, elvira" in lowered
    assert "deeyto" in lowered
    assert "deeyto, elvira" in lowered
    assert "de eyto, e" in lowered
    assert "de eyto, e." in lowered
    assert "deeyto, e" in lowered
    assert "deeyto, e." in lowered
    assert "de eyto" in lowered
