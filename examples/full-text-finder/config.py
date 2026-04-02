"""External API credentials and timeouts (env vars, optional prompts)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from rich.console import Console  # noqa: TC002  # runtime use in load_external_config


@dataclass(frozen=True)
class ExternalApiConfig:
    """Settings for Unpaywall, OpenAlex, OpenAIRE, CORE."""

    unpaywall_email: str
    core_api_key: str
    openaire_refresh_token: str
    openaire_personal_access_token: str
    timeout_seconds: float


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def load_external_config(console: Console, *, prompt: bool) -> ExternalApiConfig:
    """
    Load from env; optionally prompt for missing required values.

    Env vars:
      FULLTEXT_UNPAYWALL_EMAIL (required for Unpaywall polite pool)
      FULLTEXT_CORE_API_KEY
      FULLTEXT_OPENAIRE_REFRESH_TOKEN
      FULLTEXT_OPENAIRE_PAT — optional OpenAIRE Personal Access Token (preferred over refresh token)
      FULLTEXT_HTTP_TIMEOUT (seconds, default 30)
    """
    email = _env("FULLTEXT_UNPAYWALL_EMAIL")
    core_key = _env("FULLTEXT_CORE_API_KEY")
    oa_refresh = _env("FULLTEXT_OPENAIRE_REFRESH_TOKEN")
    oa_pat = _env("FULLTEXT_OPENAIRE_PAT")
    timeout_s = float(_env("FULLTEXT_HTTP_TIMEOUT", "30") or "30")

    if prompt:
        if not email:
            email = console.input(
                "[bold cyan]Email for Unpaywall/OpenAlex (mailto)[/bold cyan] "
                "[dim](required for API politeness):[/dim] "
            ).strip()
        if not core_key:
            core_key = console.input(
                "[bold cyan]CORE API key[/bold cyan] [dim](optional, press Enter to skip):[/dim] "
            ).strip()
        if not oa_refresh and not oa_pat:
            oa_pat = console.input(
                "[bold cyan]OpenAIRE Personal Access Token[/bold cyan] [dim](optional):[/dim] "
            ).strip()
            if not oa_pat:
                oa_refresh = console.input(
                    "[bold cyan]OpenAIRE refresh token[/bold cyan] [dim](optional):[/dim] "
                ).strip()

    if not email:
        _msg = (
            "FULLTEXT_UNPAYWALL_EMAIL is required (Unpaywall and OpenAlex expect a mailto)."
        )
        raise ValueError(_msg)

    return ExternalApiConfig(
        unpaywall_email=email,
        core_api_key=core_key,
        openaire_refresh_token=oa_refresh,
        openaire_personal_access_token=oa_pat,
        timeout_seconds=timeout_s,
    )


__all__ = ["ExternalApiConfig", "load_external_config"]
