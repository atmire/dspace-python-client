"""Bucket DSpace items by REF 2029 OA policy compliance (real-time, anonymous-by-default REST scan).

Filters items by ``dc.date.issued`` (publication year) and ``dc.type``, then
checks each item for an ORIGINAL-bundle bitstream and its access status. Each
item is bucketed against the panel-specific embargo maxima from the REF 2029
Open Access policy and emitted to a CSV.

Defaults to anonymous read-only access: every value the script consults
(items, bundles, bitstreams, embargo dates) is also visible in the public
Angular UI, so credentials are not required for a public REF-posture report.
Supplying credentials at the prompt switches to authenticated mode and
includes private/non-discoverable items in the scan.

Out of scope (DSpace metadata does not record these): permitted exceptions,
deposit-on-acceptance timing, AAM vs Version of Record, license validation,
scanned-image detection. Cross-check borderline items by hand.
"""

import asyncio
import csv
import getpass
import io
import re
from datetime import date, datetime

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from dspace_client import (
    ServerVersionMismatchError,
    create_anonymous_client,
    create_validated_client,
    show_script_attribution,
)

TARGET_VERSIONS = ["7.6", "8.0", "9.0"]
SCRIPT_AUTHORS = "Bram Luyten (Atmire)"
DEFAULT_TYPE = "Journal article"
PAGE_SIZE = 100

# REF 2029 thresholds keyed by output-year regime (see ref-oa-policy.md).
REF_THRESHOLDS = {
    "2021-2025": {"ab_max": 12, "cd_max": 24},
    "2026-2028": {"ab_max": 6, "cd_max": 12},
}

console = Console()


# ---------------------------------------------------------------------------
# Pure helpers (covered by tests/test_realtime_ref.py — keep them I/O-free).
# ---------------------------------------------------------------------------


def get_metadata_value(metadata: dict, key: str) -> str:
    """Extract metadata value, joining multiple values with ||."""
    values = metadata.get(key, [])
    if not values:
        return ""
    return " || ".join(v.get("value", "") for v in values)


def matches_type(metadata: dict, target: str) -> bool:
    """Return True if any dc.type value equals target (case- and whitespace-insensitive).

    DSpace repositories vary on dc.type casing ("Journal Article", "Journal
    article", "JOURNAL ARTICLE", "article", ...). The Solr discovery query is
    case-sensitive in many configurations, so the script runs a year-only
    search and post-filters here. An empty target accepts everything.
    """
    target_norm = (target or "").strip().casefold()
    if not target_norm:
        return True
    for v in metadata.get("dc.type", []):
        value = (v.get("value") or "").strip().casefold()
        if value == target_norm:
            return True
    return False


def parse_issued_year(date_issued: str) -> int | None:
    """Return the 4-digit year from a dc.date.issued string, or None."""
    if not date_issued:
        return None
    m = re.match(r"^\s*(\d{4})", date_issued)
    if not m:
        return None
    return int(m.group(1))


def parse_issued_date(date_issued: str) -> date | None:
    """Parse YYYY / YYYY-MM / YYYY-MM-DD; year-only falls back to Jan 1."""
    if not date_issued:
        return None
    s = date_issued.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def regime_for_year(year: int | None) -> str | None:
    """Map a publication year to its REF 2029 regime key (or None if outside)."""
    if year is None:
        return None
    if 2021 <= year <= 2025:
        return "2021-2025"
    if 2026 <= year <= 2028:
        return "2026-2028"
    return None


def embargo_months_between(issued: date, embargo_end: date) -> float:
    """Day-based embargo length in months (1 month = 30.4375 days)."""
    return (embargo_end - issued).days / 30.4375


def derive_access_from_conditions(
    conditions: list[dict],
) -> tuple[str | None, date | None]:
    """Map a bitstream's accessConditions list to (status, embargo_end_date).

    DSpace's resource-policy semantics for a READ policy on a bitstream:
      - ``openAccess``: anyone can read now -> ("open.access", None).
      - ``embargo``: ``startDate`` is the date READ becomes possible
        (i.e. the embargo *lift* date, despite the field name).
        -> ("embargo", lift_date). With multiple embargo entries, pick the
        earliest lift (most permissive).
      - administrator / group-only / lease / anything else
        -> ("restricted", None).
      - empty/unknown -> (None, None) so the caller can fall back to
        :func:`fetch_access_status`.

    This source is more reliable than the derived ``/accessStatus`` endpoint
    on customised installs (some Atmire-hosted repos report ``open.access``
    via /accessStatus while the embargo metadata still lives in the access
    conditions collection).
    """
    if not conditions:
        return (None, None)

    names = {c.get("name", "") for c in conditions}
    if "openAccess" in names:
        return ("open.access", None)

    embargo_lifts: list[date] = []
    for c in conditions:
        if c.get("name") != "embargo":
            continue
        raw = c.get("startDate")
        if not raw:
            continue
        try:
            embargo_lifts.append(datetime.strptime(raw[:10], "%Y-%m-%d").date())
        except (TypeError, ValueError):
            continue
    if embargo_lifts:
        return ("embargo", min(embargo_lifts))

    return ("restricted", None)


def extract_conditions_from_bitstream(bitstream: dict) -> list[dict]:
    """Pull the accessConditions list from a bitstream JSON object.

    The expected structure (from ``embed=accessConditions``) is::

        bitstream["_embedded"]["accessConditions"]["_embedded"]["accessConditions"]

    Returns an empty list if any link in that chain is missing.
    """
    embedded = bitstream.get("_embedded", {}) or {}
    container = embedded.get("accessConditions") or {}
    if not isinstance(container, dict):
        return []
    inner = container.get("_embedded") or {}
    conditions = inner.get("accessConditions")
    if isinstance(conditions, list):
        return conditions
    return []


def classify_item(
    *,
    issued: date | None,
    issued_year: int | None,
    has_original_deposit: bool,
    access_status: str | None,
    embargo_end: date | None,
) -> tuple[str, str]:
    """Pure REF-compliance bucketing. Returns (bucket, notes).

    Decision tree (in order):
      - missing/unparseable dc.date.issued -> unknown_no_issued_date
      - no ORIGINAL-bundle bitstream      -> not_eligible_no_deposit
      - permanently restricted/metadata-only -> not_eligible_no_open_access
      - open access or no embargo         -> eligible_all_panels
      - embargoed: compare months elapsed against the year's regime thresholds
    """
    if issued_year is None:
        return ("unknown_no_issued_date", "dc.date.issued absent or unparseable")
    if not has_original_deposit:
        return ("not_eligible_no_deposit", "no ORIGINAL-bundle bitstream found")
    if access_status in ("restricted", "metadata.only"):
        return ("not_eligible_no_open_access", f"access status={access_status}")
    if access_status == "open.access" or embargo_end is None:
        return ("eligible_all_panels", "open access or no embargo recorded")
    if access_status == "embargo" and embargo_end is not None and issued is not None:
        regime = regime_for_year(issued_year)
        if regime is None:
            return (
                "unknown_other",
                f"issued year {issued_year} outside REF 2029 regime (2021-2028)",
            )
        months = embargo_months_between(issued, embargo_end)
        th = REF_THRESHOLDS[regime]
        if months <= th["ab_max"]:
            return (
                "eligible_all_panels",
                f"embargo {months:.1f}m ≤ {th['ab_max']}m (regime {regime})",
            )
        if months <= th["cd_max"]:
            return (
                "eligible_cd_only",
                f"embargo {months:.1f}m ≤ {th['cd_max']}m, A/B max {th['ab_max']}m exceeded (regime {regime})",
            )
        return (
            "not_eligible_embargo_too_long",
            f"embargo {months:.1f}m > {th['cd_max']}m (regime {regime})",
        )
    return ("unknown_other", f"unrecognised access status={access_status!r}")


# ---------------------------------------------------------------------------
# I/O helpers — depend on a live DSpaceClient.
# ---------------------------------------------------------------------------


async def fetch_bitstreams_with_conditions(client, bundle_uuid: str) -> list[dict]:
    """GET a bundle's bitstreams with format and accessConditions embedded.

    Equivalent to the call the Angular UI makes:
    ``/server/api/core/bundles/{uuid}/bitstreams?embed=format&embed=accessConditions&size=100``.

    Returns the list of bitstream dicts (each carrying the embedded data),
    or [] on error.
    """
    url = f"{client.base_url}/server/api/core/bundles/{bundle_uuid}/bitstreams"
    params = {"embed": ["format", "accessConditions"], "size": 100}
    try:
        response = await client.client.get(
            url,
            params=params,
            headers=client._get_headers(include_csrf=False),
        )
    except httpx.RequestError as e:
        console.print(
            f"[yellow]⚠[/yellow]  bitstreams fetch failed for bundle {bundle_uuid}: {e}"
        )
        return []
    if response.status_code in (401, 403, 404):
        return []
    if response.status_code >= 400:
        console.print(
            f"[yellow]⚠[/yellow]  bitstreams {response.status_code} for bundle {bundle_uuid}"
        )
        return []
    try:
        data = response.json()
    except ValueError:
        return []
    if "_embedded" in data and "bitstreams" in data["_embedded"]:
        return data["_embedded"]["bitstreams"]
    return data.get("bitstreams", []) or []


async def fetch_access_status(
    client, bitstream_uuid: str
) -> tuple[str | None, date | None]:
    """Fallback: GET .../core/bitstreams/{uuid}/accessStatus. Returns (status, embargoDate)."""
    url = f"{client.base_url}/server/api/core/bitstreams/{bitstream_uuid}/accessStatus"
    try:
        response = await client.client.get(
            url,
            headers=client._get_headers(include_csrf=False),
        )
    except httpx.RequestError as e:
        console.print(f"[yellow]⚠[/yellow]  accessStatus fetch failed for {bitstream_uuid}: {e}")
        return (None, None)

    if response.status_code in (401, 403, 404):
        return (None, None)
    if response.status_code >= 400:
        console.print(
            f"[yellow]⚠[/yellow]  accessStatus {response.status_code} for {bitstream_uuid}"
        )
        return (None, None)

    try:
        data = response.json()
    except ValueError:
        return (None, None)

    status = data.get("status") or data.get("accessStatus")
    embargo_raw = data.get("embargoDate")
    embargo_end: date | None = None
    if embargo_raw:
        try:
            embargo_end = datetime.strptime(embargo_raw[:10], "%Y-%m-%d").date()
        except ValueError:
            embargo_end = None
    return (status, embargo_end)


def _bundles_from_response(payload: dict) -> list[dict]:
    if "_embedded" in payload and "bundles" in payload["_embedded"]:
        return payload["_embedded"]["bundles"]
    return payload.get("bundles", [])


_STATUS_PRIORITY = {
    "open.access": 0,
    "embargo": 1,
    "metadata.only": 2,
    "restricted": 3,
}


def _is_more_permissive(
    candidate: tuple[str | None, date | None],
    incumbent: tuple[str | None, date | None],
) -> bool:
    """Return True if candidate is more permissive than incumbent.

    Order: open.access > embargo (smaller embargoDate wins) > metadata.only > restricted > unknown.
    """
    cand_status, cand_end = candidate
    inc_status, inc_end = incumbent
    if inc_status is None:
        return cand_status is not None
    if cand_status is None:
        return False
    cand_rank = _STATUS_PRIORITY.get(cand_status, 99)
    inc_rank = _STATUS_PRIORITY.get(inc_status, 99)
    if cand_rank != inc_rank:
        return cand_rank < inc_rank
    # Same status — for embargo, smaller end date is more permissive.
    if cand_status == "embargo":
        if cand_end is None:
            return False
        if inc_end is None:
            return True
        return cand_end < inc_end
    return False


async def collapse_to_most_permissive(
    client, item_uuid: str
) -> tuple[bool, int, str | None, date | None]:
    """Walk ORIGINAL bundles only.

    Returns (has_original_deposit, bitstream_count, best_status, best_embargo_end).
    """
    bundles_payload = await client.get_item_bundles(item_uuid)
    bundles = _bundles_from_response(bundles_payload)
    original_bundles = [b for b in bundles if b.get("name") == "ORIGINAL"]

    bitstream_count = 0
    best: tuple[str | None, date | None] = (None, None)

    for bundle in original_bundles:
        bundle_uuid = bundle.get("uuid")
        if not bundle_uuid:
            continue
        bitstreams = await fetch_bitstreams_with_conditions(client, bundle_uuid)
        for bs in bitstreams:
            bs_uuid = bs.get("uuid")
            if not bs_uuid:
                continue
            bitstream_count += 1
            # Primary: derive from embedded accessConditions (works on any
            # DSpace 7+ including customised installs where /accessStatus is
            # unreliable).
            conditions = extract_conditions_from_bitstream(bs)
            candidate = derive_access_from_conditions(conditions)
            # Fallback: hit /accessStatus only if conditions yielded nothing.
            if candidate == (None, None):
                candidate = await fetch_access_status(client, bs_uuid)
            if _is_more_permissive(candidate, best):
                best = candidate

    has_deposit = bitstream_count > 0
    return (has_deposit, bitstream_count, best[0], best[1])


# ---------------------------------------------------------------------------
# Prompts, search, CSV, summary, main.
# ---------------------------------------------------------------------------


async def prompt_inputs() -> dict:
    """Collect interactive inputs (anonymous-by-default)."""
    base_url = console.input(
        "[bold cyan]DSpace base URL[/bold cyan] [dim](press Enter for https://demo.dspace.org):[/dim] "
    ).strip()
    if not base_url:
        base_url = "https://demo.dspace.org"
        console.print("[dim]→ Using default: https://demo.dspace.org[/dim]")
    base_url_normalised = base_url.rstrip("/").lower()
    is_demo = "demo.dspace.org" in base_url_normalised

    username = console.input(
        "[bold cyan]Username[/bold cyan] [dim](press Enter for anonymous read-only):[/dim] "
    ).strip()

    password: str | None = None
    if username:
        if is_demo and username == "dspacedemo+admin@gmail.com":
            password = "dspace"
            console.print("[dim]→ Using demo.dspace.org default password.[/dim]")
        else:
            password = getpass.getpass("Password: ")
    else:
        console.print("[dim]→ Anonymous mode: only publicly discoverable items will be scanned.[/dim]")

    current_year = datetime.now().year
    year_input = console.input(
        f"[bold cyan]Publication year (dc.date.issued)[/bold cyan] [dim](press Enter for {current_year}):[/dim] "
    ).strip()
    year = year_input or str(current_year)

    type_input = console.input(
        f'[bold cyan]Item type (dc.type, case-insensitive)[/bold cyan] [dim](press Enter for "{DEFAULT_TYPE}"):[/dim] '
    ).strip()
    type_value = type_input or DEFAULT_TYPE

    delay_input = console.input(
        "[bold cyan]Delay between page requests in seconds[/bold cyan] [dim](press Enter for 1.0):[/dim] "
    ).strip()
    try:
        delay = float(delay_input) if delay_input else 1.0
        if delay < 0:
            raise ValueError
    except ValueError:
        console.print("[yellow]⚠[/yellow]  Invalid delay; falling back to 1.0s.")
        delay = 1.0

    cap_input = console.input(
        "[bold cyan]Max items to scan[/bold cyan] [dim](press Enter for no cap):[/dim] "
    ).strip()
    max_items: int | None
    if not cap_input:
        max_items = None
    else:
        try:
            max_items = int(cap_input)
            if max_items <= 0:
                raise ValueError
        except ValueError:
            console.print("[yellow]⚠[/yellow]  Invalid cap; running without a cap.")
            max_items = None

    return {
        "base_url": base_url,
        "username": username or None,
        "password": password,
        "year": year,
        "type_value": type_value,
        "delay": delay,
        "max_items": max_items,
    }


async def search_loop(client, year: str, type_value: str, delay: float, max_items: int | None):
    """Yield item UUIDs matching the year + type filter.

    The dc.type comparison is case-insensitive. DSpace's default Solr config
    lowercases tokens at index time (StandardAnalyzer), so a lowercased
    quoted phrase here matches any casing of the indexed value — "Journal
    Article", "Journal article", "JOURNAL ARTICLE", etc. all behave the
    same. This is a no-op against a default DSpace install and a defensive
    nudge for instances with custom analyzer configurations.
    """
    type_query_value = type_value.lower()
    query = f'dc.date.issued:{year}* AND dc.type:"{type_query_value}"'
    page = 0
    yielded = 0
    while True:
        result = await client.search_items(
            query=query,
            page=page,
            size=PAGE_SIZE,
            sort="dc.date.issued,desc",
        )
        objects = (
            result.get("_embedded", {})
            .get("searchResult", {})
            .get("_embedded", {})
            .get("objects", [])
        )
        if not objects:
            return
        for obj in objects:
            indexable = obj.get("_embedded", {}).get("indexableObject", {})
            uuid = indexable.get("uuid")
            if not uuid:
                continue
            yield uuid
            yielded += 1
            if max_items is not None and yielded >= max_items:
                return
        if len(objects) < PAGE_SIZE:
            return
        page += 1
        if delay > 0:
            await asyncio.sleep(delay)


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "item"


def write_csv(rows: list[dict], filename: str) -> None:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "item_uuid",
            "handle_or_uri",
            "title",
            "date_issued",
            "type",
            "original_bundle_bitstream_count",
            "most_permissive_access_status",
            "embargo_end_date",
            "embargo_months_from_issued",
            "panel_year_regime",
            "ab_max_months",
            "cd_max_months",
            "bucket",
            "notes",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r.get("item_uuid", ""),
                r.get("handle_or_uri", ""),
                r.get("title", ""),
                r.get("date_issued", ""),
                r.get("type", ""),
                r.get("original_bundle_bitstream_count", ""),
                r.get("most_permissive_access_status", ""),
                r.get("embargo_end_date", ""),
                r.get("embargo_months_from_issued", ""),
                r.get("panel_year_regime", ""),
                r.get("ab_max_months", ""),
                r.get("cd_max_months", ""),
                r.get("bucket", ""),
                r.get("notes", ""),
            ]
        )
    with open(filename, "w", encoding="utf-8") as fh:
        fh.write(output.getvalue())


def print_summary(rows: list[dict], filename: str) -> None:
    counts: dict = {}
    for r in rows:
        bucket = r.get("bucket", "unknown_other")
        counts[bucket] = counts.get(bucket, 0) + 1
    table = Table(title="REF 2029 posture summary", show_lines=False)
    table.add_column("Bucket", style="bold")
    table.add_column("Count", justify="right")
    for bucket in (
        "eligible_all_panels",
        "eligible_cd_only",
        "not_eligible_embargo_too_long",
        "not_eligible_no_open_access",
        "not_eligible_no_deposit",
        "unknown_no_issued_date",
        "unknown_other",
    ):
        if bucket in counts:
            table.add_row(bucket, str(counts.pop(bucket)))
    for bucket, count in counts.items():
        table.add_row(bucket, str(count))
    table.add_row("[bold]TOTAL[/bold]", f"[bold]{len(rows)}[/bold]")
    console.print(table)
    console.print(f"[green]CSV:[/green] {filename}")


async def main() -> None:
    show_script_attribution(SCRIPT_AUTHORS, console=console)
    inputs = await prompt_inputs()

    auth = None
    http: httpx.AsyncClient | None = None
    client = None
    try:
        if inputs["username"]:
            auth, client = await create_validated_client(
                base_url=inputs["base_url"],
                username=inputs["username"],
                password=inputs["password"] or "",
                target_versions=TARGET_VERSIONS,
            )
        else:
            http, client = await create_anonymous_client(
                base_url=inputs["base_url"],
                target_versions=TARGET_VERSIONS,
            )

        rows: list[dict] = []
        scanned = 0
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed} items"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as progress:
            task_id = progress.add_task("Scanning items", total=inputs["max_items"])
            async for uuid in search_loop(
                client,
                inputs["year"],
                inputs["type_value"],
                inputs["delay"],
                inputs["max_items"],
            ):
                try:
                    item = await client.get_item(uuid)
                    metadata = item.get("metadata", {})
                    title = get_metadata_value(metadata, "dc.title")
                    date_issued = get_metadata_value(metadata, "dc.date.issued")
                    type_recorded = get_metadata_value(metadata, "dc.type")
                    uri = get_metadata_value(metadata, "dc.identifier.uri")

                    has_dep, bitstream_count, status, embargo_end = await collapse_to_most_permissive(
                        client, uuid
                    )
                    issued_year = parse_issued_year(date_issued)
                    issued_date = parse_issued_date(date_issued)
                    bucket, notes = classify_item(
                        issued=issued_date,
                        issued_year=issued_year,
                        has_original_deposit=has_dep,
                        access_status=status,
                        embargo_end=embargo_end,
                    )

                    regime = regime_for_year(issued_year)
                    th = REF_THRESHOLDS.get(regime, {}) if regime else {}
                    embargo_months: str = ""
                    if status == "embargo" and embargo_end and issued_date:
                        embargo_months = f"{embargo_months_between(issued_date, embargo_end):.1f}"

                    if issued_year is not None and date_issued and len(date_issued.strip()) == 4:
                        notes = f"{notes} (year-only date: embargo math anchors at Jan 1)"

                    rows.append(
                        {
                            "item_uuid": uuid,
                            "handle_or_uri": uri,
                            "title": title,
                            "date_issued": date_issued,
                            "type": type_recorded,
                            "original_bundle_bitstream_count": bitstream_count,
                            "most_permissive_access_status": status or "",
                            "embargo_end_date": embargo_end.isoformat() if embargo_end else "",
                            "embargo_months_from_issued": embargo_months,
                            "panel_year_regime": regime or "",
                            "ab_max_months": th.get("ab_max", ""),
                            "cd_max_months": th.get("cd_max", ""),
                            "bucket": bucket,
                            "notes": notes,
                        }
                    )
                except Exception as e:  # noqa: BLE001 — per-item resilience
                    console.print(f"[red]Error[/red] processing item {uuid}: {e}")
                scanned += 1
                progress.update(task_id, advance=1)

        if not rows:
            console.print("[yellow]No items matched the search; nothing to write.[/yellow]")
            return

        type_slug = _slugify(inputs["type_value"])
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"realtime_REF_{inputs['year']}_{type_slug}_{timestamp}.csv"
        write_csv(rows, filename)
        print_summary(rows, filename)
    except ServerVersionMismatchError as e:
        console.print(f"[red]Cannot connect:[/red] {e}")
        console.print(
            f"[red]This script targets DSpace versions: {', '.join(TARGET_VERSIONS)}[/red]"
        )
    finally:
        if auth is not None:
            await auth.close()
        if http is not None:
            await http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
