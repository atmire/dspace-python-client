"""Author name normalization and fuzzy matching."""

from __future__ import annotations

import unicodedata

AUTHOR_FIELD = "dc.contributor.author"


def _strip_accents(s: str) -> str:
    """Remove diacritics from a string while preserving base characters."""
    if not s:
        return ""
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


def normalize_name(s: str) -> str:
    """Normalize author name for exact match (strip spaces, ignore accents, lowercase)."""
    if not s:
        return ""
    # Collapse whitespace, then strip accents and lowercase for accent-insensitive matching
    collapsed = " ".join(s.split())
    no_accents = _strip_accents(collapsed)
    return no_accents.lower()


def _parse_family_first(name: str) -> tuple[str, str]:
    """Split 'Family, First' into (family, first). If no comma, return (normalized, '')."""
    n = normalize_name(name)
    if not n:
        return ("", "")
    if "," in n:
        parts = n.split(",", 1)
        return (normalize_name(parts[0]), normalize_name(parts[1]))
    return (n, "")


def _initials(s: str) -> str:
    """Get initials from a name part, e.g. 'Jane Marie' -> 'J M', 'John' -> 'J'."""
    if not s:
        return ""
    return " ".join(w[0] for w in s.split() if w).upper()


def _normalize_initials(s: str) -> str:
    """Normalize an initials string for comparison: 'J. M.' -> 'J M', 'J.M.' -> 'J M'."""
    if not s:
        return ""
    # Strip accents, remove periods and collapse spaces, then rejoin with single space
    base = _strip_accents(s)
    return " ".join(base.replace(".", " ").split()).upper()


def _family_name_compact(family: str) -> str:
    """Family name with internal spaces removed, e.g. 'de eyto' -> 'deeyto'."""
    return normalize_name(family).replace(" ", "")


def _families_match(item_family: str, auth_family: str) -> bool:
    """True if family names match exactly or differ only by internal spaces."""
    if item_family.lower() == auth_family.lower():
        return True
    return _family_name_compact(item_family) == _family_name_compact(auth_family)


def _first_names_match_exact(item_first: str, auth_first: str) -> bool:
    return normalize_name(item_first).lower() == normalize_name(auth_first).lower()


def _first_names_match_initials(item_first: str, auth_first: str) -> bool:
    return _normalize_initials(item_first) == _initials(auth_first)


def author_search_variants(authority_display_name: str) -> list[str]:
    """
    Build discovery author-contains filter terms for alternate metadata spellings.

    From e.g. "de Eyto, Elvira" also searches "DeEyto", "de Eyto, E", "DeEyto, E.",
    and the family name alone.
    """
    seen: set[str] = set()
    out: list[str] = []

    def add(value: str) -> None:
        v = value.strip()
        if not v:
            return
        key = v.casefold()
        if key in seen:
            return
        seen.add(key)
        out.append(v)

    raw = authority_display_name.strip()
    if not raw:
        return out

    add(raw)
    if "," not in raw:
        compact = "".join(raw.split())
        if compact.casefold() != raw.casefold():
            add(compact)
        return out

    raw_family, raw_first = (part.strip() for part in raw.split(",", 1))
    family_compact = "".join(raw_family.split())
    families = [raw_family]
    if family_compact.casefold() != raw_family.casefold():
        families.append(family_compact)
        add(family_compact)
        if raw_first:
            add(f"{family_compact}, {raw_first}")

    add(raw_family)

    initials = _initials(normalize_name(raw_first)) if raw_first else ""
    if initials:
        letter = initials.split()[0]
        for family in families:
            add(f"{family}, {letter}")
            add(f"{family}, {letter}.")

    return out


def _item_family_first_variants(item_author: str) -> list[tuple[str, str]]:
    """
    (family, first) interpretations for an item author string.

    Comma-separated values try both Family, First (DSpace/Solr style) and First, Family
    (common in free-text metadata), deduped when identical.
    """
    n = normalize_name(item_author)
    if not n:
        return []
    if "," not in n:
        return [_parse_family_first(item_author)]
    parts = n.split(",", 1)
    left, right = normalize_name(parts[0]), normalize_name(parts[1])
    std = (left, right)
    swp = (right, left)
    if std == swp:
        return [std]
    return [std, swp]


def _match_family_first_parts(
    item_family: str,
    item_first: str,
    auth_family: str,
    auth_first: str,
) -> bool:
    """True if item (family, first) matches authority (family, first); allows initials on item."""
    if not item_family or not auth_family:
        return False
    if not item_first and not auth_first:
        return _families_match(item_family, auth_family)
    if not item_first:
        return _families_match(item_family, auth_family)
    if not auth_first:
        return False
    if _first_names_match_exact(item_first, auth_first):
        return _families_match(item_family, auth_family)
    if _first_names_match_initials(item_first, auth_first):
        return _families_match(item_family, auth_family)
    return False


def fuzzy_match_author(item_author: str, authority_name: str) -> bool:
    """
    Return True if item_author matches authority_name allowing abbreviated first names.

    E.g. "Smith, J." matches "Smith, John"; "Doe, J. M." matches "Doe, Jane Marie".
    Authority display names are parsed as Family, First. Item strings with a comma also
    try First, Family so "Bert, Bogaerts" matches "Bogaerts, Bert". Family name must match
    after normalize, or with internal spaces removed (e.g. "DeEyto" matches "de Eyto").
    First name matches if exact or item's first is initials of authority's given name.
    """
    auth_family, auth_first = _parse_family_first(authority_name)
    if not auth_family:
        return False
    for item_family, item_first in _item_family_first_variants(item_author):
        if _match_family_first_parts(item_family, item_first, auth_family, auth_first):
            return True
    return False


def get_unlinked_authors(metadata: dict) -> list[tuple[int, dict]]:
    """Return list of (index, value_obj) for dc.contributor.author where authority is null."""
    entries = metadata.get(AUTHOR_FIELD) or []
    result = []
    for i, obj in enumerate(entries):
        if not isinstance(obj, dict):
            continue
        authority = obj.get("authority") if obj else None
        if authority is None or (isinstance(authority, str) and authority.strip() == ""):
            result.append((i, obj))
    return result
