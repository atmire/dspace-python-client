"""
Reports: summary Markdown, extended Markdown, raw JSON (for tools and AI), plus the
per-request JSONL written during the run. Filenames follow the MegaSpace convention:
``YYYY-MM-DD-HH.MM-access-load-{scenario}-{host}-{kind}``, UTC.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from access_load_test.metrics import CLASS_ORDER, WindowStats
from access_load_test.orchestrator import RunResult

SCHEMA_VERSION = "1.0"


def sanitize_hostname(host: str) -> str:
    h = re.sub(r"[^a-z0-9._-]+", "-", host.lower())
    return h.strip("-") or "unknown"


def report_basename(result: RunResult) -> str:
    now = datetime.now(UTC)
    return f"{now:%Y-%m-%d-%H.%M}-access-load-{result.cfg.scenario}-{sanitize_hostname(result.cfg.target_host)}"


# ---- payload ---------------------------------------------------------------------------


def _cfg_dict(result: RunResult) -> dict:
    d = asdict(result.cfg)
    d["output_dir"] = str(result.cfg.output_dir)
    d["ui_base"] = result.cfg.ui_base
    d["rest_base"] = result.cfg.rest_base
    d["personas"] = {p: result.cfg.personas().count(p) for p in ("human", "good-bot", "bad-bot")}
    return d


def _class_table_for(windows: list[WindowStats]) -> dict[str, dict]:
    """Pool load-phase windows per class (count-weighted percentiles are approximated by max/mean)."""
    out: dict[str, dict] = {}
    classes = {c for w in windows for c in w.by_class}
    for c in sorted(classes, key=lambda x: CLASS_ORDER.index(x) if x in CLASS_ORDER else 99):
        rows = [(w.by_class[c], w.end_ts) for w in windows if c in w.by_class]
        n = sum(r.count for r, _ in rows)
        if n == 0:
            continue
        p50 = sum(r.p50 * r.count for r, _ in rows) / n
        p95 = sum(r.p95 * r.count for r, _ in rows) / n
        errors = sum(r.errors for r, _ in rows)
        server_errors = sum(r.server_errors for r, _ in rows)
        first, last = rows[0][0], rows[-1][0]
        out[c] = {
            "requests": n,
            "errors": errors,
            "server_errors": server_errors,
            "error_rate": round(errors / n, 4),
            "server_error_rate": round(server_errors / n, 4),
            "p50_s": round(p50, 4),
            "p95_s": round(p95, 4),
            "p99_max_s": round(max(r.p99 for r, _ in rows), 4),
            "bytes": sum(r.bytes_received for r, _ in rows),
            "first_window_p50_s": round(first.p50, 4),
            "last_window_p50_s": round(last.p50, 4),
            "last_vs_first_p50_ratio": round(last.p50 / first.p50, 3) if first.p50 > 0 else None,
            "mb_per_s": round(
                sum(r.mb_per_s for r, _ in rows if r.mb_per_s)
                / max(1, sum(1 for r, _ in rows if r.mb_per_s)),
                3,
            )
            if any(r.mb_per_s for r, _ in rows)
            else None,
        }
    return out


def build_payload(result: RunResult) -> dict:
    load_windows = [w for w in result.windows if w.phase == "load"]
    baseline_windows = [w for w in result.windows if w.phase == "baseline"]
    per_class = _class_table_for(load_windows)
    baseline_class = _class_table_for(baseline_windows)
    # Attach the baseline comparison to each class for the reader.
    for c, row in per_class.items():
        b = baseline_class.get(c)
        row["baseline_p50_s"] = b["p50_s"] if b else None
        row["load_vs_baseline_p50_ratio"] = (
            round(row["p50_s"] / b["p50_s"], 3) if b and b["p50_s"] > 0 else None
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "access-load-test",
        "run_id": result.cfg.run_id,
        "started_utc": result.started_utc,
        "ended_utc": result.ended_utc,
        "timezone": "UTC",
        "target_host": result.cfg.target_host,
        "stop_reason": result.stop_reason,
        "interrupted": result.interrupted,
        "dry_run": result.dry_run,
        "config": _cfg_dict(result),
        "phase_seconds": result.phase_seconds,
        "verdict": result.verdict.to_dict(),
        "signals": [s.to_dict() for s in result.signals],
        "baseline": result.baseline.to_dict() if result.baseline else None,
        "totals": result.totals,
        "per_class_load_phase": per_class,
        "per_class_baseline_phase": baseline_class,
        "status_counts": result.status_counts,
        "class_counts": result.class_counts,
        "persona_counts": result.persona_counts,
        "windows": [w.to_dict() for w in result.windows],
        "slowest_requests": result.slowest,
        "top_errors": result.top_errors,
        "target_pool": result.pool_summary,
        "queries": result.queries,
        "generator": result.generator,
        "persona_notes": result.persona_notes,
        "requests_log": result.requests_log,
        "analysis_hints": [
            "verdict.status is 'healthy', 'degraded' (latency trend up vs baseline) or 'breaking' (errors/timeouts/extreme p95).",
            "verdict.onset gives the first window where rolling p50 exceeded baseline * onset_factor for onset_confirm_windows windows, with the active user count and request rate at that moment.",
            "per_class_load_phase.<class>.load_vs_baseline_p50_ratio > 1.5 means that request class slowed markedly under load; compare 'search' (Solr) against 'ssr-*' (Node) and 'rest' (Tomcat) to see which tier gave way first.",
            "windows[] is the time series; windows[].offered_action_rate vs achieved_action_rate shows closed-loop self-regulation (users slowing down because the server did).",
            "generator.unreliable true means the load generator itself saturated; do not draw conclusions about the server from that run.",
            "status_counts with many 403/429 plus edge_blocks in windows means a CDN/WAF, not DSpace, answered.",
        ],
    }


# ---- markdown --------------------------------------------------------------------------


def _fmt_s(v: float | None) -> str:
    return "-" if v is None else f"{v:.3f}"


def _class_rows(table: dict[str, dict]) -> list[str]:
    lines = [
        "| Class | Requests | 4xx | Server err | p50 (s) | p95 (s) | Baseline p50 | Load/baseline | Last/first window | MB |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for c, r in table.items():
        four_xx = r["errors"] - r.get("server_errors", 0)
        lines.append(
            f"| {c} | {r['requests']} | {four_xx} | {r.get('server_errors', 0)} | {r['p50_s']:.3f} | {r['p95_s']:.3f} | "
            f"{_fmt_s(r.get('baseline_p50_s'))} | {r.get('load_vs_baseline_p50_ratio') or '-'} | "
            f"{r.get('last_vs_first_p50_ratio') or '-'} | {r['bytes'] / 1048576:.1f} |"
        )
    return lines


def _signal_line(label: str, s: dict | None) -> str:
    if not s:
        return f"- **{label}:** not observed"
    return (
        f"- **{label}:** at {s['at_s']:.0f}s into the load phase (window {s['window_index']}, "
        f"{s['active_users']} active users, {s['rps']:.1f} req/s), class `{s['req_class']}`: {s['reason']}"
    )


def render_summary(payload: dict) -> str:
    cfg = payload["config"]
    v = payload["verdict"]
    t = payload["totals"]
    status_word = {"healthy": "HEALTHY", "degraded": "DEGRADED", "breaking": "BREAKING"}[
        v["status"]
    ]
    lines = [
        f"# Access load test summary: {payload['target_host']}",
        "",
        f"- **Run id:** `{payload['run_id']}` (every request carried header `X-DSpace-Load-Test: {payload['run_id']}`)",
        f"- **Window (UTC):** {payload['started_utc']} to {payload['ended_utc']}",
        f"- **Scenario:** {cfg['scenario']} with {cfg['users']} users "
        f"(human {cfg['personas']['human']}, good-bot {cfg['personas']['good-bot']}, bad-bot {cfg['personas']['bad-bot']})",
        f"- **Think time:** mean {cfg['think_time_s']}s between actions ({'fixed' if cfg['fixed_think_time'] else 'lognormal'}); "
        f"browser requests were never throttled",
        f"- **Ramp:** {cfg['ramp'] or 'none (all users started within the first seconds)'}",
        f"- **Stop reason:** {payload['stop_reason']}",
        f"- **Seed:** {cfg['seed']}",
        "",
        f"## Verdict: {status_word}",
        "",
        "_Breaking point keys on server-fault responses (5xx, 429, timeouts), not on benign "
        "4xx such as the Angular app's anonymous 401 probes or a crawler's 404 on a stale link._",
        "",
        _signal_line("Degradation onset", v["onset"]),
        _signal_line("Breaking point", v["breaking"]),
        _signal_line(
            "Throughput collapse (achieved vs offered actions, only flagged alongside latency onset)",
            v["collapse"],
        ),
    ]
    if v["onset_by_class"]:
        lines.append("- **Onset per request class:**")
        for c, s in sorted(v["onset_by_class"].items(), key=lambda kv: kv[1]["at_s"]):
            lines.append(
                f"  - `{c}` at {s['at_s']:.0f}s ({s['active_users']} users): {s['reason']}"
            )
    if v["generator_unreliable"]:
        lines.append("")
        lines.append(
            "> **WARNING: the load generator itself saturated.** "
            + "; ".join(v["generator_reasons"])
            + ". Results do not describe the server."
        )
    for n in v["notes"]:
        lines.append(f"- Note: {n}")
    lines += [
        "",
        "## Totals",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Requests (all phases) | {t['requests']} |",
        f"| Responses >= 400 (incl. benign 401/404 from the app and stale links) | {t['errors']} |",
        f"| Server-fault errors (5xx / 429 / timeouts) - the verdict keys on these | {t['server_errors']} |",
        f"| Load-phase requests | {t['load_phase_requests']} |",
        f"| Load-phase average req/s | {t['load_phase_avg_rps']} |",
        f"| User actions | {t['actions']} |",
        f"| Bytes received | {t['bytes_received'] / 1048576:.1f} MB |",
        f"| Bitstream download bytes (discarded, never stored) | {t['download_bytes'] / 1048576:.1f} MB |",
        f"| Third-party requests blocked in browsers | {t['blocked_third_party_requests']} |",
        f"| URLs skipped by good bots (robots.txt) | {t['robots_skipped_urls']} |",
        "",
        "## Per request class (load phase)",
        "",
        *_class_rows(payload["per_class_load_phase"]),
        "",
        "## Status codes",
        "",
        "| Status | Count |",
        "|---|---:|",
        *[
            f"| {k} | {n} |"
            for k, n in sorted(payload["status_counts"].items(), key=lambda kv: -kv[1])
        ],
        "",
        "## Next steps",
        "",
        "- Correlate the UTC window and the run id header with Tomcat, Node (SSR) and Solr logs.",
        "- If `search` degraded first, look at Solr (filter cache, GC, disk); if `ssr-*` did, look at Node/SSR workers; if `rest` did, at Tomcat threads and the database pool.",
        "- The extended report has the per-window time series; the raw JSON has everything for tooling.",
        "",
    ]
    return "\n".join(lines)


def _window_rows(windows: list[dict]) -> list[str]:
    lines = [
        "| # | Phase | t (s) | Users | In-flight | req/s | p50 | p95 | p99 | Err | Timeouts | Actions | Offered/s | Achieved/s | search p50 | ssr-item p50 | rest p50 | bitstream MB/s | MB | 3rd-party | Lag p95 ms | Load avg |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    t0 = windows[0]["start_ts"] if windows else 0
    for w in windows:
        bc = w["by_class"]

        def p50(c: str) -> str:
            return f"{bc[c]['p50']:.3f}" if c in bc else "-"

        bs = bc.get("bitstream")
        lines.append(
            f"| {w['index']} | {w['phase']} | {w['start_ts'] - t0:.0f} | {w['active_users']} | {w['in_flight_max']} | "
            f"{w['rps']:.1f} | {w['total']['p50']:.3f} | {w['total']['p95']:.3f} | {w['total']['p99']:.3f} | "
            f"{w['total']['errors']} | {w['total']['timeouts'] + w['action_timeouts']} | {w['actions']} | "
            f"{_fmt_s(w['offered_action_rate'])} | {w['achieved_action_rate']:.2f} | {p50('search')} | {p50('ssr-item')} | {p50('rest')} | "
            f"{_fmt_s(bs['mb_per_s']) if bs else '-'} | {w['total']['bytes_received'] / 1048576:.1f} | {w['blocked_third_party']} | "
            f"{_fmt_s(w['loop_lag_p95_ms'])} | {_fmt_s(w['load_avg_1m'])} |"
        )
    return lines


def render_extended(payload: dict) -> str:
    cfg = payload["config"]
    lines = [
        f"# Access load test extended report: {payload['target_host']}",
        "",
        f"Run `{payload['run_id']}`, {payload['started_utc']} to {payload['ended_utc']} (UTC). See the summary report for the verdict.",
        "",
        "## Configuration",
        "",
        "```json",
        json.dumps(cfg, indent=2, default=str),
        "```",
        "",
        "## Phases",
        "",
        "| Phase | Seconds |",
        "|---|---:|",
        *[f"| {k} | {v} |" for k, v in payload["phase_seconds"].items()],
        "",
        "## Baseline (unloaded reference)",
        "",
    ]
    if payload["baseline"]:
        b = payload["baseline"]
        lines += [
            f"{b['windows']} windows, {b['requests']} requests; overall p50 {b['total_p50']:.3f}s, p95 {b['total_p95']:.3f}s; "
            f"action p50 {_fmt_s(b['action_p50'])}s.",
            "",
            "| Class | p50 (s) | p95 (s) |",
            "|---|---:|---:|",
            *[f"| {c} | {b['p50'][c]:.3f} | {b['p95'][c]:.3f} |" for c in b["p50"]],
        ]
    else:
        lines.append("_No baseline phase._")
    lines += [
        "",
        "## Per request class (load phase)",
        "",
        *_class_rows(payload["per_class_load_phase"]),
        "",
        "## Time series (one row per window)",
        "",
        *_window_rows(payload["windows"]),
        "",
        "## Signals",
        "",
    ]
    if payload["signals"]:
        for s in payload["signals"]:
            lines.append(
                f"- {s['kind']} at {s['at_s']:.0f}s, class `{s['req_class']}`, {s['active_users']} users, {s['rps']:.1f} req/s: {s['reason']}"
            )
    else:
        lines.append("_None._")
    lines += ["", "## Per persona", "", "| Persona | Requests |", "|---|---:|"]
    lines += [f"| {k} | {n} |" for k, n in payload["persona_counts"].items()]
    lines += ["", "## Per class (all phases)", "", "| Class | Requests |", "|---|---:|"]
    lines += [
        f"| {k} | {n} |" for k, n in sorted(payload["class_counts"].items(), key=lambda kv: -kv[1])
    ]
    lines += [
        "",
        "## Slowest requests",
        "",
        "| Duration (s) | Class | Status | URL |",
        "|---:|---|---:|---|",
    ]
    lines += [
        f"| {r['duration_s']} | {r['class']} | {r['status']} | `{r['url'][:160]}` |"
        for r in payload["slowest_requests"][:50]
    ]
    lines += [
        "",
        "## Top errors",
        "",
        "| Count | Class | Error | URL template |",
        "|---:|---|---|---|",
    ]
    lines += [
        f"| {r['count']} | {r['class']} | {r['error'][:80]} | `{r['url_template']}` |"
        for r in payload["top_errors"]
    ] or ["| 0 | - | - | - |"]
    tp = payload["target_pool"]
    lines += [
        "",
        "## Target pool and robots.txt",
        "",
        f"- Source: {tp.get('source')}; items {tp.get('items')}, collections {tp.get('collections')}, communities {tp.get('communities')}",
        f"- Sitemaps used: {', '.join(tp.get('sitemaps') or []) or 'none'}",
        f"- robots.txt fetched: {tp.get('robots', {}).get('fetched')}; matched group: `{tp.get('robots', {}).get('matched_agent_group')}`; "
        f"crawl-delay requested: {tp.get('robots', {}).get('crawl_delay')} (ignored by design; think time {cfg['think_time_s']}s used instead)",
        f"- Disallow: {', '.join('`' + p + '`' for p in tp.get('robots', {}).get('disallow', [])) or 'none'}",
        *[f"- Note: {n}" for n in tp.get("notes", [])],
        "",
        "## Queries",
        "",
        "```json",
        json.dumps(payload["queries"], indent=2),
        "```",
        "",
        "## Generator health",
        "",
        "```json",
        json.dumps(payload["generator"], indent=2),
        "```",
        "",
        "## Persona counters",
        "",
        "```json",
        json.dumps(payload["persona_notes"], indent=2),
        "```",
        "",
    ]
    if payload.get("requests_log"):
        lines += [
            "## Per-request log",
            "",
            f"`{payload['requests_log']['path']}`: {payload['requests_log']['lines']} lines, "
            f"{payload['requests_log']['bytes'] / 1048576:.1f} MB, dropped after cap: {payload['requests_log']['dropped_after_cap']}",
            "",
        ]
    return "\n".join(lines)


def write_reports(result: RunResult, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = report_basename(result)
    payload = build_payload(result)
    paths = {
        "summary": out_dir / f"{base}-summary.md",
        "extended": out_dir / f"{base}-extended.md",
        "raw": out_dir / f"{base}-raw.json",
    }
    paths["summary"].write_text(render_summary(payload), encoding="utf-8")
    paths["extended"].write_text(render_extended(payload), encoding="utf-8")
    paths["raw"].write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return paths


__all__ = ["build_payload", "render_extended", "render_summary", "report_basename", "write_reports"]
