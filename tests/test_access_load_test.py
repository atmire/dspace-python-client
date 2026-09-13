"""Tests for examples/access_load_test (imported via path, like the other example tests)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
import respx

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))

from access_load_test.config import (
    ConfigError,
    MixConfig,
    RampConfig,
    RunConfig,
    check_host_allowed,
    estimate_resources,
    machine_resources,
    parse_duration,
    parse_mix,
    parse_ramp,
)
from access_load_test.html_links import extract_links
from access_load_test.metrics import (
    BrowserErrorRecord,
    LoopLagMonitor,
    MetricsCollector,
    RequestRecord,
    TrendDetector,
    build_baseline,
    classify_url,
    error_template,
    percentile,
)
from access_load_test.pacing import session_length, start_offsets, think_time
from access_load_test.queries import QueryGenerator
from access_load_test.robots import RobotsRules
from access_load_test.sink import TrafficBudget, stream_and_discard

# ---- config -------------------------------------------------------------------------


def _cfg(**kw) -> RunConfig:
    base = {
        "base_url": "https://repo.example.org",
        "scenario": "good-bots",
        "users": 3,
        "think_time_s": 1.0,
        "duration_s": 60.0,
        "baseline_s": 0.0,
    }
    base.update(kw)
    return RunConfig(**base)


class TestConfig:
    def test_blocklist_rejects_dspace_org(self):
        for host in ("demo.dspace.org", "sandbox.dspace.org", "dspace.org"):
            with pytest.raises(ConfigError):
                check_host_allowed(host)

    def test_blocklist_allows_other_hosts(self):
        check_host_allowed("repo.myuni.edu")
        check_host_allowed("localhost")

    def test_validate_blocks_dspace_org_base_url(self):
        with pytest.raises(ConfigError):
            _cfg(base_url="https://demo.dspace.org").validate()

    def test_rest_and_ui_base(self):
        c = _cfg(base_url="https://repo.example.org/")
        assert c.rest_base == "https://repo.example.org/server/api"
        assert c.ui_base == "https://repo.example.org"
        c2 = _cfg(ui_url="https://ui.example.org")
        assert c2.ui_base == "https://ui.example.org"
        assert "repo.example.org" in c2.allowed_hosts
        assert "ui.example.org" in c2.allowed_hosts

    def test_personas_pure_scenarios(self):
        assert _cfg(scenario="human", users=4).personas() == ["human"] * 4
        assert _cfg(scenario="good-bots", users=2).personas() == ["good-bot"] * 2
        assert _cfg(scenario="bad-bots", users=2).personas() == ["bad-bot"] * 2

    def test_mix_assignment_sums_and_rounds(self):
        mix = MixConfig(human=50, good_bot=20, bad_bot=30)
        assigned = mix.assign(10)
        assert len(assigned) == 10
        assert assigned.count("human") == 5
        assert assigned.count("good-bot") == 2
        assert assigned.count("bad-bot") == 3

    def test_mix_must_sum_to_100(self):
        with pytest.raises(ConfigError):
            MixConfig(human=50, good_bot=20, bad_bot=20).validate()

    def test_human_cap_enforced(self):
        with pytest.raises(ConfigError, match="exceeds the per-host cap"):
            _cfg(scenario="human", users=100, max_human_users=60).validate()

    def test_human_cap_override(self):
        _cfg(scenario="human", users=100, max_human_users=60, force_resources=True).validate()

    def test_needs_browser(self):
        assert _cfg(scenario="human", users=1).needs_browser
        assert not _cfg(scenario="good-bots", users=1).needs_browser
        assert _cfg(scenario="mix", users=10).needs_browser

    def test_parse_duration(self):
        assert parse_duration("90") == 90
        assert parse_duration("90s") == 90
        assert parse_duration("10m") == 600
        assert parse_duration("1.5h") == 5400
        with pytest.raises(ConfigError):
            parse_duration("-5")

    def test_parse_ramp_both_forms(self):
        r = parse_ramp("start=2,step=3,every=30s")
        assert (r.start, r.step, r.every_s) == (2, 3, 30)
        r2 = parse_ramp("2:3:30")
        assert (r2.start, r2.step, r2.every_s) == (2, 3, 30)

    def test_ramp_users_at(self):
        r = RampConfig(start=2, step=2, every_s=10)
        assert r.users_at(0, 10) == 2
        assert r.users_at(9, 10) == 2
        assert r.users_at(10, 10) == 4
        assert r.users_at(100, 10) == 10  # clamped to total

    def test_parse_mix_aliases(self):
        m = parse_mix("humans=40,good=30,bad=30")
        assert (m.human, m.good_bot, m.bad_bot) == (40, 30, 30)

    def test_estimate_resources_scales_with_humans(self):
        from access_load_test.config import MachineResources

        machine = MachineResources(cpu_count=8, ram_mb=16000)
        est = estimate_resources(_cfg(scenario="human", users=10), machine)
        assert est.human_users == 10
        assert est.browsers == 1
        assert est.ram_mb > 10 * 150
        assert est.cpu_cores > 0

    def test_estimate_over_budget(self):
        from access_load_test.config import MachineResources

        machine = MachineResources(cpu_count=2, ram_mb=2000)
        est = estimate_resources(_cfg(scenario="human", users=40, force_resources=True), machine)
        assert est.over_budget

    def test_machine_resources_returns_something(self):
        m = machine_resources()
        assert m.cpu_count is None or m.cpu_count >= 1


# ---- classification ------------------------------------------------------------------


class TestClassify:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://x/server/api/discover/search/objects?query=a", "search"),
            ("https://x/server/api/discover/browses/author/items", "browse"),
            ("https://x/server/api/statistics/viewevents", "event"),
            ("https://x/server/api/core/bitstreams/uuid/content", "bitstream"),
            ("https://x/server/api/core/items/uuid", "rest"),
            ("https://x/items/abc-123", "ssr-item"),
            ("https://x/search?query=z", "ssr-search"),
            ("https://x/browse/title", "ssr-browse"),
            ("https://x/assets/main.js", "static"),
            ("https://x/server/oai/request?verb=ListRecords", "oai"),
            ("https://x/robots.txt", "crawl-meta"),
            ("https://x/sitemap_index.xml", "crawl-meta"),
        ],
    )
    def test_classify(self, url, expected):
        assert classify_url(url) == expected

    def test_thumbnail_vs_bitstream_by_resource_type(self):
        url = "https://x/server/api/core/bitstreams/uuid/content"
        assert classify_url(url, resource_type="image") == "thumbnail"
        assert classify_url(url) == "bitstream"

    def test_hint_wins(self):
        assert classify_url("https://x/anything", hint="bitstream") == "bitstream"


def test_percentile():
    assert percentile([], 0.5) == 0.0
    vals = list(range(1, 101))
    assert percentile(vals, 0.5) == 50
    assert percentile(vals, 0.95) == 95
    assert percentile(vals, 1.0) == 100


# ---- metrics windows & trend ---------------------------------------------------------


def _rec(cls="rest", status=200, dur=0.1, error=None, phase="load", ts=None, persona="good-bot"):
    import time as _t

    return RequestRecord(
        ts_end=ts if ts is not None else _t.time(),
        user_id="u1",
        persona=persona,
        phase=phase,
        method="GET",
        url=f"https://x/server/api/{cls}",
        req_class=cls,
        status=status,
        duration_s=dur,
        ttfb_s=dur / 2,
        bytes_received=100,
        error=error,
    )


class TestMetrics:
    def test_windows_roll_and_summarise(self):
        mc = MetricsCollector(window_s=1.0)
        mc.set_phase("load")
        t0 = mc.t0
        for i in range(5):
            mc.record(_rec(dur=0.2, ts=t0 + 0.1 + i * 0.1))
        closed = mc.roll(now=t0 + 2.0, final=False)
        assert len(closed) >= 1
        w = closed[0]
        assert w.total.count == 5
        assert w.total.p50 == pytest.approx(0.2)
        assert w.total.errors == 0

    def test_error_and_timeout_counting(self):
        mc = MetricsCollector(window_s=1.0)
        mc.set_phase("load")
        t0 = mc.t0
        mc.record(_rec(status=503, error=None, ts=t0 + 0.1))
        mc.record(_rec(status=None, error="timeout: ReadTimeout", ts=t0 + 0.2))
        w = mc.roll(now=t0 + 2.0)[0]
        assert w.total.errors == 2
        assert w.total.timeouts == 1

    def test_records_land_in_completion_window(self):
        mc = MetricsCollector(window_s=1.0)
        mc.set_phase("load")
        t0 = mc.t0
        mc.record(_rec(ts=t0 + 0.5))
        mc.record(_rec(ts=t0 + 1.5))
        mc.record(_rec(ts=t0 + 2.5))
        closed = mc.roll(now=t0 + 3.0)
        assert [w.total.count for w in closed] == [1, 1, 1]

    def test_action_burst_counting(self):
        from access_load_test.context import ActionScope, RunContext

        mc = MetricsCollector(window_s=10.0)
        mc.set_phase("load")
        ctx = RunContext(
            cfg=_cfg(),
            collector=mc,
            queries=QueryGenerator(seed=1),
            budget=TrafficBudget(None),
            inflight=asyncio.Semaphore(10),
            stop=asyncio.Event(),
        )
        scope = ActionScope(ctx, "u1", "human", "landing", "https://x/")
        base = 1000.0
        for _ in range(6):
            scope.note_request(base + 0.1)  # same second
        scope.note_request(base + 1.5)
        assert scope.burst_max() == 6

    def test_trend_detects_onset(self):
        mc = MetricsCollector(window_s=1.0)
        det = TrendDetector(factor=1.5, min_increase_s=0.01, confirm_windows=2)
        # baseline windows: fast
        for wi in range(2):
            for i in range(10):
                mc.record(_rec(dur=0.1, phase="baseline", ts=mc.t0 + wi + 0.05 + i * 0.01))
        mc.set_phase("baseline")
        mc.roll(now=mc.t0 + 3.0)
        det.set_baseline(build_baseline(mc.windows))
        assert det.baseline is not None
        # loaded windows: slow (0.5s p50)
        signals = []
        for wi in range(3, 8):
            for i in range(10):
                mc.record(_rec(dur=0.5, phase="load", ts=mc.t0 + wi + 0.05 + i * 0.01))
        for w in mc.roll(now=mc.t0 + 9.0):
            signals.extend(det.observe(w))
        assert det.verdict.onset is not None
        assert det.verdict.status in ("degraded", "breaking")

    def test_trend_detects_breaking_on_errors(self):
        mc = MetricsCollector(window_s=1.0)
        det = TrendDetector(break_error_rate=0.05, break_confirm_windows=1)
        det.set_baseline(None)
        for wi in range(3):
            for i in range(20):
                err = "boom" if i < 5 else None
                st = 503 if i < 5 else 200
                mc.record(_rec(status=st, error=err, phase="load", ts=mc.t0 + wi + 0.05 + i * 0.01))
        mc.set_phase("load")
        for w in mc.roll(now=mc.t0 + 5.0):
            det.observe(w)
        assert det.verdict.breaking is not None
        assert det.verdict.status == "breaking"

    def test_healthy_run_stays_healthy(self):
        mc = MetricsCollector(window_s=1.0)
        det = TrendDetector()
        for wi in range(2):
            for i in range(10):
                mc.record(_rec(dur=0.1, phase="baseline", ts=mc.t0 + wi + 0.05 + i * 0.01))
        mc.set_phase("baseline")
        mc.roll(now=mc.t0 + 3.0)
        det.set_baseline(build_baseline(mc.windows))
        for wi in range(3, 8):
            for i in range(10):
                mc.record(_rec(dur=0.1, phase="load", ts=mc.t0 + wi + 0.05 + i * 0.01))
        for w in mc.roll(now=mc.t0 + 9.0):
            det.observe(w)
        assert det.verdict.status == "healthy"

    def test_server_error_classification(self):
        assert not _rec(status=401).is_server_error
        assert not _rec(status=403).is_server_error
        assert not _rec(status=404).is_server_error
        assert not _rec(status=400).is_server_error
        assert _rec(status=500).is_server_error
        assert _rec(status=503).is_server_error
        # 429 is rate-limiting, a capacity signal, NOT a server fault:
        assert not _rec(status=429).is_server_error
        assert _rec(status=429).is_rate_limited
        assert _rec(status=None, error="timeout: ReadTimeout").is_server_error

    def test_browser_abort_is_not_an_error(self):
        rec = RequestRecord(
            ts_end=0.0,
            user_id="u",
            persona="human",
            phase="load",
            method="GET",
            url="https://x/assets/a.js",
            req_class="static",
            status=None,
            duration_s=0.1,
            ttfb_s=None,
            bytes_received=0,
            error="net::ERR_ABORTED",
            source="browser",
            aborted=True,
        )
        assert rec.ok  # a cancelled request is not a failure
        assert not rec.is_server_error

    def test_aborted_excluded_from_error_counts(self):
        mc = MetricsCollector(window_s=1.0)
        mc.set_phase("load")
        t0 = mc.t0
        for i in range(30):
            mc.record(
                RequestRecord(
                    ts_end=t0 + 0.1 + i * 0.01,
                    user_id="u",
                    persona="human",
                    phase="load",
                    method="GET",
                    url="https://x/assets/a.js",
                    req_class="static",
                    status=None,
                    duration_s=0.05,
                    ttfb_s=None,
                    bytes_received=0,
                    error="net::ERR_ABORTED",
                    source="browser",
                    aborted=True,
                )
            )
        mc.record(_rec(status=200, ts=t0 + 0.5))
        w = mc.roll(now=t0 + 2.0)[0]
        assert w.total.errors == 0
        assert w.total.server_errors == 0
        assert mc.status_counts.get("aborted(browser)") == 30

    def test_window_separates_4xx_from_server_errors(self):
        mc = MetricsCollector(window_s=1.0)
        mc.set_phase("load")
        t0 = mc.t0
        for i in range(5):
            mc.record(_rec(status=401, ts=t0 + 0.1 + i * 0.01))
        mc.record(_rec(status=503, ts=t0 + 0.2))
        w = mc.roll(now=t0 + 2.0)[0]
        assert w.total.errors == 6  # all >= 400
        assert w.total.server_errors == 1  # only the 503

    def test_rate_limiting_is_not_breaking(self):
        mc = MetricsCollector(window_s=1.0)
        det = TrendDetector(
            break_error_rate=0.05,
            break_confirm_windows=1,
            break_p95_s=100,
            min_requests_for_error_rate=20,
            rate_limit_confirm_windows=2,
        )
        det.set_baseline(None)
        for wi in range(3):
            for i in range(40):
                st = 429 if i < 15 else 200  # ~37% rate-limited
                mc.record(_rec(status=st, dur=0.02, phase="load", ts=mc.t0 + wi + 0.02 + i * 0.01))
        mc.set_phase("load")
        for w in mc.roll(now=mc.t0 + 5.0):
            det.observe(w)
        assert det.verdict.breaking is None
        assert det.verdict.status == "healthy"
        assert det.verdict.rate_limited is not None  # reported as a capacity signal

    def test_breaking_needs_a_real_sample(self):
        # A quiet window with only a few 5xx must not trip breaking.
        mc = MetricsCollector(window_s=1.0)
        det = TrendDetector(
            break_error_rate=0.05,
            break_confirm_windows=1,
            break_p95_s=100,
            min_requests_for_error_rate=20,
        )
        det.set_baseline(None)
        for wi in range(3):
            # 15 requests, 4 are 5xx (27%) — below the 20-request sample floor
            for i in range(15):
                st = 503 if i < 4 else 200
                mc.record(_rec(status=st, dur=0.02, phase="load", ts=mc.t0 + wi + 0.02 + i * 0.01))
        mc.set_phase("load")
        for w in mc.roll(now=mc.t0 + 5.0):
            det.observe(w)
        assert det.verdict.breaking is None

    def test_single_timeout_does_not_break(self):
        # One transient request timeout in a busy window must not hard-stop the run.
        mc = MetricsCollector(window_s=1.0)
        det = TrendDetector(
            break_error_rate=0.05,
            break_confirm_windows=1,
            break_p95_s=100,
            min_requests_for_error_rate=20,
        )
        det.set_baseline(None)
        for wi in range(3):
            for i in range(50):
                if i == 0:
                    mc.record(
                        _rec(
                            status=None,
                            error="timeout: ReadTimeout",
                            dur=0.1,
                            phase="load",
                            ts=mc.t0 + wi + 0.02 + i * 0.01,
                        )
                    )
                else:
                    mc.record(
                        _rec(status=200, dur=0.02, phase="load", ts=mc.t0 + wi + 0.02 + i * 0.01)
                    )
        mc.set_phase("load")
        for w in mc.roll(now=mc.t0 + 5.0):
            det.observe(w)
        assert det.verdict.breaking is None  # 1 timeout / 50 req = 2%, under the guard

    def test_timeout_storm_breaks_via_p95(self):
        # Many hung requests spike p95 and DO break.
        mc = MetricsCollector(window_s=1.0)
        det = TrendDetector(break_confirm_windows=1, break_p95_s=10, min_requests_for_error_rate=20)
        det.set_baseline(None)
        for wi in range(2):
            for i in range(30):
                mc.record(
                    _rec(
                        status=None,
                        error="timeout: ReadTimeout",
                        dur=60.0,
                        phase="load",
                        ts=mc.t0 + wi + 0.02 + i * 0.01,
                    )
                )
        mc.set_phase("load")
        for w in mc.roll(now=mc.t0 + 4.0):
            det.observe(w)
        assert det.verdict.breaking is not None

    def test_breaking_ignores_benign_4xx_flood(self):
        mc = MetricsCollector(window_s=1.0)
        det = TrendDetector(break_error_rate=0.05, break_confirm_windows=1, break_p95_s=100)
        det.set_baseline(None)
        # Every request is a 404 (stale links); none is a server fault.
        for wi in range(3):
            for i in range(20):
                mc.record(_rec(status=404, dur=0.05, phase="load", ts=mc.t0 + wi + 0.05 + i * 0.01))
        mc.set_phase("load")
        for w in mc.roll(now=mc.t0 + 5.0):
            det.observe(w)
        assert det.verdict.breaking is None
        assert det.verdict.status == "healthy"

    def test_slowest_and_top_errors(self):
        mc = MetricsCollector(window_s=1.0, top_slow=3)
        for d in (0.1, 5.0, 0.3, 9.0, 2.0):
            mc.record(_rec(dur=d))
        mc.record(_rec(status=500, error="HTTP 500"))
        slow = mc.slowest()
        assert slow[0]["duration_s"] == 9.0
        assert len(slow) == 3
        errs = mc.top_errors()
        assert errs and errs[0]["count"] >= 1


class TestLoopLag:
    @pytest.mark.asyncio
    async def test_loop_lag_runs(self):
        mon = LoopLagMonitor(interval_s=0.05)
        mon.start()
        await asyncio.sleep(0.2)
        p95, mx = mon.take_window()
        await mon.stop()
        assert p95 is not None
        assert mx is not None


# ---- queries -------------------------------------------------------------------------


class TestQueries:
    def test_uniqueness_within_run(self):
        q = QueryGenerator(seed=42)
        seen = {q.next_query() for _ in range(300)}
        assert len(seen) == 300

    def test_different_seed_diverges(self):
        a = [QueryGenerator(seed=1).next_query() for _ in range(20)]
        b = [QueryGenerator(seed=2).next_query() for _ in range(20)]
        assert a != b

    def test_harvest_text_grows_vocab(self):
        q = QueryGenerator(seed=1)
        before = q.vocabulary_size
        q.add_text("Bayesian inference for marine sediment transport dynamics")
        assert q.vocabulary_size > before

    def test_search_params_have_query(self):
        q = QueryGenerator(seed=3)
        for _ in range(20):
            p = q.next_search_params()
            assert p.get("query")

    def test_browse_returns_known_type(self):
        q = QueryGenerator(seed=3)
        kind, params = q.next_browse()
        assert kind in ("author", "title", "dateissued", "subject")
        assert isinstance(params, dict)


# ---- robots --------------------------------------------------------------------------


ROBOTS = """
User-agent: *
Disallow: /search
Disallow: /admin/*
Disallow: /entities/*?f
Crawl-delay: 10
Sitemap: https://x/sitemap_index.xml

User-agent: BadBot
Disallow: /
"""


class TestRobots:
    def test_parse_and_allow(self):
        r = RobotsRules.parse(ROBOTS, "generic-crawler")
        assert r.allowed("https://x/items/123")
        assert not r.allowed("https://x/search")
        assert not r.allowed("https://x/search?query=z")
        assert not r.allowed("https://x/admin/users")
        assert r.crawl_delay == 10
        assert "https://x/sitemap_index.xml" in r.sitemaps

    def test_wildcard_query_pattern(self):
        r = RobotsRules.parse(ROBOTS, "generic")
        assert not r.allowed("https://x/entities/abc?f=author")
        assert r.allowed("https://x/entities/abc")

    def test_specific_agent_group_wins(self):
        r = RobotsRules.parse(ROBOTS, "BadBot")
        assert not r.allowed("https://x/items/123")
        assert r.matched_agent == "badbot"

    def test_empty_disallow_allows_all(self):
        r = RobotsRules.parse("User-agent: *\nDisallow:\n", "x")
        assert r.allowed("https://x/anything")


# ---- html links ----------------------------------------------------------------------


HTML = """
<html><head><title>Item</title><script src="/assets/main.js"></script></head>
<body>
<a href="/items/abc">Item</a>
<a href="/collections/col1">Coll</a>
<a href="https://external.example.com/x">External</a>
<a href="/bitstreams/bs1/download">PDF</a>
<a href="/server/api/core/bitstreams/bs2/content">Content</a>
<a href="mailto:x@y.z">mail</a>
<a href="#frag">frag</a>
</body></html>
"""


class TestHtmlLinks:
    def test_extracts_same_site_and_downloads(self):
        pl = extract_links(HTML, "https://x/page", {"x"})
        assert "https://x/items/abc" in pl.links
        assert "https://x/collections/col1" in pl.links
        assert any("external" in u for u in pl.links) is False
        assert "https://x/bitstreams/bs1/download" in pl.downloads
        assert "https://x/server/api/core/bitstreams/bs2/content" in pl.downloads
        assert pl.title == "Item"

    def test_malformed_html_safe(self):
        pl = extract_links("<a href=/items/1>x<//bad", "https://x/", {"x"})
        assert isinstance(pl.links, list)


# ---- pacing --------------------------------------------------------------------------


class TestPacing:
    def test_think_time_zero(self):
        import random

        assert think_time(0, random.Random(1)) == 0.0

    def test_think_time_fixed(self):
        import random

        assert think_time(2.0, random.Random(1), fixed=True) == 2.0

    def test_think_time_bounded(self):
        import random

        rng = random.Random(1)
        for _ in range(1000):
            assert 0 <= think_time(1.0, rng) <= 4.0

    def test_start_offsets_sorted_within_spread(self):
        import random

        offs = start_offsets(10, 5.0, random.Random(1))
        assert offs == sorted(offs)
        assert all(0 <= o <= 5.0 for o in offs)
        assert start_offsets(3, 0, random.Random(1)) == [0, 0, 0]

    def test_session_length_bounds(self):
        import random

        rng = random.Random(1)
        for _ in range(200):
            n = session_length(4.0, 12, rng)
            assert 1 <= n <= 12


# ---- sink ----------------------------------------------------------------------------


class TestSink:
    @pytest.mark.asyncio
    @respx.mock
    async def test_stream_counts_bytes_and_discards(self):
        payload = b"a" * 10000
        respx.get("https://x/file").mock(return_value=httpx.Response(200, content=payload))
        async with httpx.AsyncClient() as client:
            res = await stream_and_discard(client, "https://x/file")
        assert res.ok
        assert res.bytes_received == 10000
        assert res.ttfb_s is not None

    @pytest.mark.asyncio
    @respx.mock
    async def test_per_download_cap_truncates(self):
        respx.get("https://x/big").mock(return_value=httpx.Response(200, content=b"b" * 1_000_000))
        async with httpx.AsyncClient() as client:
            res = await stream_and_discard(client, "https://x/big", max_bytes=50_000)
        assert res.truncated
        assert res.bytes_received >= 50_000

    @pytest.mark.asyncio
    @respx.mock
    async def test_budget_exhaustion_refuses(self):
        budget = TrafficBudget(max_bytes=100)
        respx.get("https://x/f").mock(return_value=httpx.Response(200, content=b"x" * 500))
        async with httpx.AsyncClient() as client:
            r1 = await stream_and_discard(client, "https://x/f", budget=budget)
            r2 = await stream_and_discard(client, "https://x/f", budget=budget)
        assert r1.bytes_received > 0
        assert r2.error == "traffic_budget_exhausted"
        assert budget.refused == 1


class TestTrafficBudget:
    def test_unlimited(self):
        b = TrafficBudget(None)
        assert not b.exhausted
        b.note(10**9)
        assert not b.exhausted
        assert b.remaining() is None

    def test_limited(self):
        b = TrafficBudget(100)
        b.note(60)
        assert b.remaining() == 40
        b.note(60)
        assert b.exhausted


class TestBrowserErrors:
    def _be(self, kind="pageerror", text="TypeError: x is undefined"):
        return BrowserErrorRecord(
            ts=1000.0,
            user_id="human-001",
            persona="human",
            phase="load",
            kind=kind,
            text=text,
            url="https://x/items/abc",
            action_id="a1",
        )

    def test_error_template_collapses_ids_and_numbers(self):
        a = error_template(
            "Cannot read property of 11111111-1111-1111-1111-111111111111 at line 42"
        )
        b = error_template(
            "Cannot read property of 22222222-2222-2222-2222-222222222222 at line 99"
        )
        assert a == b
        assert "{uuid}" in a and "{n}" in a

    def test_collector_aggregates_browser_errors(self):
        mc = MetricsCollector(window_s=1.0)
        for _ in range(3):
            mc.record_browser_error(self._be(text="TypeError: a at 1"))
        mc.record_browser_error(self._be(kind="console.error", text="app blew up"))
        assert mc.total_browser_errors == 4
        top = mc.top_browser_errors()
        # the 3 TypeErrors collapse to one template with count 3
        counts = {(r["kind"], r["count"]) for r in top}
        assert ("pageerror", 3) in counts
        assert ("console.error", 1) in counts

    def test_sample_reservoir_is_bounded(self):
        mc = MetricsCollector(window_s=1.0)
        mc._browser_error_sample_cap = 5
        for i in range(20):
            mc.record_browser_error(self._be(text=f"err {i}"))
        assert len(mc.browser_error_samples()) == 5
        assert mc.total_browser_errors == 20

    def test_should_record_console_filters(self):
        from access_load_test.persona_human import should_record_console

        assert should_record_console("error", "Uncaught TypeError: boom")
        assert not should_record_console("warning", "something")
        assert not should_record_console("log", "info")
        # network failures are already captured as request records:
        assert not should_record_console(
            "error", "Failed to load resource: the server responded with 500"
        )
        assert not should_record_console("error", "   ")
