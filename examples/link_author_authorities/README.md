# Link author authorities (`examples/link_author_authorities/`)

Interactive tool to link free-text `dc.contributor.author` values on DSpace items to **local** author authority records already in your repository's SOLR authority core (for example `CacheableAuthorAuthority` or `SolrAuthorAuthority`).

It does **not** query the public ORCID registry. Authorities must already exist in your vocabulary (typically because at least one item was linked manually before).

## Setup

From the repository root (with the project venv activated):

```bash
pip install -e "."
python examples/link_author_authorities.py
# Item mode with a UUID:
python examples/link_author_authorities.py <item-uuid>
```

On login failure (wrong password), the script prompts again up to three times before exiting with a clear message. Other example scripts can use ``prompt_and_authenticate`` from ``dspace_client`` the same way.

## What it does

For each **unlinked** author on an item (`authority` null or empty), the script tries to find a matching entry in the local vocabulary and PATCHes the item metadata with `authority` and `confidence` set.

Supported DSpace versions: **7.x, 8.x, 9.x, 10.x** (items PATCH + submission vocabularies).

## Interactive flow

1. DSpace base URL and credentials.
2. Vocabulary name (default: `CacheableAuthorAuthority`).
3. **Exact or Fuzzy** matching (see [Exact vs fuzzy](#exact-vs-fuzzy) below).
4. Whether to **auto-link** when there is exactly one vocabulary match (Item / Repository modes), or when ORCID/Name mode has a single fixed authority match (same prompt — answer **No** to confirm each link individually).
5. **Mode**: Item, Repository, ORCID, or Name.
6. A timestamped log file: `link_author_authorities_YYYY-MM-DD_HH-MM-SS.log` (override directory with `LINK_AUTHOR_LOG_DIR`).

Repository mode also supports checkpoint/resume via `link_author_authorities_state.jsonl` in the log directory.

## Modes

| Mode | What you provide | How items are found | How authorities are chosen |
|------|------------------|---------------------|----------------------------|
| **Item** | One item UUID | That item only | Vocabulary lookup per unlinked author (Exact or Fuzzy) |
| **Repository** | Run scope (new / since N days / force all) | Discovery API, newest first | Vocabulary lookup per unlinked author |
| **ORCID** | One ORCID id | Discovery API `author contains …` using [search variants](#discovery-search-variants-orcid-and-name-modes) derived from the resolved authority display name | Fixed authority from ORCID; item author must [fuzzy-match](#fuzzy-matching-rules) that display name. Respects **auto-link** setting (prompt per match when you answered No). |
| **Name** | Author name text; optional ORCID to link to | Discovery API with search variants from the name or resolved authority | Vocabulary lookup, or fixed ORCID authority if you supplied one (also respects **auto-link**) |

ORCID and Name modes use the Discovery Search API (`f.author=<value>,contains`). See `docs/dspace-rest-api/*/search-endpoint.md` in this repo.

### ORCID mode behaviour (important)

1. Resolve the ORCID to a local authority `(uuid, display_name)` — e.g. `"de Eyto, Elvira"`.
2. Discover items using multiple spelling variants of that display name (not only the exact string).
3. On each discovered item, scan **every** unlinked author:
   - If `fuzzy_match_author(item_author, display_name)` → **link** to the fixed ORCID authority.
   - Otherwise → log `SKIP … reason=name_mismatch_with_target_authority` (co-authors on the same paper are skipped this way; that is expected).

The **Exact / Fuzzy** prompt does **not** change the comparison in ORCID mode when a target authority is fixed: name matching always uses the fuzzy rules below. The **auto-link** prompt does apply: answer **No** to review each proposed link before it is written.

## Exact vs fuzzy

| Setting | Used in | Behaviour |
|---------|---------|-----------|
| **Exact** | Item / Repository modes | Vocabulary `filter` with `exact=true`; display name must equal the normalized item author string (case- and accent-insensitive, whitespace collapsed). |
| **Fuzzy** | Item / Repository modes | Vocabulary `filter` by family name (`exact=false`), then each candidate is checked with `fuzzy_match_author`. |

Fuzzy mode shows a warning at startup because abbreviated first names can match the wrong person when several authorities share a surname and initial.

## Fuzzy matching rules

All fuzzy logic lives in `scoring.py` (`fuzzy_match_author`). Use this section to predict what will link and what will not.

### Name parsing

- Authority strings are read as **`Family, First`** (DSpace / Solr convention).
- Item strings with a comma try **both** `Family, First` and `First, Family` (free-text metadata often reverses order).
- Names are normalized before comparison: collapse whitespace, strip accents, lowercase.

### When two authors fuzzy-match

Both must pass **family** and **first name** checks.

#### Family name (last name)

Family names match if either:

1. They are equal after normalization (`De Eyto` = `de eyto`), **or**
2. They are equal with **all internal spaces removed** (`DeEyto` = `de eyto` → both become `deeyto`).

Examples against authority `"de Eyto, Elvira"`:

| Item author | Family match? |
|-------------|---------------|
| `de Eyto, Elvira` | Yes |
| `DeEyto, Elvira` | Yes (spacing only) |
| `De Eyto, E` | Yes |
| `DeEyto, E` | Yes |
| `Eyto, Elvira` | **No** (different family) |

#### First name (given name)

The item may use a **full** first name or **initials**; the authority is expected to have the full given name.

| Rule | Example (authority: `Smith, John`) |
|------|----------------------------------|
| Exact first name | `Smith, John` → match |
| Item is initials of authority | `Smith, J.` → match (`J` = initial of `John`) |
| Item is initials of multi-part given name | `Doe, J. M.` vs `Doe, Jane Marie` → match |
| Periods in initials ignored | `Smith, J.` = `Smith, J` |
| Authority has only initials | Item `Smith, John` vs authority `Smith, J.` → **no** match (initials only allowed on the **item** side) |
| Wrong initial | `DeEyto, M` vs `de Eyto, Elvira` → **no** match |
| Item has no first name | Family match alone is enough |
| Item has first name, authority has none | **No** match |

#### Worked example: compound surname (`de Eyto, Elvira`)

| Item metadata | Matches authority? | Why |
|---------------|------------------|-----|
| `de Eyto, Elvira` | Yes | Exact family + first |
| `De Eyto, Elvira` | Yes | Case-insensitive |
| `DeEyto, Elvira` | Yes | Family spacing variant |
| `De Eyto, E` | Yes | Family OK + initial `E` |
| `de Eyto, E.` | Yes | Initial with period |
| `DeEyto, E` | Yes | Spacing + initial |
| `DeEyto, M` | No | Wrong initial |
| `Jennings, Eleanor` | No | Different person (co-author on same item) |

### What fuzzy matching does **not** do

- No typo tolerance on family names (beyond internal spaces).
- No partial first names (`Elv` vs `Elvira`).
- No matching when only a substring of the full name overlaps.
- No cross-family matching (`van Dyke` vs `Dyke` without shared compact form).

### Ambiguity risk

Initials are inherently ambiguous: `de Eyto, E` matches any local authority `de Eyto, <name starting with E>` (e.g. `Elvira`, `Edward`). In Item / Repository fuzzy mode, **multiple** vocabulary hits always require manual selection. In ORCID mode the ORCID is fixed, so that particular ambiguity is resolved — but co-authors on the same item are still skipped.

## Discovery search variants (ORCID and Name modes)

`author_search_variants()` builds several Discovery `author contains` filters from one authority display name so items are found even when metadata spelling differs.

From `"de Eyto, Elvira"` the tool also searches (among others):

- `DeEyto`, `DeEyto, Elvira`
- `de Eyto`, `de Eyto, E`, `de Eyto, E.`
- `DeEyto, E`, `DeEyto, E.`

Results are deduplicated by item UUID.

Item / Repository modes do **not** use this variant list; they rely on per-author vocabulary lookup instead.

## Log file

Each run writes lines such as:

```
LINK item_uuid=… author='DeEyto, Elvira' authority=… orcid='https://orcid.org/…'
SKIP … author='Jennings, Eleanor' reason=name_mismatch_with_target_authority authority_display='de Eyto, Elvira'
NO_MATCH … author='…' 
SKIP … reason=multiple_matches
SKIP … reason=lookup_error
```

| Line | Meaning |
|------|---------|
| `LINK` | Author linked to authority |
| `SKIP … name_mismatch_with_target_authority` | ORCID/Name mode: author on item does not fuzzy-match the target display name |
| `SKIP … multiple_matches` | User chose not to pick among several vocabulary matches |
| `NO_MATCH` | No vocabulary candidate (Item/Repository) or lookup error |
| `SUMMARY` | Counts: `linked`, `skipped` (user), `no_match` |

In ORCID mode, `no_match` includes every co-author skipped on discovered items — expect a large number even when linking succeeds.

Log files matching `link_author_authorities_*.log` and `link_author_authorities_state.jsonl` are in the repo `.gitignore`.

## Package layout

| File | Role |
|------|------|
| `main.py` | Interactive prompts, mode orchestration |
| `process.py` | Item processing, discovery, PATCH |
| `scoring.py` | Name normalization, fuzzy matching, search variants |
| `orcid.py` | ORCID parsing and vocabulary resolution |
| `session.py` | Throttled API calls, console |
| `state.py` | Repository checkpoint / attempt state |

The runner at `examples/link_author_authorities.py` imports `main` from this package.

## Tests

Fuzzy matching and search variants are covered in:

```bash
pytest tests/test_link_author_orcid_normalize.py -v
```

## Compatibility notes

- **Local vocabulary only** — authorities must exist in your SOLR authority core.
- **Vocabulary filter behaviour** varies by DSpace site (Solr configuration). ORCID resolution uses several filter strategies; see `orcid.py`.
- **Atmire promo**: optional session-end panel; set `DSPACE_CLIENT_DISABLE_ATMIRE_PROMO=1` to disable.
