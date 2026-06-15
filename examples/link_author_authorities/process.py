"""Item processing and discovery helpers."""

from __future__ import annotations

from datetime import datetime

from rich.panel import Panel

from dspace_client import AuthenticationError, DSpaceAuthClient, DSpaceClient
from dspace_client.throttle import ThrottleController

from orcid import extract_orcid_from_entry, fetch_entry_detail
from scoring import (
    AUTHOR_FIELD,
    _parse_family_first,
    author_search_variants,
    fuzzy_match_author,
    get_unlinked_authors,
    normalize_name,
)
from session import _throttled_call, console

CONFIDENCE_LINKED = 600


def _log(log_file: object | None, line: str) -> None:
    """Write a line to the log file and flush."""
    if log_file is not None:
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        log_file.write(f"{ts} {line}\n")
        log_file.flush()


def _first_metadata_value(metadata: dict, key: str) -> str:
    """Get first metadata value for a key, or empty string."""
    vals = metadata.get(key) or []
    if not isinstance(vals, list) or not vals:
        return ""
    first = vals[0]
    if isinstance(first, dict):
        return str(first.get("value") or "").strip()
    return str(first).strip()


def _all_metadata_values(metadata: dict, key: str) -> list[str]:
    """Get all metadata values for a key as strings."""
    vals = metadata.get(key) or []
    result: list[str] = []
    if not isinstance(vals, list):
        return result
    for v in vals:
        if isinstance(v, dict):
            s = str(v.get("value") or "").strip()
        else:
            s = str(v).strip()
        if s:
            result.append(s)
    return result


async def discover_item_uuids_newest_first(
    auth: DSpaceAuthClient,
    client: DSpaceClient,
    username: str,
    password: str,
    throttle: ThrottleController,
    page_size: int = 100,
) -> list[str]:
    """Discover all item UUIDs via discovery API, newest first. Paginates until no more results."""
    uuids: list[str] = []
    page = 0
    while True:
        results = await _throttled_call(
            auth,
            client,
            username,
            password,
            throttle,
            lambda: client.search_items(
                query="*",
                sort="dc.date.accessioned,desc",
                page=page,
                size=page_size,
            ),
        )
        objects = (
            results.get("_embedded") or {}
        ).get("searchResult", {}).get("_embedded", {}).get("objects", [])
        if not objects:
            break
        for obj in objects:
            indexable = (obj.get("_embedded") or {}).get("indexableObject", {})
            uuid_val = indexable.get("uuid")
            if uuid_val:
                uuids.append(uuid_val)
        if len(objects) < page_size:
            break
        page += 1
    return uuids


async def _fetch_discovery_page_item_uuids(
    auth: DSpaceAuthClient,
    client: DSpaceClient,
    username: str,
    password: str,
    throttle: ThrottleController,
    page: int,
    page_size: int = 100,
) -> tuple[list[str], int | None]:
    """Fetch one discovery page and return (uuids, totalElements if present)."""
    results = await _throttled_call(
        auth,
        client,
        username,
        password,
        throttle,
        lambda: client.search_items(
            query="*",
            sort="dc.date.accessioned,desc",
            page=page,
            size=page_size,
        ),
    )
    emb = results.get("_embedded") or {}
    search_result = emb.get("searchResult") or emb.get("searchResults") or {}
    objects = (search_result.get("_embedded") or {}).get("objects", [])
    page_info = search_result.get("page") or {}
    total_elements = None
    if isinstance(page_info, dict):
        total_val = page_info.get("totalElements")
        if isinstance(total_val, int):
            total_elements = total_val

    uuids: list[str] = []
    for obj in objects:
        indexable = (obj.get("_embedded") or {}).get("indexableObject", {})
        uuid_val = indexable.get("uuid")
        if uuid_val:
            uuids.append(uuid_val)
    return uuids, total_elements


async def discover_item_uuids_by_author(
    auth: DSpaceAuthClient,
    client: DSpaceClient,
    username: str,
    password: str,
    throttle: ThrottleController,
    author_name: str,
    page_size: int = 100,
) -> list[str]:
    """
    Discover item UUIDs that have the given author (Discovery API author filter).

    Uses the documented f.author=<value>,contains filter per search-endpoint.md.
    Searches multiple spelling variants (e.g. "de Eyto, Elvira", "DeEyto, E") and
    deduplicates results.
    """
    seen_uuids: set[str] = set()
    uuids: list[str] = []
    for search_term in author_search_variants(author_name):
        page = 0
        while True:
            results = await _throttled_call(
                auth,
                client,
                username,
                password,
                throttle,
                lambda term=search_term: client.search_items(
                    query="*",
                    filters={"author": (term.strip(), "contains")},
                    sort="dc.date.accessioned,desc",
                    page=page,
                    size=page_size,
                ),
            )
            emb = results.get("_embedded") or {}
            search_result = emb.get("searchResult") or emb.get("searchResults") or {}
            objects = (search_result.get("_embedded") or {}).get("objects", [])
            if not objects:
                break
            for obj in objects:
                indexable = (obj.get("_embedded") or {}).get("indexableObject", {})
                uuid_val = indexable.get("uuid")
                if uuid_val and uuid_val not in seen_uuids:
                    seen_uuids.add(uuid_val)
                    uuids.append(uuid_val)
            if len(objects) < page_size:
                break
            page += 1
    return uuids


async def process_item(
    auth: DSpaceAuthClient,
    client: DSpaceClient,
    username: str,
    password: str,
    throttle: ThrottleController,
    item_uuid: str,
    vocabulary_name: str,
    auto_link_single: bool,
    use_fuzzy: bool,
    log_file: object | None,
    target_authority: tuple[str, str] | None = None,
    filter_author_name: str | None = None,
) -> tuple[int, int, int]:
    """
    Process one item: find unlinked authors, match to local authority, optionally prompt, PATCH.
    use_fuzzy: if True, allow abbreviated first names (e.g. "Smith, J." matches "Smith, John").
    target_authority: if set, (authority_uuid, display_name) to link matching unlinked authors to
        without vocabulary lookup; only unlinked authors that fuzzy-match display_name are linked.
        When auto_link_single is False, each match is confirmed interactively.
    filter_author_name: if set, only process unlinked authors that fuzzy-match this name (for Name mode without ORCID).
    Returns (linked_count, skipped_user, no_match_count).
    """
    try:
        item = await _throttled_call(
            auth,
            client,
            username,
            password,
            throttle,
            lambda: client.get_item(item_uuid),
        )
    except AuthenticationError as e:
        console.print(f"[red]Authentication error while getting item {item_uuid}: {e}[/red]")
        # Fatal: bubble up so the main loop can abort the run
        raise
    except Exception as e:
        console.print(f"[red]Failed to get item {item_uuid}: {e}[/red]")
        return (0, 0, 0)

    metadata = item.get("metadata") or {}
    title = _first_metadata_value(metadata, "dc.title")
    uris = _all_metadata_values(metadata, "dc.identifier.uri")
    uris_str = ",".join(uris) if uris else ""
    unlinked = get_unlinked_authors(metadata)
    _log(
        log_file,
        f"ITEM uuid={item_uuid} title={title!r} uris={uris_str!r} unlinked_count={len(unlinked)}",
    )

    if not unlinked:
        return (0, 0, 0)

    console.print(
        f"[cyan]Item {item_uuid}: '{title}' ({uris_str or 'no dc.identifier.uri'}) – "
        f"found {len(unlinked)} unlinked author(s).[/cyan]"
    )

    linked_count = 0
    skipped_user = 0
    no_match_count = 0

    for idx, value_obj in unlinked:
        author_value = (value_obj.get("value") or "").strip()
        if not author_value:
            continue
        language = value_obj.get("language")
        normalized = normalize_name(author_value)

        # When filter_author_name is set (Name mode without ORCID), only process authors matching that name
        if filter_author_name is not None and not fuzzy_match_author(author_value, filter_author_name):
            continue

        # When target_authority is set (ORCID/Name mode), only link if author fuzzy-matches that authority
        if target_authority is not None:
            authority_uuid, authority_display_name = target_authority
            if not fuzzy_match_author(author_value, authority_display_name):
                no_match_count += 1
                console.print(
                    f"[yellow]Skipped (does not match resolved authority name): {author_value!r} "
                    f"vs authority display {authority_display_name!r}[/yellow]"
                )
                _log(
                    log_file,
                    f"SKIP item_uuid={item_uuid} title={title!r} uris={uris_str!r} "
                    f"author={author_value!r} reason=name_mismatch_with_target_authority "
                    f"authority_display={authority_display_name!r}",
                )
                continue
            if not auto_link_single:
                detail_preview = await fetch_entry_detail(
                    client, vocabulary_name, authority_uuid
                )
                orcid_preview = extract_orcid_from_entry(
                    {"authority": authority_uuid, "metadata": {}}, detail_preview
                )
                lines = [
                    f"Author (item): [bold]{author_value}[/bold]",
                    f"Authority display: [bold]{authority_display_name}[/bold]",
                    f"Authority UUID: [bold]{authority_uuid}[/bold]",
                ]
                if orcid_preview:
                    lines.append(
                        f"ORCID: [link={orcid_preview}]{orcid_preview}[/link]"
                    )
                console.print(
                    Panel(
                        "\n".join(lines),
                        title="Link this author to the above authority?",
                        border_style="cyan",
                    )
                )
                answer = console.input("[bold]Link? (y/n)[/bold]: ").strip().lower()
                if answer not in ("y", "yes"):
                    console.print("[dim]Skipped by user.[/dim]")
                    skipped_user += 1
                    _log(
                        log_file,
                        f"SKIP item_uuid={item_uuid} title={title!r} uris={uris_str!r} "
                        f"author={author_value!r} authority={authority_uuid} "
                        f"reason=user_declined",
                    )
                    continue
            patch_value = {
                "value": author_value,
                "language": language,
                "authority": authority_uuid,
                "confidence": CONFIDENCE_LINKED,
            }
            operations = [
                {"op": "replace", "path": f"/metadata/{AUTHOR_FIELD}/{idx}", "value": patch_value}
            ]
            try:
                await _throttled_call(
                    auth,
                    client,
                    username,
                    password,
                    throttle,
                    lambda: client.patch_item(item_uuid, operations),
                )
                detail = await fetch_entry_detail(client, vocabulary_name, authority_uuid)
                orcid_url = extract_orcid_from_entry(
                    {"authority": authority_uuid, "metadata": {}}, detail
                )
                orcid_display = orcid_url or ""
                console.print(f"[green]Linked:[/green] {author_value!r}")
                linked_count += 1
                _log(
                    log_file,
                    f"LINK item_uuid={item_uuid} title={title!r} uris={uris_str!r} "
                    f"author={author_value!r} authority={authority_uuid} orcid={orcid_display!r}",
                )
            except AuthenticationError as e:
                console.print(
                    f"[red]Authentication error during PATCH for item {item_uuid}: {e}[/red]"
                )
                raise
            except Exception as e:
                console.print(f"[red]PATCH failed: {e}[/red]")
            continue

        try:
            if use_fuzzy:
                # Fuzzy: paginate by family name to find "Smith, John" when item has "Smith, J."
                family, _ = _parse_family_first(author_value)
                if not family:
                    matching = []
                else:
                    matching = []
                    page = 0
                    size = 100
                    while True:
                        resp = await _throttled_call(
                            auth,
                            client,
                            username,
                            password,
                            throttle,
                            lambda: client.get_vocabulary_entries(
                                vocabulary_name,
                                filter_term=family,
                                exact=False,
                                page=page,
                                size=size,
                            ),
                        )
                        entries = (resp.get("_embedded") or {}).get("entries") or []
                        for e in entries:
                            if not isinstance(e, dict) or not e.get("authority"):
                                continue
                            auth_name = e.get("display") or e.get("value") or ""
                            if fuzzy_match_author(author_value, auth_name):
                                matching.append(e)
                        if matching or len(entries) < size:
                            break
                        page += 1
            else:
                resp = await _throttled_call(
                    auth,
                    client,
                    username,
                    password,
                    throttle,
                    lambda: client.get_vocabulary_entries(
                        vocabulary_name,
                        filter_term=author_value,
                        exact=True,
                        page=0,
                        size=20,
                    ),
                )
                entries = (resp.get("_embedded") or {}).get("entries") or []
                matching = [
                    e
                    for e in entries
                    if isinstance(e, dict)
                    and normalize_name(e.get("display") or e.get("value") or "") == normalized
                    and e.get("authority")
                ]
        except AuthenticationError as e:
            console.print(
                f"[red]Authentication error during vocabulary lookup for '{author_value}': {e}[/red]"
            )
            # Fatal: bubble up so the main loop can abort the run
            raise
        except Exception as e:
            console.print(f"[red]Vocabulary lookup failed for '{author_value}': {e}[/red]")
            no_match_count += 1
            _log(
                log_file,
                f"NO_MATCH item_uuid={item_uuid} title={title!r} uris={uris_str!r} "
                f"author={author_value!r} reason=lookup_error",
            )
            continue

        if not matching:
            console.print(f"[yellow]No local authority match for: {author_value!r}[/yellow]")
            no_match_count += 1
            _log(
                log_file,
                f"NO_MATCH item_uuid={item_uuid} title={title!r} uris={uris_str!r} "
                f"author={author_value!r}",
            )
            continue

        # Decide which authority entry to use.
        selected_entry: dict | None = None

        if len(matching) > 1:
            # Multiple possible matches: always require explicit user choice.
            lines = [
                f"Multiple local authority matches found for [bold]{author_value}[/bold]:",
            ]
            for opt_idx, cand in enumerate(matching, 1):
                cand_name = cand.get("display") or cand.get("value") or ""
                cand_auth = cand.get("authority") or ""
                lines.append(f"{opt_idx}. {cand_name} (authority={cand_auth})")
            console.print(
                Panel(
                    "\n".join(lines),
                    title="Select authority to link",
                    border_style="cyan",
                )
            )

            while True:
                choice_raw = console.input(
                    "[bold]Enter number to link, or 0 to skip[/bold]: "
                ).strip()
                try:
                    choice = int(choice_raw)
                except ValueError:
                    console.print("[red]Please enter a valid number.[/red]")
                    continue

                if choice == 0:
                    console.print("[dim]Skipped by user (multiple matches).[/dim]")
                    skipped_user += 1
                    _log(
                        log_file,
                        f"SKIP item_uuid={item_uuid} title={title!r} uris={uris_str!r} "
                        f"author={author_value!r} reason=multiple_matches",
                    )
                    selected_entry = None
                    break

                if 1 <= choice <= len(matching):
                    selected_entry = matching[choice - 1]
                    break

                console.print("[red]Choice out of range.[/red]")

            if selected_entry is None:
                continue
        else:
            # Exactly one match; respect auto_link_single flag.
            selected_entry = matching[0]
            authority_uuid_preview = selected_entry.get("authority") or ""
            if not authority_uuid_preview:
                no_match_count += 1
                continue

            if not auto_link_single:
                # Require per-match confirmation for even single matches.
                detail_preview = await fetch_entry_detail(
                    client, vocabulary_name, authority_uuid_preview
                )
                orcid_preview = extract_orcid_from_entry(
                    selected_entry, detail_preview
                )
                lines = [
                    f"Author (item): [bold]{author_value}[/bold]",
                    f"Authority UUID: [bold]{authority_uuid_preview}[/bold]",
                ]
                if orcid_preview:
                    lines.append(
                        f"ORCID: [link={orcid_preview}]{orcid_preview}[/link]"
                    )
                console.print(
                    Panel(
                        "\n".join(lines),
                        title="Link this author to the above authority?",
                        border_style="cyan",
                    )
                )
                answer = console.input("[bold]Link? (y/n)[/bold]: ").strip().lower()
                if answer not in ("y", "yes"):
                    console.print("[dim]Skipped by user.[/dim]")
                    skipped_user += 1
                    _log(
                        log_file,
                        f"SKIP item_uuid={item_uuid} title={title!r} uris={uris_str!r} "
                        f"author={author_value!r} authority={authority_uuid_preview}",
                    )
                    continue

        authority_uuid = selected_entry.get("authority") or ""
        if not authority_uuid:
            no_match_count += 1
            continue

        detail = await fetch_entry_detail(client, vocabulary_name, authority_uuid)
        orcid_url = extract_orcid_from_entry(selected_entry, detail)

        # PATCH
        patch_value = {
            "value": author_value,
            "language": language,
            "authority": authority_uuid,
            "confidence": CONFIDENCE_LINKED,
        }
        operations = [
            {"op": "replace", "path": f"/metadata/{AUTHOR_FIELD}/{idx}", "value": patch_value}
        ]
        try:
            await _throttled_call(
                auth,
                client,
                username,
                password,
                throttle,
                lambda: client.patch_item(item_uuid, operations),
            )
            orcid_display = orcid_url or ""
            console.print(f"[green]Linked:[/green] {author_value!r}")
            linked_count += 1
            _log(
                log_file,
                f"LINK item_uuid={item_uuid} title={title!r} uris={uris_str!r} "
                f"author={author_value!r} authority={authority_uuid} orcid={orcid_display!r}",
            )
        except AuthenticationError as e:
            console.print(
                f"[red]Authentication error during PATCH for item {item_uuid}: {e}[/red]"
            )
            # Fatal: bubble up so the main loop can abort the run
            raise
        except Exception as e:
            console.print(f"[red]PATCH failed: {e}[/red]")

    return (linked_count, skipped_user, no_match_count)
