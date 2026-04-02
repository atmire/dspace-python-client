"""Ordered full-text resolution: Unpaywall → OpenAlex → OpenAIRE → CORE."""

from __future__ import annotations

import httpx

from config import ExternalApiConfig
from sources import ResolveContext, SourceHit, try_core, try_openaire, try_openalex, try_unpaywall


async def get_full_text_from_sources(
    http: httpx.AsyncClient,
    doi: str,
    cfg: ExternalApiConfig,
) -> tuple[SourceHit | None, ResolveContext]:
    """
    Try each source in order; return first hit and context (OpenAlex OA hints).
    """
    ctx = ResolveContext()
    hit = await try_unpaywall(http, doi, cfg.unpaywall_email, cfg.timeout_seconds)
    if hit:
        return hit, ctx
    hit = await try_openalex(http, doi, cfg.unpaywall_email, cfg.timeout_seconds, ctx)
    if hit:
        return hit, ctx
    hit = await try_openaire(
        http,
        doi,
        cfg.openaire_personal_access_token,
        cfg.openaire_refresh_token,
        cfg.timeout_seconds,
        ctx,
    )
    if hit:
        return hit, ctx
    hit = await try_core(http, doi, cfg.core_api_key, cfg.timeout_seconds, ctx)
    if hit:
        return hit, ctx
    return None, ctx


__all__ = ["get_full_text_from_sources"]
