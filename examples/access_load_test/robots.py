"""
robots.txt rules with wildcard support, the way major crawlers interpret them.

The stdlib ``urllib.robotparser`` treats ``*`` literally, and DSpace's default robots.txt
uses patterns like ``Disallow: /browse/*``. This implementation supports ``*`` and ``$``
with longest-match precedence (Google's documented behaviour) and exposes crawl-delay and
sitemap directives for the report.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class _Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]


@dataclass
class RobotsRules:
    raw: str = ""
    fetched: bool = False
    status: int | None = None
    sitemaps: list[str] = field(default_factory=list)
    crawl_delay: float | None = None
    matched_agent: str = "*"
    _rules: list[_Rule] = field(default_factory=list)

    # -- parsing -----------------------------------------------------------------

    @classmethod
    def parse(cls, text: str, user_agent: str = "*") -> RobotsRules:
        """Parse ``text`` and keep the group that best matches ``user_agent``."""
        groups: list[tuple[list[str], list[tuple[str, str]]]] = []
        current_agents: list[str] = []
        current_lines: list[tuple[str, str]] = []
        sitemaps: list[str] = []
        last_was_agent = False
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "sitemap":
                if value:
                    sitemaps.append(value)
                continue
            if key == "user-agent":
                if not last_was_agent and (current_agents or current_lines):
                    groups.append((current_agents, current_lines))
                    current_agents, current_lines = [], []
                current_agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            current_lines.append((key, value))
        if current_agents or current_lines:
            groups.append((current_agents, current_lines))

        ua = user_agent.lower()
        chosen: list[tuple[str, str]] = []
        chosen_agent = "*"
        best_len = -1
        for agents, lines in groups:
            for a in agents:
                if a == "*":
                    if best_len < 0:
                        chosen, chosen_agent, best_len = lines, "*", 0
                elif a in ua and len(a) > best_len:
                    chosen, chosen_agent, best_len = lines, a, len(a)

        rules = cls(raw=text, sitemaps=sitemaps, matched_agent=chosen_agent)
        for key, value in chosen:
            if key in ("allow", "disallow"):
                if not value:
                    continue  # "Disallow:" with no path allows everything
                rules._rules.append(_Rule(key == "allow", value, _compile(value)))
            elif key == "crawl-delay":
                with contextlib.suppress(ValueError):
                    rules.crawl_delay = float(value)
        return rules

    # -- queries -----------------------------------------------------------------

    def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        best: _Rule | None = None
        best_len = -1
        for rule in self._rules:
            if rule.regex.match(path) and len(rule.pattern) > best_len:
                best, best_len = rule, len(rule.pattern)
            elif rule.regex.match(path) and len(rule.pattern) == best_len and rule.allow:
                best = rule  # tie: allow wins
        return True if best is None else best.allow

    @property
    def disallow_patterns(self) -> list[str]:
        return [r.pattern for r in self._rules if not r.allow]

    @property
    def allow_patterns(self) -> list[str]:
        return [r.pattern for r in self._rules if r.allow]

    def to_dict(self) -> dict:
        return {
            "fetched": self.fetched,
            "status": self.status,
            "matched_agent_group": self.matched_agent,
            "disallow": self.disallow_patterns,
            "allow": self.allow_patterns,
            "crawl_delay": self.crawl_delay,
            "sitemaps": list(self.sitemaps),
        }


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    parts = [re.escape(p) for p in body.split("*")]
    regex = ".*".join(parts)
    if anchored:
        regex += "$"
    return re.compile("^" + regex)


__all__ = ["RobotsRules"]
