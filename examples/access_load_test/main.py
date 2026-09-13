"""
Access load test for DSpace: humans in real headless browsers, good bots, bad bots.

    python examples/access_load_test/main.py                      # interactive
    python examples/access_load_test/main.py --scenario good-bots --base-url https://repo.example.org \\
        --users 5 --think-time 1 --duration 10m --i-own-this-host repo.example.org

Scenarios: human, good-bots, bad-bots, mix. See README.md in this folder.

ONLY run this against DSpace instances you own or have written permission to load test.
It is designed to find the point where a server degrades, which means it can and will
degrade the server. The DSpace community demo hosts are hard-blocked.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from access_load_test.config import (
    SCENARIOS,
    ConfigError,
    MixConfig,
    RunConfig,
    check_host_allowed,
    estimate_resources,
    parse_duration,
    parse_mix,
    parse_ramp,
)
from access_load_test.orchestrator import run_load_test
from access_load_test.report import write_reports
from access_load_test.reqlog import RequestLog
from dspace_client import (
    ServerVersionMismatchError,
    create_anonymous_client,
    show_script_attribution,
)

# DEVELOPER DECLARES: read-only public endpoints (discovery, items, bitstream content,
# sitemaps, OAI) plus whatever the Angular UI itself calls. Human traffic is modelled on the
# DSpace 7+ Angular UI.
TARGET_VERSIONS = ["7.6", "8.0", "9.0", "10.0"]
SCRIPT_AUTHORS = "Bram Luyten (Atmire)"

console = Console()

WARNING_TEXT = """[bold red]THIS TOOL GENERATES REAL LOAD AND IS MEANT TO FIND THE POINT WHERE A SERVER DEGRADES.[/bold red]

Run it ONLY against a DSpace instance that you own, or for which you hold explicit written
permission to load test. Load testing somebody else's server is abuse, whatever the intent.

What it will do to the target:
  • open real headless browsers that hit the Angular UI, Node SSR, REST API and Solr at full speed;
  • crawl as a well-behaved bot and as a bot that ignores robots.txt entirely;
  • download bitstreams (the bytes are counted and thrown away, never written to disk here);
  • pollute the Solr statistics core with view events and downloads;
  • send everything from ONE IP address, which per-IP rate limiters and WAFs will notice.

What it may do to the machine running it:
  • every simulated human is a Chromium tab; tens of them need several GB of RAM and real CPU.
    A saturated generator produces numbers that say nothing about the server.

Every request carries the header [bold]X-DSpace-Load-Test: <run id>[/bold] so operators can find it in logs.
Tell the people who run the target before you start. Prefer a staging copy. Prefer off-hours.
The DSpace community demo and sandbox hosts are blocked in this tool, with no override."""


def _print_banner() -> None:
    show_script_attribution(SCRIPT_AUTHORS, console=console)
    console.print(Panel(WARNING_TEXT, title="READ THIS FIRST", border_style="red", expand=False))
    console.print("[bold]Supported DSpace versions:[/bold] " + ", ".join(TARGET_VERSIONS))
    console.print()


# ---- CLI ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Access load test for DSpace (humans via headless browsers, good bots, bad bots).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--base-url", help="DSpace base URL without /server (REST is at <base>/server/api)"
    )
    p.add_argument("--ui-url", help="Angular UI URL if different from the base URL")
    p.add_argument("--scenario", choices=SCENARIOS, help="human | good-bots | bad-bots | mix")
    p.add_argument("--users", type=int, help="number of parallel simulated users")
    p.add_argument(
        "--think-time", type=float, help="mean seconds between one user's actions (0 allowed)"
    )
    p.add_argument(
        "--fixed-think-time", action="store_true", help="constant think time instead of lognormal"
    )
    p.add_argument("--duration", help="load phase length, e.g. 300, 5m, 1h")
    p.add_argument(
        "--baseline", default="30s", help="baseline phase length with a single user (0 to skip)"
    )
    p.add_argument("--window", type=float, default=10.0, help="metrics window in seconds")
    p.add_argument("--ramp", help="add users gradually: start=2,step=2,every=60s")
    p.add_argument("--mix", help="persona split for the mix scenario, e.g. human=50,good=20,bad=30")
    p.add_argument("--no-downloads", action="store_true", help="never download bitstreams")
    p.add_argument(
        "--download-probability",
        type=float,
        default=0.25,
        help="chance a page visit with a file link downloads it",
    )
    p.add_argument(
        "--max-download-mb", type=float, default=50.0, help="per-download byte cap (0 = unlimited)"
    )
    p.add_argument(
        "--max-total-download-gb",
        type=float,
        default=5.0,
        help="run-wide download budget (0 = unlimited)",
    )
    p.add_argument(
        "--max-inflight",
        type=int,
        default=200,
        help="hard cap on concurrent httpx requests (safety valve)",
    )
    p.add_argument(
        "--bad-bot-open-loop",
        action="store_true",
        help="bad bots launch fetches on a fixed interval regardless of completion",
    )
    p.add_argument("--bad-bot-max-depth", type=int, default=6)
    p.add_argument(
        "--good-bot-ua",
        help="override the good-bot user agent (e.g. a Googlebot string; changes WAF behaviour)",
    )
    p.add_argument(
        "--oai-share",
        type=float,
        default=0.2,
        help="fraction of good bots that harvest OAI-PMH instead of crawling",
    )
    p.add_argument(
        "--no-view-events",
        action="store_true",
        help="block the browser's statistics view/search events (less statistics pollution, less realism)",
    )
    p.add_argument(
        "--humans-per-browser", type=int, default=10, help="browser contexts per Chromium process"
    )
    p.add_argument("--headed", action="store_true", help="show the browsers (debugging only)")
    p.add_argument("--no-auto-stop", action="store_true", help="keep going past the breaking point")
    p.add_argument(
        "--stop-at-onset",
        action="store_true",
        help="stop as soon as degradation onset is confirmed",
    )
    p.add_argument(
        "--onset-factor", type=float, default=1.5, help="rolling p50 must exceed baseline * factor"
    )
    p.add_argument(
        "--seed",
        type=int,
        help="RNG seed for reproducible runs (default: random, printed in the report)",
    )
    p.add_argument("--output-dir", type=Path, default=Path("access-load-reports"))
    p.add_argument("--no-requests-log", action="store_true", help="skip the per-request JSONL file")
    p.add_argument("--requests-log-max-mb", type=float, default=200.0)
    p.add_argument(
        "--dry-run", action="store_true", help="discover targets and print the plan; send no load"
    )
    p.add_argument("--skip-version-check", action="store_true")
    p.add_argument(
        "--force-resources",
        action="store_true",
        help="ignore the generator-host RAM/CPU guard and the human cap",
    )
    p.add_argument(
        "--max-human-users", type=int, default=60, help="per-host cap on simulated humans"
    )
    p.add_argument(
        "--i-own-this-host",
        metavar="HOST",
        help="non-interactive confirmation: must equal the target hostname",
    )
    return p


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [dim](Enter for {default})[/dim]" if default is not None else ""
    ans = console.input(f"[bold cyan]{prompt}[/bold cyan]{suffix}: ").strip()
    return ans or (default or "")


def _build_config(args: argparse.Namespace) -> RunConfig:
    interactive = sys.stdin.isatty() and args.i_own_this_host is None
    base_url = args.base_url
    if not base_url:
        if not interactive:
            raise ConfigError("--base-url is required in non-interactive mode")
        base_url = _ask("DSpace base URL (without /server)", "http://localhost:4000")
    check_host_allowed(base_url.split("//", 1)[-1].split("/", 1)[0].split(":")[0])
    ui_url = args.ui_url
    if ui_url is None and interactive:
        ui_url = _ask("Angular UI URL", base_url) or None
        if ui_url == base_url:
            ui_url = None
    scenario = args.scenario or (
        _ask("Scenario (human / good-bots / bad-bots / mix)", "mix") if interactive else None
    )
    if scenario not in SCENARIOS:
        raise ConfigError(f"scenario must be one of {', '.join(SCENARIOS)}")
    users = args.users
    if users is None:
        if not interactive:
            raise ConfigError("--users is required in non-interactive mode")
        users = int(_ask("Parallel simulated users", "5"))
    think = args.think_time
    if think is None:
        if not interactive:
            raise ConfigError("--think-time is required in non-interactive mode")
        console.print(
            "[dim]Think time is the mean gap between one user's actions (page visit, crawl fetch, search).\n"
            "Requests the browser makes to build a page are never slowed down. 0 is allowed.[/dim]"
        )
        think = float(_ask("Mean think time in seconds", "2"))
    duration = args.duration
    if duration is None:
        if not interactive:
            raise ConfigError("--duration is required in non-interactive mode")
        duration = _ask("Load phase duration", "5m")
    mix = parse_mix(args.mix) if args.mix else MixConfig()
    if scenario == "mix" and not args.mix and interactive:
        mix = parse_mix(_ask("Persona split", "human=50,good=20,bad=30"))
    downloads = not args.no_downloads
    if interactive and not args.no_downloads:
        downloads = _ask("Download bitstreams? (yes/no)", "yes").lower() in ("y", "yes")

    cfg = RunConfig(
        base_url=base_url.rstrip("/"),
        ui_url=ui_url.rstrip("/") if ui_url else None,
        scenario=scenario,
        users=users,
        think_time_s=think,
        duration_s=parse_duration(duration),
        baseline_s=parse_duration(args.baseline) if args.baseline not in ("0", "0s", "") else 0.0,
        window_s=args.window,
        ramp=parse_ramp(args.ramp) if args.ramp else None,
        mix=mix,
        fixed_think_time=args.fixed_think_time,
        downloads_enabled=downloads,
        download_probability=args.download_probability,
        max_download_mb=args.max_download_mb,
        max_total_download_gb=args.max_total_download_gb,
        max_inflight=args.max_inflight,
        bad_bot_open_loop=args.bad_bot_open_loop,
        bad_bot_max_depth=args.bad_bot_max_depth,
        oai_share=args.oai_share,
        view_events=not args.no_view_events,
        humans_per_browser=args.humans_per_browser,
        headless=not args.headed,
        auto_stop=not args.no_auto_stop,
        stop_at_onset=args.stop_at_onset,
        onset_factor=args.onset_factor,
        seed=args.seed,
        output_dir=args.output_dir,
        requests_log=not args.no_requests_log,
        requests_log_max_mb=args.requests_log_max_mb,
        dry_run=args.dry_run,
        skip_version_check=args.skip_version_check,
        force_resources=args.force_resources,
        max_human_users=args.max_human_users,
    )
    if args.good_bot_ua:
        cfg.good_bot_ua = args.good_bot_ua
    cfg.validate()
    return cfg


def _print_plan(cfg: RunConfig) -> None:
    personas = cfg.personas()
    t = Table(title="Planned run", show_header=False, expand=False)
    t.add_column("k", style="bold")
    t.add_column("v")
    t.add_row("Target UI", cfg.ui_base)
    t.add_row("Target REST", cfg.rest_base)
    t.add_row("Scenario", cfg.scenario)
    t.add_row(
        "Users",
        f"{cfg.users}  (human {personas.count('human')}, good-bot {personas.count('good-bot')}, bad-bot {personas.count('bad-bot')})",
    )
    t.add_row(
        "Think time",
        f"mean {cfg.think_time_s}s {'fixed' if cfg.fixed_think_time else 'lognormal'}; browsers never throttled",
    )
    t.add_row(
        "Ramp",
        f"start {cfg.ramp.start}, +{cfg.ramp.step} every {cfg.ramp.every_s:.0f}s"
        if cfg.ramp
        else "none",
    )
    t.add_row(
        "Baseline / load", f"{cfg.baseline_s:.0f}s single user, then {cfg.duration_s:.0f}s load"
    )
    t.add_row(
        "Downloads",
        f"{'on' if cfg.downloads_enabled else 'off'}; p={cfg.download_probability}; cap {cfg.max_download_mb} MB each, {cfg.max_total_download_gb} GB total; nothing stored",
    )
    t.add_row(
        "Bad bots",
        "open loop (fixed launch rate)"
        if cfg.bad_bot_open_loop
        else "closed loop (wait for each response)",
    )
    t.add_row(
        "Auto-stop",
        ("at breaking point" + (" or onset" if cfg.stop_at_onset else ""))
        if cfg.auto_stop
        else "off",
    )
    t.add_row("Run id", cfg.run_id)
    t.add_row("Reports", str(cfg.output_dir.resolve()))
    console.print(t)


def _resource_guard(cfg: RunConfig) -> None:
    if not cfg.needs_browser:
        return
    est = estimate_resources(cfg)
    m = est.machine
    console.print(
        Panel(
            f"Simulated humans: [bold]{est.human_users}[/bold] Chromium tabs in {est.browsers} browser process(es)\n"
            f"Estimated need:   [bold]{est.ram_mb} MB RAM[/bold], about [bold]{est.cpu_cores} CPU cores[/bold] busy\n"
            f"This machine:     {m.ram_mb or '?'} MB RAM, {m.cpu_count or '?'} cores\n\n"
            "If the generator saturates, the report flags the run as unreliable. Split large human\n"
            "counts over several machines rather than forcing one.",
            title="Generator host resources",
            border_style="yellow" if not est.over_budget else "red",
            expand=False,
        )
    )
    if est.over_budget and not cfg.force_resources:
        raise ConfigError(
            "The estimated browser footprint exceeds a safe share of this machine "
            "(70 % of RAM or all cores). Lower --users, or pass --force-resources if you have measured otherwise."
        )


def _confirm(cfg: RunConfig, args: argparse.Namespace) -> bool:
    host = cfg.target_host
    if args.i_own_this_host is not None:
        if args.i_own_this_host.strip().lower() != host.lower():
            raise ConfigError(
                f"--i-own-this-host {args.i_own_this_host!r} does not match the target host {host!r}"
            )
        return True
    console.print(
        f"\n[bold yellow]Type the target hostname to confirm you own it and accept the consequences:[/bold yellow] [bold]{host}[/bold]"
    )
    typed = console.input("> ").strip().lower()
    if typed != host.lower():
        console.print("[red]Hostname did not match. Nothing was sent.[/red]")
        return False
    return True


async def _version_check(cfg: RunConfig) -> str | None:
    http, client = await create_anonymous_client(
        cfg.base_url, TARGET_VERSIONS, timeout=30.0, courtesy_delay=0.0
    )
    try:
        return client.last_detected_server_version
    finally:
        await http.aclose()


async def _main_async(args: argparse.Namespace) -> int:
    _print_banner()
    try:
        cfg = _build_config(args)
    except (ConfigError, ValueError) as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        return 2
    if cfg.needs_browser:
        from access_load_test.persona_human import playwright_available

        if not playwright_available():
            console.print(
                "[red]The human persona needs Playwright.[/red] Install with:\n"
                '  pip install -e ".[loadtest]"\n  playwright install chromium'
            )
            return 2
    _print_plan(cfg)
    try:
        _resource_guard(cfg)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        return 2
    if not cfg.skip_version_check:
        try:
            version = await _version_check(cfg)
            console.print(f"[green]Server version:[/green] {version or 'unknown'}")
        except ServerVersionMismatchError as e:
            console.print(f"[red]Version check failed:[/red] {e}")
            return 2
        except Exception as e:
            console.print(
                f"[yellow]Version check could not complete ({e}). Continue at your own risk or pass --skip-version-check.[/yellow]"
            )
            if args.i_own_this_host is None and console.input(
                "Continue anyway? (yes/no): "
            ).strip().lower() not in ("y", "yes"):
                return 2
    if not cfg.dry_run and not _confirm(cfg, args):
        return 1

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    request_log = None
    if cfg.requests_log and not cfg.dry_run:
        from datetime import UTC, datetime

        from access_load_test.report import sanitize_hostname

        stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H.%M")
        log_path = (
            cfg.output_dir
            / f"{stamp}-access-load-{cfg.scenario}-{sanitize_hostname(cfg.target_host)}-requests.jsonl"
        )
        request_log = RequestLog(log_path, int(cfg.requests_log_max_mb * 1024 * 1024))

    console.print(
        "\n[bold]Starting.[/bold] Press Ctrl-C once to stop early and still get reports; twice to abort.\n"
    )
    try:
        result = await run_load_test(cfg, console, request_log=request_log)
    finally:
        if request_log is not None:
            request_log.close()
    if request_log is not None:
        result.requests_log = request_log.summary()
    if cfg.dry_run:
        console.print(
            "[green]Dry run complete.[/green] Target pool and robots.txt were fetched; no load was sent."
        )
        console.print(result.pool_summary)
        return 0
    paths = write_reports(result, cfg.output_dir)
    v = result.verdict
    colour = {"healthy": "green", "degraded": "yellow", "breaking": "red"}[v.status]
    console.print(
        f"\n[bold {colour}]Verdict: {v.status.upper()}[/bold {colour}]  (stop reason: {result.stop_reason})"
    )
    if v.onset:
        console.print(
            f"  onset at {v.onset.at_s:.0f}s with {v.onset.active_users} users, {v.onset.rps:.1f} req/s: {v.onset.reason}"
        )
    if v.breaking:
        console.print(
            f"  breaking at {v.breaking.at_s:.0f}s with {v.breaking.active_users} users: {v.breaking.reason}"
        )
    if v.generator_unreliable:
        console.print("[bold red]  generator saturated; results unreliable[/bold red]")
    console.print("\n[bold]Reports:[/bold]")
    for k, p in paths.items():
        console.print(f"  {k:9s} {p}")
    if request_log is not None:
        console.print(f"  requests  {request_log.path}")
    return 0


def main() -> None:
    args = build_parser().parse_args()
    try:
        code = asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        console.print("\n[red]Aborted.[/red]")
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
