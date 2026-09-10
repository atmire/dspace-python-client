"""DSpace REST API client for Python.

A comprehensive Python client for the DSpace REST API with version-aware
compatibility checking and automatic documentation management.

Key Features:
- Version-first initialization with automatic documentation fetching
- Pre-execution validation for all API operations
- Multi-version compatibility support
- Git-based documentation management with auto-updates
- Rich console output for beautiful user experience
- Batch operations with adaptive concurrency control

Example:
    # DEVELOPER DECLARES: This script is compatible with DSpace 8.0 and 9.0
    TARGET_VERSIONS = ["8.0", "9.0"]
    SCRIPT_AUTHORS = "Bram Luyten (Atmire)"

    from dspace_client import create_validated_client, ServerVersionMismatchError, show_script_attribution

    async def main():
        show_script_attribution(SCRIPT_AUTHORS)
        base_url = input("DSpace base URL: ")
        username = input("Username: ")
        password = input("Password: ")
        try:
            auth, client = await create_validated_client(
                base_url=base_url,
                username=username,
                password=password,
                target_versions=TARGET_VERSIONS,
            )
            community = await client.create_community("My Community")
        except ServerVersionMismatchError as e:
            print(f"Cannot connect: {e}")
            print(f"This script only works with DSpace versions: {', '.join(TARGET_VERSIONS)}")
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from .attribution import show_script_attribution
from .auth import DSpaceAuthClient
from .batch import BatchItemCreator
from .concurrency import ConcurrencyConfig, ConcurrencyController
from .core import DSpaceClient
from .credentials import prompt_and_authenticate
from .exceptions import (
    AuthenticationError,
    DSpaceAPIError,
    DSpaceClientError,
    OAIError,
    ServerVersionMismatchError,
    VersionIncompatibilityError,
)
from .oai import OAIClient
from .promo import (
    is_atmire_promo_disabled,
    show_atmire_promo_end,
    show_atmire_promo_start,
)
from .rest_pdf_cache import RestPDFCountCache

logging.getLogger("dspace_client").addHandler(logging.NullHandler())


async def create_validated_client(
    base_url: str,
    username: str,
    password: str,
    target_versions: str | list[str] = "bleeding-edge",
    *,
    show_atmire_promo: bool = False,
    fetch_docs: bool = False,
    **client_kwargs: Any,
) -> tuple[DSpaceAuthClient, DSpaceClient]:
    """
    Authenticate and create DSpaceClient with automatic version validation.

    This helper function performs the complete flow:
    1. Authenticate with DSpace server
    2. Create DSpaceClient with specified target_versions
    3. Verify server version compatibility
    4. Raise ServerVersionMismatchError if major version mismatch
    5. Print warnings for minor version differences
    6. Optional Atmire thank-you panel when the session ends (disable with DSPACE_CLIENT_DISABLE_ATMIRE_PROMO=1)

    Args:
        base_url: DSpace server base URL
        username: Username for authentication
        password: Password for authentication
        target_versions: DSpace version(s) that the client should be compatible with.
                        Can be a single string (e.g., "9.0") or list (e.g., ["8.0", "9.0"]).
                        Defaults to "bleeding-edge".
        **client_kwargs: Additional keyword arguments passed to DSpaceClient constructor
                        (timeout, max_retries, courtesy_delay, etc.)
        show_atmire_promo: When True, show optional Atmire thank-you panels at session start/end.
        fetch_docs: When True, fetch RestContract documentation for each target version.

    Returns:
        Tuple of (auth_client, dspace_client)

    Raises:
        AuthenticationError: If authentication fails
        ServerVersionMismatchError: If server version major version doesn't match target_versions

    Example:
        from dspace_client import create_validated_client

        auth, client = await create_validated_client(
            base_url="https://demo.dspace.org",
            username="admin@example.com",
            password="password",
            target_versions=["8.0", "9.0"]
        )

        # Server version will be validated automatically
        # If major version mismatch, ServerVersionMismatchError is raised
        # If minor version difference, warning is printed but connection proceeds
    """
    timeout = client_kwargs.pop("timeout", 30.0)
    auth = DSpaceAuthClient(base_url, timeout=timeout)
    jwt, _status = await auth.authenticate(username, password)

    client = DSpaceClient(
        base_url=base_url,
        jwt_token=jwt,
        csrf_token=auth.csrf_token,
        http_client=auth.client,
        target_versions=target_versions,
        timeout=timeout,
        **client_kwargs,
    )

    await client.verify_server_version(raise_on_mismatch=True)

    if fetch_docs:
        versions = [target_versions] if isinstance(target_versions, str) else list(target_versions)
        for version in versions:
            await client.docs_fetcher.fetch_version(version)

    if show_atmire_promo:
        auth.show_atmire_promo = True
        show_atmire_promo_start()

    return auth, client


@asynccontextmanager
async def managed_client(
    base_url: str,
    username: str,
    password: str,
    target_versions: str | list[str] = "bleeding-edge",
    **client_kwargs: Any,
) -> AsyncIterator[tuple[DSpaceAuthClient, DSpaceClient]]:
    """Authenticate, validate, and guarantee ``auth.close()`` in a ``finally`` block."""
    auth: DSpaceAuthClient | None = None
    try:
        auth, client = await create_validated_client(
            base_url=base_url,
            username=username,
            password=password,
            target_versions=target_versions,
            **client_kwargs,
        )
        yield auth, client
    finally:
        if auth is not None:
            await auth.close()


async def create_anonymous_client(
    base_url: str,
    target_versions: str | list[str] = "bleeding-edge",
    **client_kwargs: Any,
) -> tuple[httpx.AsyncClient, DSpaceClient]:
    """Build a DSpaceClient for anonymous, read-only access.

    This is the read-only sibling of :func:`create_validated_client`. It
    creates a fresh ``httpx.AsyncClient`` (no authentication, no cookies),
    instantiates :class:`DSpaceClient` with ``jwt_token=None`` and
    ``csrf_token=None``, and verifies the server version against
    ``target_versions``.

    Mutating operations (POST/PUT/PATCH/DELETE) raise
    :class:`AuthenticationError` before any request is dispatched, so this
    client is safe to hand to scripts that only read public data.

    The caller owns the returned ``httpx.AsyncClient`` and must close it,
    typically via ``await http.aclose()`` in a ``finally`` block (mirrors
    ``await auth.close()`` in the authenticated path).

    Args:
        base_url: DSpace server base URL.
        target_versions: DSpace version(s) the client should be compatible
            with. See :func:`create_validated_client` for value semantics.
        **client_kwargs: Additional keyword arguments forwarded to
            :class:`DSpaceClient` (e.g. ``timeout``, ``courtesy_delay``,
            ``slow_request_threshold_seconds``, ``slow_request_callback``).

    Returns:
        Tuple of (httpx.AsyncClient, DSpaceClient).

    Raises:
        ServerVersionMismatchError: If the server's major version does not
            match ``target_versions``.

    Example:
        from dspace_client import create_anonymous_client

        http, client = await create_anonymous_client(
            base_url="https://demo.dspace.org",
            target_versions=["7.6", "8.0", "9.0"],
        )
        try:
            results = await client.search_items(query="dc.type:\"Journal article\"")
        finally:
            await http.aclose()
    """
    timeout = client_kwargs.pop("timeout", 30.0)
    http = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    client = DSpaceClient(
        base_url=base_url,
        jwt_token=None,
        csrf_token=None,
        http_client=http,
        target_versions=target_versions,
        **client_kwargs,
    )
    await client.verify_server_version(raise_on_mismatch=True)
    return http, client


__version__ = "0.1.0"
__author__ = "Bram Luyten"
__email__ = "bram@atmire.com"

__all__ = [  # noqa: RUF022 - grouped by category deliberately; more readable than sorted
    # Main client classes
    "DSpaceAuthClient",
    "DSpaceClient",
    "BatchItemCreator",
    "OAIClient",
    "RestPDFCountCache",
    # Concurrency control
    "ConcurrencyController",
    "ConcurrencyConfig",
    # Exceptions
    "DSpaceClientError",
    "AuthenticationError",
    "DSpaceAPIError",
    "VersionIncompatibilityError",
    "ServerVersionMismatchError",
    "OAIError",
    # Helper functions
    "create_validated_client",
    "create_anonymous_client",
    "managed_client",
    "prompt_and_authenticate",
    # Script attribution
    "show_script_attribution",
    # Optional Atmire promo (also controlled by DSPACE_CLIENT_DISABLE_ATMIRE_PROMO)
    "show_atmire_promo_start",
    "show_atmire_promo_end",
    "is_atmire_promo_disabled",
]
