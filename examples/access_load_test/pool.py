"""
Target discovery: what URLs exist to be visited.

Order of preference: the UI sitemap index (what real crawlers use), then the REST
discovery endpoint. A few discovery pages are always fetched to harvest vocabulary for the
query generator. Everything here is recorded under the ``setup`` phase, so it never mixes
with load measurements.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from defusedxml import ElementTree as SafeET

from access_load_test.config import RunConfig
from access_load_test.http_user import HttpUser
from access_load_test.queries import QueryGenerator
from access_load_test.robots import RobotsRules

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
ZIPF_EXPONENT = 0.8


@dataclass(slots=True)
class TargetItem:
    url: str
    uuid: str | None = None
    handle: str | None = None


@dataclass
class TargetPool:
    ui_base: str
    rest_base: str
    items: list[TargetItem] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)
    communities: list[str] = field(default_factory=list)
    robots: RobotsRules = field(default_factory=RobotsRules)
    sitemap_urls: list[str] = field(default_factory=list)
    source: str = "none"
    notes: list[str] = field(default_factory=list)
    _cum_weights: list[float] = field(default_factory=list)
    _rng: random.Random = field(default_factory=random.Random)

    # -- sampling ----------------------------------------------------------------

    def finalise(self, rng: random.Random) -> None:
        """Shuffle once, then give earlier items Zipf-like popularity."""
        self._rng = rng
        rng.shuffle(self.items)
        weights = [1.0 / (i + 1) ** ZIPF_EXPONENT for i in range(len(self.items))]
        total = 0.0
        self._cum_weights = []
        for w in weights:
            total += w
            self._cum_weights.append(total)

    def pick_item(self, rng: random.Random | None = None) -> TargetItem | None:
        if not self.items:
            return None
        r = rng or self._rng
        if not self._cum_weights:
            return r.choice(self.items)
        return r.choices(self.items, cum_weights=self._cum_weights, k=1)[0]

    def pick_container(self, rng: random.Random | None = None) -> str | None:
        r = rng or self._rng
        pool = self.collections + self.communities
        return r.choice(pool) if pool else None

    def seeds(self) -> list[str]:
        """Entry points every crawler knows about."""
        return [self.ui_base + "/", f"{self.ui_base}/community-list"]

    def summary(self) -> dict:
        return {
            "source": self.source,
            "items": len(self.items),
            "collections": len(self.collections),
            "communities": len(self.communities),
            "sitemaps": list(self.sitemap_urls),
            "robots": self.robots.to_dict(),
            "notes": list(self.notes),
        }


# ---- discovery ---------------------------------------------------------------------


async def fetch_robots(user: HttpUser, ui_base: str, user_agent: str) -> RobotsRules:
    res = await user.get(f"{ui_base}/robots.txt", req_class="crawl-meta", accept="text/plain,*/*")
    if res.ok and res.text is not None:
        rules = RobotsRules.parse(res.text, user_agent)
        rules.fetched = True
        rules.status = res.status
        return rules
    rules = RobotsRules()
    rules.status = res.status
    return rules


def _classify_sitemap_url(url: str, pool: TargetPool) -> None:
    path = urlparse(url).path
    low = path.lower()
    m = _UUID_RE.search(path)
    uid = m.group(0).lower() if m else None
    if low.startswith(("/items/", "/entities/")):
        pool.items.append(TargetItem(url=url, uuid=uid))
    elif low.startswith("/handle/"):
        pool.items.append(TargetItem(url=url, handle=path[len("/handle/") :]))
    elif low.startswith("/collections/"):
        pool.collections.append(url)
    elif low.startswith("/communities/"):
        pool.communities.append(url)


def _parse_locs(xml_text: str) -> list[str]:
    try:
        root = SafeET.fromstring(xml_text)
    except Exception:
        return []
    locs: list[str] = []
    for el in root.iter():
        if el.tag.endswith("loc") and el.text:
            locs.append(el.text.strip())
    return locs


async def _discover_from_sitemaps(user: HttpUser, cfg: RunConfig, pool: TargetPool) -> bool:
    candidates = list(pool.robots.sitemaps) or []
    candidates += [
        f"{cfg.ui_base}/sitemap_index.xml",
        f"{cfg.base_url.rstrip('/')}/server/sitemaps/sitemap_index.xml",
    ]
    seen: set[str] = set()
    for index_url in candidates:
        if index_url in seen:
            continue
        seen.add(index_url)
        res = await user.get(
            index_url, req_class="crawl-meta", accept="application/xml,text/xml,*/*"
        )
        if not res.ok or not res.text:
            continue
        locs = _parse_locs(res.text)
        if not locs:
            continue
        pool.sitemap_urls.append(index_url)
        child_maps = [u for u in locs if "sitemap" in urlparse(u).path.lower()]
        page_urls = [u for u in locs if u not in child_maps]
        for u in page_urls:
            _classify_sitemap_url(u, pool)
        for child in child_maps[:50]:
            if len(pool.items) >= cfg.pool_max_items:
                break
            r = await user.get(child, req_class="crawl-meta", accept="application/xml,text/xml,*/*")
            if r.ok and r.text:
                pool.sitemap_urls.append(child)
                for u in _parse_locs(r.text):
                    if len(pool.items) >= cfg.pool_max_items:
                        break
                    _classify_sitemap_url(u, pool)
        if pool.items:
            pool.source = "sitemap"
            return True
    return False


def _harvest_vocab(item: dict, queries: QueryGenerator) -> None:
    md = item.get("metadata") or {}
    for key in ("dc.title", "dc.contributor.author", "dc.subject", "dc.description.abstract"):
        for entry in md.get(key) or []:
            value = entry.get("value") if isinstance(entry, dict) else None
            if value:
                queries.add_text(value)


async def _discover_from_rest(
    user: HttpUser, cfg: RunConfig, pool: TargetPool, queries: QueryGenerator, *, pages: int
) -> int:
    """Walk discovery pages; returns how many items were added. Always harvests vocabulary."""
    added = 0
    for page in range(pages):
        res = await user.get(
            f"{cfg.rest_base}/discover/search/objects",
            req_class="search",
            accept="application/json",
            params={
                "dsoType": "item",
                "size": "100",
                "page": str(page),
                "sort": "dc.date.accessioned,desc",
            },
        )
        if not res.ok or not res.text:
            break
        try:
            import json

            data = json.loads(res.text)
        except ValueError:
            break
        objects = (
            data.get("_embedded", {})
            .get("searchResult", {})
            .get("_embedded", {})
            .get("objects", [])
        )
        if not objects:
            break
        for obj in objects:
            item = obj.get("_embedded", {}).get("indexableObject", {})
            uid = item.get("uuid")
            _harvest_vocab(item, queries)
            if uid and len(pool.items) < cfg.pool_max_items and pool.source != "sitemap":
                pool.items.append(
                    TargetItem(
                        url=f"{cfg.ui_base}/items/{uid}", uuid=uid, handle=item.get("handle")
                    )
                )
                added += 1
        if len(objects) < 100:
            break
    return added


async def _discover_containers(user: HttpUser, cfg: RunConfig, pool: TargetPool) -> None:
    if pool.communities and pool.collections:
        return
    res = await user.get(
        f"{cfg.rest_base}/core/communities/search/top",
        req_class="rest",
        accept="application/json",
        params={"size": "50"},
    )
    if not res.ok or not res.text:
        return
    try:
        import json

        data = json.loads(res.text)
    except ValueError:
        return
    for comm in data.get("_embedded", {}).get("communities", []):
        uid = comm.get("uuid")
        if uid:
            pool.communities.append(f"{cfg.ui_base}/communities/{uid}")


async def discover_targets(
    user: HttpUser, cfg: RunConfig, queries: QueryGenerator, rng: random.Random
) -> TargetPool:
    pool = TargetPool(ui_base=cfg.ui_base, rest_base=cfg.rest_base)
    pool.robots = await fetch_robots(user, cfg.ui_base, cfg.good_bot_ua)
    if not pool.robots.fetched:
        pool.notes.append("robots.txt could not be fetched; good bots treat everything as allowed")
    found = await _discover_from_sitemaps(user, cfg, pool)
    if not found:
        pool.notes.append("no usable sitemap; falling back to REST discovery for item URLs")
    vocab_pages = 3 if found else 10
    added = await _discover_from_rest(user, cfg, pool, queries, pages=vocab_pages)
    if not found and added:
        pool.source = "rest-discovery"
    await _discover_containers(user, cfg, pool)
    pool.finalise(rng)
    return pool


__all__ = ["TargetItem", "TargetPool", "discover_targets", "fetch_robots"]
