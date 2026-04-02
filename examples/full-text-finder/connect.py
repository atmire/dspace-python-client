"""Authenticate to DSpace (same pattern as examples/seed/seed_client.py)."""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple, Union

from dspace_client import DSpaceAuthClient, DSpaceClient

HTTP_TIMEOUT = 120.0


async def connect_fulltext_client(
    base_url: str,
    username: str,
    password: str,
    target_versions: Union[str, List[str]],
    *,
    strict_versions: bool = False,
    courtesy_delay: float = 0.0,
    slow_request_threshold_seconds: float = 5.0,
    slow_request_callback: Optional[Callable[[str, str, float], None]] = None,
) -> Tuple[DSpaceAuthClient, DSpaceClient]:
    auth = DSpaceAuthClient(base_url, timeout=HTTP_TIMEOUT)
    jwt, _status = await auth.authenticate(username, password)

    client = DSpaceClient(
        base_url=base_url,
        jwt_token=jwt,
        csrf_token=auth.csrf_token,
        http_client=auth.client,
        target_versions=target_versions,
        timeout=HTTP_TIMEOUT,
        courtesy_delay=courtesy_delay,
        slow_request_threshold_seconds=slow_request_threshold_seconds,
        slow_request_callback=slow_request_callback,
    )

    if strict_versions:
        await client.verify_server_version(raise_on_mismatch=True)

    return auth, client


__all__ = ["connect_fulltext_client", "HTTP_TIMEOUT"]
