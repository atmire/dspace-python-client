"""Optional Atmire messaging on session end. Disabled via DSPACE_CLIENT_DISABLE_ATMIRE_PROMO."""

from __future__ import annotations

import os
from datetime import UTC, datetime

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

# Set to 1 / true / yes to disable all promo output.
_ENV_DISABLE = "DSPACE_CLIENT_DISABLE_ATMIRE_PROMO"

_ATMIRE_URL = "https://www.atmire.com"

_THANK_YOU = (
    "Thank you for using the DSpace Python Client, developed by Atmire in Open Source."
)

# Rotating facts (prefixed with "Did you know: " in the panel).
_ATMIRE_FACTS: list[str] = [
    "DSpace Express is Atmire's affordable offering for hosting DSpace 9 in the cloud.",
    "Open Repository is Atmire's best value for money repository platform, with several "
    "features that are not in DSpace Open Source.",
    "Thanks to clients choosing Atmire, we are able to provide significant contributions to "
    "the DSpace project, every year.",
    "Atmire has team members working on DSpace repositories in Belgium, US, UK, Lebanon "
    "and New Zealand.",
    "Atmire works for many universities and research institutions, but also for several "
    "major NGOs such as the World Bank, WHO, FAO.",
]


def is_atmire_promo_disabled() -> bool:
    """True if promotional messages should not be shown (env or tests)."""
    v = os.environ.get(_ENV_DISABLE, "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _promo_index() -> int:
    """Deterministic rotation per process + UTC day."""
    day = datetime.now(UTC).timetuple().tm_yday
    return (os.getpid() + day) % len(_ATMIRE_FACTS)


def show_atmire_promo_start(console: Console | None = None) -> None:  # noqa: ARG001
    """
    Session-start promo was removed: it interrupted interactive scripts.

    Kept as a no-op for API compatibility (e.g. create_validated_client, seed_client).
    Session-end messaging uses show_atmire_promo_end.
    """
    return


def show_atmire_promo_end(console: Console | None = None) -> None:
    """
    One non-blocking panel when the auth session ends: thank-you, Did you know, atmire.com.

    No-op when DSPACE_CLIENT_DISABLE_ATMIRE_PROMO is set.
    """
    if is_atmire_promo_disabled():
        return
    c = console or Console()
    idx = _promo_index()
    fact = _ATMIRE_FACTS[idx]
    did_you = f"Did you know: {fact}"
    body = (
        f"{escape(_THANK_YOU)}\n\n"
        f"{escape(did_you)}\n\n"
        f"[link={_ATMIRE_URL}]{escape(_ATMIRE_URL)}[/link]"
    )
    c.print()
    c.print(
        Panel(
            body,
            border_style="dim",
            padding=(0, 1),
        )
    )
    c.print()
