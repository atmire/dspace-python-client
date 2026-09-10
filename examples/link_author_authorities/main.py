"""
Link item authors to local authority records (interactive).

Enhances an item that has dc.contributor.author in clear text by linking each
unlinked author to an authority already in this repository's SOLR authority core.
Uses the local vocabulary endpoint (e.g. CacheableAuthorAuthority) only — does
NOT search the public ORCID registry.

Modes: [I]tem (one UUID), [R]epository (all items, with run mode), [O]RCID (one ORCID
→ find items by that authority's name and link unlinked authors), [N]ame (one name
→ find items by author filter, then link to vocabulary or a given ORCID).
[O] and [N] use the Discovery Search API (see docs/dspace-rest-api/7.6/search-endpoint.md)
with the author filter.

You can choose Exact or Fuzzy author matching; and whether to auto-link when there is
exactly one local authority match. Each run writes a timestamped log file.

Run with the project venv (see README "Development")::
  source venv/bin/activate
  python examples/link_author_authorities.py
  # For Item mode you can pass a UUID: python examples/link_author_authorities.py <item-uuid>
"""

from __future__ import annotations

import getpass
import os
import sys
from datetime import datetime

from orcid import normalize_orcid_identifier, resolve_authority_by_orcid
from process import (
    _fetch_discovery_page_item_uuids,
    _log,
    discover_item_uuids_by_author,
    process_item,
)
from rich.panel import Panel
from session import console
from state import (
    AUTO_CHUNK_THRESHOLD_ENV_VAR,
    DEFAULT_AUTO_CHUNK_SIZE,
    DEFAULT_AUTO_CHUNK_THRESHOLD,
    _append_attempt_state,
    _clear_repo_checkpoint,
    _get_checkpoint_path,
    _get_state_path,
    _load_attempt_state,
    _load_repo_checkpoint,
    _save_repo_checkpoint,
    _should_process_uuid,
)

from dspace_client import (
    AuthenticationError,
    DSpaceAuthClient,
    DSpaceClient,
    prompt_and_authenticate,
    show_script_attribution,
)
from dspace_client.throttle import ThrottleConfig, ThrottleController

# Compatible with DSpace 7.x, 8.x, 9.x, 10.x (items PATCH and submission vocabularies)
TARGET_VERSIONS = ["7.0", "8.0", "9.0", "10.0"]
SCRIPT_AUTHORS = "Bram Luyten (Atmire), Wesley Van Dessel (Sciensano)"

_INTRO_TITLE = "Link plain metadata text values to ORCID authorities in your SOLR core"
_INTRO_BODY = (
    "This script offers different modes to enrich your DSpace items with links to author ORCID "
    "authority records that are already in your SOLR authority core. "
    'As a prerequisite, any authors you want to link must already be present as "local" authorities: '
    "you need to have them already linked in at least one publication. "
    "The main reason is that searching and matching in your own local authorities has much higher "
    "accuracy, versus matching to the entire ORCID registry of all researchers worldwide."
)

# Default vocabulary for local author authority (SOLR cache); may be SolrAuthorAuthority on some instances
DEFAULT_AUTHORITY_VOCABULARY = "CacheableAuthorAuthority"


async def main() -> None:
    """Interactive flow: URL and credentials first, then optional review-each, then item UUID or all items."""
    show_script_attribution(SCRIPT_AUTHORS, console=console)

    console.print(
        Panel.fit(
            f"[bold cyan]{_INTRO_TITLE}[/bold cyan]\n\n{_INTRO_BODY}",
            border_style="cyan",
        )
    )
    console.print()

    # --- 1. Base URL ---
    base_url = console.input(
        "[bold cyan]DSpace base URL[/bold cyan] [dim](press Enter for https://demo.dspace.org):[/dim] "
    ).strip()
    if not base_url:
        base_url = "https://demo.dspace.org"
        console.print("[dim]→ Using default: https://demo.dspace.org[/dim]")

    # --- 2. Credentials ---
    base_url_normalized = base_url.rstrip("/").lower()
    is_demo = "demo.dspace.org" in base_url_normalized
    if is_demo:
        console.print("[dim]Using demo credentials: dspacedemo+admin@gmail.com[/dim]")
        username = "dspacedemo+admin@gmail.com"
        password = "dspace"
    else:
        username = console.input("[bold cyan]Admin username:[/bold cyan] ").strip()
        password = getpass.getpass("Admin password: ")

    # --- 3. Vocabulary name ---
    vocab_input = console.input(
        f"[bold cyan]Author authority vocabulary[/bold cyan] [dim](press Enter for {DEFAULT_AUTHORITY_VOCABULARY}):[/dim] "
    ).strip()
    vocabulary_name = vocab_input or DEFAULT_AUTHORITY_VOCABULARY

    # --- 4. Throttle ---
    throttle_input = console.input(
        "[bold cyan]Throttle delay (seconds)[/bold cyan] [dim](press Enter for 1.0):[/dim] "
    ).strip()
    try:
        courtesy_delay = float(throttle_input) if throttle_input else 1.0
    except ValueError:
        courtesy_delay = 1.0

    # --- Authenticate and create client ---
    auth = DSpaceAuthClient(base_url)
    auth.show_atmire_promo = True
    try:
        jwt, _status = await prompt_and_authenticate(
            auth,
            username,
            password,
            console=console,
        )
    except AuthenticationError as e:
        console.print(f"[red]{e}[/red]")
        await auth.close()
        return

    client = DSpaceClient(
        base_url=base_url,
        jwt_token=jwt,
        csrf_token=auth.csrf_token,
        http_client=auth.client,
        target_versions=TARGET_VERSIONS,
        courtesy_delay=courtesy_delay,
    )

    # --- Adaptive throttle (single-threaded, delay-based) ---
    throttle_config = ThrottleConfig(initial_delay=courtesy_delay)
    throttle = ThrottleController(throttle_config)

    # --- 5. Exact or Fuzzy matching ---
    while True:
        match_mode = console.input(
            "[bold cyan]Match author names by: Exact or Fuzzy?[/bold cyan] (type [bold]Exact[/bold] or [bold]Fuzzy[/bold]): "
        ).strip()
        if match_mode.lower() == "exact":
            use_fuzzy = False
            break
        if match_mode.lower() == "fuzzy":
            console.print(
                "[yellow]Fuzzy matching may link publications to the wrong ORCID author.[/yellow]"
            )
            confirm = (
                console.input("[bold cyan]Continue with fuzzy matching?[/bold cyan] (Yes/No): ")
                .strip()
                .lower()
            )
            if confirm in ("yes", "y"):
                use_fuzzy = True
                break
            console.print("[dim]Please choose Exact or Fuzzy.[/dim]")
            continue
        console.print("[red]Please type exactly [bold]Exact[/bold] or [bold]Fuzzy[/bold].[/red]")

    # --- 6. Auto-link single unambiguous matches? ---
    review_ans = (
        console.input(
            "[bold cyan]Allow automatic linking when there is exactly one local authority match?[/bold cyan] (y/n): "
        )
        .strip()
        .lower()
    )
    auto_link_single = review_ans in ("y", "yes")

    # --- 7. Mode: Item / Repository / ORCID / Name ---
    mode_prompt = (
        "[bold cyan]How would you like to link local authority records to items?[/bold cyan]\n"
        "[I]tem - you provide a uuid for one specific item, and I try to match as many unlinked authors in the item as possible\n"
        "[R]epository - I go over a wide range of items in the repository (different selections possible), where I try to match as many unlinked authors in the items in scope as possible.\n"
        "[O]RCID - You give me one specific ORCID id, for a local authority that already exists, and I try to find unlinked author names based on a range of fuzzy searches for the name of your ORCID author.\n"
        "[N]ame - You give me a (text) name and I try to find items that have this name, to which we can link an ORCID ID from your local authority cache. Optionally, you can provide a specific ORCID ID that we should be linking to for your search (even when the name is totally different than the name on the ORCID ID).\n"
        "[dim]Enter I, R, O, or N:[/dim] "
    )
    while True:
        mode_input = console.input(mode_prompt).strip().lower()
        if mode_input in ("i", "item"):
            run_mode_key = "item"
            break
        if mode_input in ("r", "repository"):
            run_mode_key = "repository"
            break
        if mode_input in ("o", "orcid"):
            run_mode_key = "orcid"
            break
        if mode_input in ("n", "name"):
            run_mode_key = "name"
            break
        console.print("[red]Please enter I, R, O, or N.[/red]")

    run_mode = "force"
    min_age_days: int | None = None
    repository_resume = False
    max_items_this_run: int | None = None
    if run_mode_key == "repository":
        mode_input = (
            console.input(
                "[bold cyan]Run mode[/bold cyan] "
                "[dim]([N]ew only, [S]ince days, [F]orce all; press Enter for New only):[/dim] "
            )
            .strip()
            .lower()
        )
        if mode_input in ("s", "since"):
            run_mode = "since"
            days_str = console.input(
                "[bold cyan]Re-run items not updated for at least how many days?[/bold cyan]: "
            ).strip()
            try:
                min_age_days = int(days_str)
            except ValueError:
                min_age_days = 0
        elif mode_input in ("f", "force"):
            run_mode = "force"
            min_age_days = None
        else:
            run_mode = "new"
            min_age_days = None

        resume_input = (
            console.input(
                "[bold cyan]Resume from previous repository checkpoint?[/bold cyan] "
                "[dim](y/n, press Enter for y):[/dim] "
            )
            .strip()
            .lower()
        )
        repository_resume = resume_input not in ("n", "no")

        max_items_input = console.input(
            f"[bold cyan]Max items to process in this run[/bold cyan] "
            f"[dim](press Enter for auto: {DEFAULT_AUTO_CHUNK_SIZE} when repo is large):[/dim] "
        ).strip()
        if max_items_input:
            try:
                parsed = int(max_items_input)
                max_items_this_run = parsed if parsed > 0 else None
            except ValueError:
                max_items_this_run = None

    # --- Log file ---
    log_dir = os.environ.get("LINK_AUTHOR_LOG_DIR", ".")
    log_filename = datetime.now().strftime("link_author_authorities_%Y-%m-%d_%H-%M-%S.log")
    log_path = os.path.join(log_dir, log_filename)
    try:
        log_file = open(log_path, "w", encoding="utf-8")
    except OSError as e:
        console.print(f"[yellow]Could not open log file {log_path}: {e}[/yellow]")
        log_file = None
    else:
        console.print(f"[dim]Log file: {log_path}[/dim]")

    # --- Incremental state (per item UUID) ---
    state_path = _get_state_path(log_dir, base_url)
    attempt_state = _load_attempt_state(state_path)
    checkpoint_path = _get_checkpoint_path(log_dir, base_url)
    now = datetime.now()

    total_linked = 0
    total_skipped = 0
    total_no_match = 0
    items_processed = 0
    fatal_auth = False

    try:
        if run_mode_key == "item":
            item_uuid_input = (
                sys.argv[1].strip()
                if len(sys.argv) > 1
                else console.input("[bold cyan]Item UUID:[/bold cyan] ").strip()
            )
            if not item_uuid_input:
                console.print("[yellow]No item UUID provided; skipping.[/yellow]")
            else:
                console.print(f"[cyan]Processing item {item_uuid_input}[/cyan]")
                linked, skipped, no_match = await process_item(
                    auth,
                    client,
                    username,
                    password,
                    throttle,
                    item_uuid_input,
                    vocabulary_name,
                    auto_link_single,
                    use_fuzzy,
                    log_file,
                )
                total_linked += linked
                total_skipped += skipped
                total_no_match += no_match
                items_processed = 1

        elif run_mode_key == "repository":
            checkpoint = _load_repo_checkpoint(checkpoint_path) if repository_resume else {}
            start_page = 0
            if repository_resume:
                start_page = int(checkpoint.get("next_page", 0) or 0)
                if start_page > 0:
                    console.print(
                        f"[dim]Resuming repository scan from page {start_page} "
                        f"(checkpoint: {checkpoint_path}).[/dim]"
                    )
            else:
                _clear_repo_checkpoint(checkpoint_path)

            auto_chunk_threshold = DEFAULT_AUTO_CHUNK_THRESHOLD
            try:
                auto_chunk_threshold = int(
                    os.environ.get(
                        AUTO_CHUNK_THRESHOLD_ENV_VAR,
                        str(DEFAULT_AUTO_CHUNK_THRESHOLD),
                    )
                )
            except ValueError:
                auto_chunk_threshold = DEFAULT_AUTO_CHUNK_THRESHOLD

            page_size = 100
            page = start_page
            global_seen = 0
            discovered_total: int | None = None
            hit_run_limit = False

            while True:
                try:
                    page_uuids, total_elements = await _fetch_discovery_page_item_uuids(
                        auth,
                        client,
                        username,
                        password,
                        throttle,
                        page=page,
                        page_size=page_size,
                    )
                except Exception as e:
                    console.print(f"[red]Repository discovery failed at page {page}: {e}[/red]")
                    _log(
                        log_file,
                        f"ERROR repository_discovery_failed page={page} error={e!r}",
                    )
                    checkpoint_payload = {
                        "next_page": page,
                        "updated_at": datetime.now().isoformat(),
                        "run_mode": run_mode,
                    }
                    if discovered_total is not None:
                        checkpoint_payload["total_elements"] = discovered_total
                    _save_repo_checkpoint(checkpoint_path, checkpoint_payload)
                    break

                if discovered_total is None and total_elements is not None:
                    discovered_total = total_elements
                    console.print(
                        f"[cyan]Repository reports ~{discovered_total} item(s) total.[/cyan]"
                    )
                    if max_items_this_run is None and discovered_total >= auto_chunk_threshold:
                        max_items_this_run = DEFAULT_AUTO_CHUNK_SIZE
                        console.print(
                            "[yellow]Large repository detected; enabling chunked run "
                            f"({max_items_this_run} items max this run).[/yellow]"
                        )

                if not page_uuids:
                    console.print("[green]Repository scan complete.[/green]")
                    _clear_repo_checkpoint(checkpoint_path)
                    break

                for uuid in page_uuids:
                    global_seen += 1
                    if not _should_process_uuid(uuid, run_mode, attempt_state, now, min_age_days):
                        console.print(
                            f"[dim]Item {global_seen}: {uuid} – skipped by incremental run mode.[/dim]"
                        )
                        continue

                    console.print(f"[dim]Item {global_seen}: {uuid}[/dim]")
                    linked, skipped, no_match = await process_item(
                        auth,
                        client,
                        username,
                        password,
                        throttle,
                        uuid,
                        vocabulary_name,
                        auto_link_single,
                        use_fuzzy,
                        log_file,
                    )
                    total_linked += linked
                    total_skipped += skipped
                    total_no_match += no_match
                    items_processed += 1
                    attempt_state[uuid] = now
                    _append_attempt_state(state_path, uuid, now)

                    if max_items_this_run is not None and items_processed >= max_items_this_run:
                        hit_run_limit = True
                        break

                next_page = page + 1
                checkpoint_payload = {
                    "next_page": next_page,
                    "updated_at": datetime.now().isoformat(),
                    "run_mode": run_mode,
                }
                if discovered_total is not None:
                    checkpoint_payload["total_elements"] = discovered_total
                _save_repo_checkpoint(checkpoint_path, checkpoint_payload)

                if hit_run_limit:
                    console.print(
                        f"[yellow]Reached run limit ({max_items_this_run} items). "
                        "You can rerun in repository mode with resume enabled.[/yellow]"
                    )
                    break

                page = next_page

        elif run_mode_key == "orcid":
            orcid_input = console.input(
                "[bold cyan]ORCID id[/bold cyan] [dim](e.g. 0000-0002-1825-0097 or full URL):[/dim] "
            ).strip()
            if not orcid_input:
                console.print("[yellow]No ORCID provided; skipping.[/yellow]")
            elif not normalize_orcid_identifier(orcid_input):
                console.print(
                    "[yellow]Could not parse a valid ORCID. Use hyphenated form "
                    "(e.g. 0000-0002-1825-0097; the last character may be X), "
                    "or a profile URL such as https://orcid.org/… or https://www.orcid.org/…[/yellow]"
                )
            else:
                console.print("[dim]Resolving ORCID to local authority...[/dim]")
                resolved = await resolve_authority_by_orcid(
                    client,
                    vocabulary_name,
                    orcid_input,
                    auth,
                    username,
                    password,
                    throttle,
                )
                if not resolved:
                    console.print(
                        "[red]No local authority found for that ORCID. "
                        "Ensure the authority exists in your vocabulary.[/red]"
                    )
                else:
                    authority_uuid, display_name = resolved
                    console.print(
                        f"[green]Resolved to:[/green] {display_name!r} (authority={authority_uuid})"
                    )
                    console.print("[cyan]Discovering items by author name...[/cyan]")
                    uuids = await discover_item_uuids_by_author(
                        auth, client, username, password, throttle, display_name
                    )
                    console.print(
                        f"[cyan]Found {len(uuids)} item(s) with that author. Processing each.[/cyan]"
                    )
                    for i, uuid in enumerate(uuids, 1):
                        console.print(f"[dim]Item {i}/{len(uuids)}: {uuid}[/dim]")
                        linked, skipped, no_match = await process_item(
                            auth,
                            client,
                            username,
                            password,
                            throttle,
                            uuid,
                            vocabulary_name,
                            auto_link_single,
                            use_fuzzy,
                            log_file,
                            target_authority=(authority_uuid, display_name),
                        )
                        total_linked += linked
                        total_skipped += skipped
                        total_no_match += no_match
                        items_processed += 1
                        attempt_state[uuid] = now
                        _append_attempt_state(state_path, uuid, now)

        else:  # run_mode_key == "name"
            name_input = console.input(
                "[bold cyan]Author name[/bold cyan] [dim](as it may appear in items):[/dim] "
            ).strip()
            if not name_input:
                console.print("[yellow]No name provided; skipping.[/yellow]")
            else:
                orcid_opt = console.input(
                    "[bold cyan]Optional: ORCID id to link to[/bold cyan] [dim](press Enter to skip):[/dim] "
                ).strip()
                target_authority: tuple[str, str] | None = None
                if orcid_opt:
                    if not normalize_orcid_identifier(orcid_opt):
                        console.print(
                            "[yellow]Could not parse that ORCID; skipping optional authority link. "
                            "Use hyphenated id or an orcid.org / www.orcid.org profile URL.[/yellow]"
                        )
                    else:
                        console.print("[dim]Resolving ORCID to local authority...[/dim]")
                        resolved = await resolve_authority_by_orcid(
                            client,
                            vocabulary_name,
                            orcid_opt,
                            auth,
                            username,
                            password,
                            throttle,
                        )
                        if resolved:
                            target_authority = resolved
                            console.print(
                                f"[green]Will link to:[/green] {target_authority[1]!r} "
                                f"(authority={target_authority[0]})"
                            )
                        else:
                            console.print(
                                "[yellow]No local authority for that ORCID; will match from vocabulary per author.[/yellow]"
                            )

                console.print("[cyan]Discovering items by author name...[/cyan]")
                uuids = await discover_item_uuids_by_author(
                    auth, client, username, password, throttle, name_input
                )
                console.print(f"[cyan]Found {len(uuids)} item(s). Processing each.[/cyan]")
                for i, uuid in enumerate(uuids, 1):
                    console.print(f"[dim]Item {i}/{len(uuids)}: {uuid}[/dim]")
                    linked, skipped, no_match = await process_item(
                        auth,
                        client,
                        username,
                        password,
                        throttle,
                        uuid,
                        vocabulary_name,
                        auto_link_single,
                        use_fuzzy,
                        log_file,
                        target_authority=target_authority,
                        filter_author_name=name_input if not target_authority else None,
                    )
                    total_linked += linked
                    total_skipped += skipped
                    total_no_match += no_match
                    items_processed += 1
                    attempt_state[uuid] = now
                    _append_attempt_state(state_path, uuid, now)
    except AuthenticationError as e:
        fatal_auth = True
        console.print(
            "[red]Fatal authentication error (e.g. CSRF/login refresh failed). Aborting run.[/red]"
        )
        console.print(f"[dim]{e}[/dim]")
    finally:
        # Summary (normal completion and on auth failure)
        console.print("\n[bold cyan]Summary[/bold cyan]")
        console.print(f"  Items processed: {items_processed}")
        console.print(f"  Linked: {total_linked}")
        console.print(f"  Skipped (user said no): {total_skipped}")
        console.print(f"  No local match: {total_no_match}")

        if log_file is not None:
            _log(
                log_file,
                f"SUMMARY items_processed={items_processed} linked={total_linked} skipped={total_skipped} no_match={total_no_match}",
            )
            log_file.close()
        await auth.close()

    if fatal_auth:
        sys.exit(1)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
