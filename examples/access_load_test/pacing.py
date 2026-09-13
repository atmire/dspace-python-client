"""Think time and user start scheduling. The browser is never throttled; only actions are."""

from __future__ import annotations

import math
import random

LOGNORMAL_SIGMA = 0.5


def think_time(mean_s: float, rng: random.Random, *, fixed: bool = False) -> float:
    """
    Gap between two actions of one user.

    Lognormal around ``mean_s`` (clipped to four times the mean) so users desynchronise
    instead of acting in lockstep. ``fixed`` returns the mean itself.
    """
    if mean_s <= 0:
        return 0.0
    if fixed:
        return mean_s
    mu = math.log(mean_s) - LOGNORMAL_SIGMA**2 / 2
    return min(4 * mean_s, rng.lognormvariate(mu, LOGNORMAL_SIGMA))


def start_offsets(count: int, spread_s: float, rng: random.Random) -> list[float]:
    """Stagger ``count`` users over ``spread_s`` seconds so the first burst is not a wall."""
    if count <= 0:
        return []
    if spread_s <= 0 or count == 1:
        return [0.0] * count
    return sorted(rng.uniform(0, spread_s) for _ in range(count))


def session_length(mean_pages: float, max_pages: int, rng: random.Random) -> int:
    """Geometric number of pages per human session, at least one, at most ``max_pages``."""
    p = 1.0 / max(1.0, mean_pages)
    n = 1
    while n < max_pages and rng.random() > p:
        n += 1
    return n


__all__ = ["session_length", "start_offsets", "think_time"]
