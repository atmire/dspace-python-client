"""DSpace session refresh, re-auth on 401, and adaptive throttling."""

from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable

from rich.console import Console

from dspace_client import DSpaceAPIError, DSpaceAuthClient, DSpaceClient
from dspace_client.throttle import ThrottleController

console = Console()


async def _ensure_fresh_session(
    auth: DSpaceAuthClient,
    client: DSpaceClient,
    username: str,
    password: str,
) -> None:
    """
    Proactively refresh the session if it is near or past its configured max age.

    Updates the DSpaceClient's JWT/CSRF tokens if a refresh occurs.
    """
    jwt = await auth.ensure_session(username, password)
    # Keep client in sync with latest auth tokens
    client.jwt_token = auth.jwt_token or jwt
    if auth.csrf_token:
        client.csrf_token = auth.csrf_token


async def _call_with_reauth(
    auth: DSpaceAuthClient,
    client: DSpaceClient,
    username: str,
    password: str,
    func: Callable[[], Awaitable],
) -> object:
    """
    Wrap a DSpaceClient operation so that:
    - It uses proactive session refresh via _ensure_fresh_session.
    - On first 401 DSpaceAPIError, it forces re-auth and retries once.
    """
    await _ensure_fresh_session(auth, client, username, password)
    retry_on_401 = os.environ.get("DSPACE_RETRY_ON_401", "1").lower() not in ("0", "false", "no")

    try:
        return await func()
    except DSpaceAPIError as e:
        status = getattr(e, "status_code", None)
        if not retry_on_401 or status != 401:
            raise

        console.print(
            "[yellow]Received 401 from DSpace API; refreshing session and retrying once...[/yellow]"
        )
        # Force re-auth and sync client tokens, then retry once
        jwt = await auth.ensure_session(username, password, force=True)
        client.jwt_token = auth.jwt_token or jwt
        if auth.csrf_token:
            client.csrf_token = auth.csrf_token

        return await func()


async def _throttled_call(
    auth: DSpaceAuthClient,
    client: DSpaceClient,
    username: str,
    password: str,
    throttle: ThrottleController,
    func: Callable[[], Awaitable],
) -> object:
    """
    Wrap a DSpaceClient operation with adaptive, single-threaded throttling.

    This keeps execution linear:
    - Sleep for the current delay
    - Delegate to _call_with_reauth (which handles session refresh/401 retry)
    - Record duration and any HTTP status code for feedback
    """
    await throttle.before_call()
    start = time.time()
    try:
        result = await _call_with_reauth(auth, client, username, password, func)
        duration = time.time() - start
        await throttle.after_call(duration, success=True, status_code=None)
        return result
    except DSpaceAPIError as e:
        duration = time.time() - start
        status = getattr(e, "status_code", None)
        await throttle.after_call(duration, success=False, status_code=status)
        raise
    except Exception:
        duration = time.time() - start
        await throttle.after_call(duration, success=False, status_code=None)
        raise
