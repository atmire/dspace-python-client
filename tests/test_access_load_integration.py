"""
Integration-ish tests for access_load_test: bots and the orchestrator driven against a
respx-mocked DSpace, plus the human persona's pure decision logic against a fake page.

No real network and no real browser here. The live smoke tests live outside the suite.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import httpx
import pytest
import respx
from rich.console import Console

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))

from access_load_test.config import RunConfig
from access_load_test.orchestrator import run_load_test

UI = "https://repo.test"
API = "https://repo.test/server/api"

ROBOTS = """User-agent: *
Disallow: /search
Disallow: /admin
"""

SITEMAP_INDEX = f"""<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>{UI}/sitemap_0.xml</loc></sitemap>
</sitemapindex>"""

SITEMAP_0 = f"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>{UI}/items/11111111-1111-1111-1111-111111111111</loc></url>
<url><loc>{UI}/items/22222222-2222-2222-2222-222222222222</loc></url>
<url><loc>{UI}/collections/33333333-3333-3333-3333-333333333333</loc></url>
</urlset>"""

ITEM_HTML = f"""<html><head><title>Item</title></head><body>
<a href="{UI}/items/22222222-2222-2222-2222-222222222222">related</a>
<a href="{UI}/browse/author?value=Smith">author</a>
<a href="{UI}/bitstreams/aaaaaaaa-1111-1111-1111-111111111111/download">PDF</a>
<a href="{UI}/search?query=x">search</a>
</body></html>"""


def _mock_server(router: respx.Router) -> None:
    router.get(f"{UI}/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
    router.get(f"{UI}/sitemap_index.xml").mock(return_value=httpx.Response(200, text=SITEMAP_INDEX))
    router.get(f"{UI}/sitemap_0.xml").mock(return_value=httpx.Response(200, text=SITEMAP_0))
    router.get(url__regex=rf"{UI}/server/sitemaps/.*").mock(return_value=httpx.Response(404))
    router.get(url__regex=rf"{API}/discover/search/objects.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "_embedded": {
                    "searchResult": {
                        "_embedded": {
                            "objects": [
                                {
                                    "_embedded": {
                                        "indexableObject": {
                                            "uuid": "44444444-4444-4444-4444-444444444444",
                                            "type": "item",
                                            "metadata": {
                                                "dc.title": [
                                                    {"value": "Quantum sediment transport models"}
                                                ]
                                            },
                                        }
                                    }
                                }
                            ]
                        },
                        "page": {"number": 0, "size": 100, "totalElements": 1, "totalPages": 1},
                    }
                }
            },
        )
    )
    router.get(url__regex=rf"{API}/core/communities/search/top.*").mock(
        return_value=httpx.Response(200, json={"_embedded": {"communities": []}})
    )
    router.get(url__regex=rf"{UI}/items/.*").mock(return_value=httpx.Response(200, text=ITEM_HTML))
    router.get(url__regex=rf"{UI}/collections/.*").mock(
        return_value=httpx.Response(200, text=ITEM_HTML)
    )
    router.get(url__regex=rf"{UI}/browse.*").mock(return_value=httpx.Response(200, text=ITEM_HTML))
    router.get(url__regex=rf"{UI}/community-list.*").mock(
        return_value=httpx.Response(200, text=ITEM_HTML)
    )
    router.get(f"{UI}/").mock(return_value=httpx.Response(200, text=ITEM_HTML))
    router.get(url__regex=rf"{UI}/search.*").mock(return_value=httpx.Response(200, text=ITEM_HTML))
    router.get(url__regex=rf"{UI}/bitstreams/.*/download").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.4 fake" + b"0" * 5000)
    )
    router.get(url__regex=rf"{API}/core/bitstreams/.*/content").mock(
        return_value=httpx.Response(200, content=b"%PDF" + b"0" * 5000)
    )
    router.get(url__regex=rf"{UI}/server/oai/.*").mock(
        return_value=httpx.Response(200, text="<OAI-PMH><ListRecords></ListRecords></OAI-PMH>")
    )


def _cfg(**kw) -> RunConfig:
    base = {
        "base_url": UI,
        "scenario": "good-bots",
        "users": 2,
        "think_time_s": 0.0,
        "duration_s": 2.0,
        "baseline_s": 0.0,
        "window_s": 0.5,
        "downloads_enabled": True,
        "download_probability": 1.0,
        "skip_version_check": True,
        "requests_log": False,
        "auto_stop": False,
        "max_total_download_gb": 0.01,
    }
    base.update(kw)
    return RunConfig(**base)


@pytest.fixture
def console() -> Console:
    return Console(quiet=True, file=io.StringIO())


class TestPoolDiscovery:
    @pytest.mark.asyncio
    @respx.mock
    async def test_discovers_items_from_sitemap(self, console):
        _mock_server(respx.mock)
        result = await run_load_test(_cfg(dry_run=True), console, live=False)
        assert result.dry_run
        assert result.pool_summary["items"] == 2
        assert result.pool_summary["source"] == "sitemap"
        assert result.pool_summary["robots"]["fetched"] is True

    @pytest.mark.asyncio
    @respx.mock
    async def test_falls_back_to_rest_without_sitemap(self, console):
        router = respx.mock
        router.get(f"{UI}/robots.txt").mock(return_value=httpx.Response(404))
        router.get(url__regex=rf"{UI}/sitemap.*").mock(
            return_value=httpx.Response(
                200,
                text='<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></sitemapindex>',
            )
        )
        router.get(url__regex=rf"{UI}/server/sitemaps/.*").mock(return_value=httpx.Response(404))
        _rest_only(router)
        result = await run_load_test(_cfg(dry_run=True), console, live=False)
        assert result.pool_summary["items"] == 1
        assert result.pool_summary["source"] == "rest-discovery"


def _rest_only(router: respx.Router) -> None:
    router.get(url__regex=rf"{API}/discover/search/objects.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "_embedded": {
                    "searchResult": {
                        "_embedded": {
                            "objects": [
                                {
                                    "_embedded": {
                                        "indexableObject": {
                                            "uuid": "55555555-5555-5555-5555-555555555555",
                                            "type": "item",
                                            "metadata": {},
                                        }
                                    }
                                }
                            ]
                        },
                        "page": {"number": 0, "size": 100, "totalElements": 1, "totalPages": 1},
                    }
                }
            },
        )
    )
    router.get(url__regex=rf"{API}/core/communities/search/top.*").mock(
        return_value=httpx.Response(200, json={"_embedded": {"communities": []}})
    )


class TestGoodBotRun:
    @pytest.mark.asyncio
    @respx.mock
    async def test_good_bot_obeys_robots_and_reports(self, console):
        _mock_server(respx.mock)
        result = await run_load_test(
            _cfg(scenario="good-bots", users=2, duration_s=1.5), console, live=False
        )
        assert result.totals["requests"] > 0
        assert result.verdict.status in ("healthy", "degraded", "breaking")
        # /search (the SSR search page) is disallowed; a good bot must never fetch it.
        # The REST discovery endpoint (…/discover/search/objects) is a different path and
        # is only used in the setup phase, so ssr-search must be absent from load traffic.
        assert result.class_counts.get("ssr-search", 0) == 0
        assert result.stop_reason

    @pytest.mark.asyncio
    @respx.mock
    async def test_reports_have_expected_shape(self, console):
        from access_load_test.report import build_payload, render_extended, render_summary

        _mock_server(respx.mock)
        result = await run_load_test(_cfg(duration_s=1.0), console, live=False)
        payload = build_payload(result)
        assert payload["schema_version"]
        assert "verdict" in payload and "per_class_load_phase" in payload
        assert "windows" in payload
        assert isinstance(render_summary(payload), str)
        assert isinstance(render_extended(payload), str)
        assert payload["run_id"] == result.cfg.run_id


class TestBadBotRun:
    @pytest.mark.asyncio
    @respx.mock
    async def test_bad_bot_closed_loop(self, console):
        _mock_server(respx.mock)
        result = await run_load_test(
            _cfg(scenario="bad-bots", users=2, duration_s=1.5), console, live=False
        )
        assert result.totals["requests"] > 0

    @pytest.mark.asyncio
    @respx.mock
    async def test_bad_bot_open_loop(self, console):
        _mock_server(respx.mock)
        result = await run_load_test(
            _cfg(
                scenario="bad-bots",
                users=2,
                duration_s=1.5,
                bad_bot_open_loop=True,
                think_time_s=0.05,
            ),
            console,
            live=False,
        )
        assert result.totals["requests"] > 0


class TestBaselinePhase:
    @pytest.mark.asyncio
    @respx.mock
    async def test_baseline_builds_reference(self, console):
        _mock_server(respx.mock)
        result = await run_load_test(
            _cfg(scenario="good-bots", users=2, duration_s=1.5, baseline_s=1.0), console, live=False
        )
        assert result.baseline is not None
        assert result.phase_seconds.get("baseline", 0) > 0


# ---- human decision logic (no browser) ----------------------------------------------


class TestHumanLogic:
    def test_categorise_hrefs(self):
        from access_load_test.persona_human import HumanUser

        hrefs = [
            "/items/abc",
            "/collections/c1",
            "/communities/x1",
            "/browse/author",
            "/search?query=z",
            "/bitstreams/b1/download",
            "/search?query=y&page=2",
            "/",
        ]
        cats = HumanUser._categorise(hrefs)
        assert "/items/abc" in cats["item"]
        assert "/collections/c1" in cats["container"]
        assert "/communities/x1" in cats["container"]
        assert "/browse/author" in cats["browse"]
        assert "/bitstreams/b1/download" in cats["download"]
        assert any("page=2" in h for h in cats["pagination"])
        assert "/" in cats["home"]

    def test_playwright_available_flag(self):
        from access_load_test.persona_human import playwright_available

        assert isinstance(playwright_available(), bool)
