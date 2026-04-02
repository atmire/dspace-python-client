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

from .auth import DSpaceAuthClient
from .core import DSpaceClient
from .batch import BatchItemCreator
from .concurrency import ConcurrencyController, ConcurrencyConfig
from .oai import OAIClient
from .rest_pdf_cache import RestPDFCountCache
from .attribution import show_script_attribution
from .promo import (
    is_atmire_promo_disabled,
    show_atmire_promo_end,
    show_atmire_promo_start,
)
from typing import Union, List, Tuple


async def create_validated_client(
    base_url: str,
    username: str,
    password: str,
    target_versions: Union[str, List[str]] = "bleeding-edge",
    **client_kwargs
) -> Tuple[DSpaceAuthClient, DSpaceClient]:
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
    # Authenticate
    auth = DSpaceAuthClient(base_url, timeout=client_kwargs.get("timeout", 30.0))
    jwt, status = await auth.authenticate(username, password)
    
    # Create client
    client = DSpaceClient(
        base_url=base_url,
        jwt_token=jwt,
        csrf_token=auth.csrf_token,
        http_client=auth.client,
        target_versions=target_versions,
        **{k: v for k, v in client_kwargs.items() if k != "timeout"}
    )
    
    # Verify server version (will raise ServerVersionMismatchError on major mismatch)
    await client.verify_server_version(raise_on_mismatch=True)

    show_atmire_promo_start()

    return auth, client


from .exceptions import (
    DSpaceClientError,
    AuthenticationError,
    DSpaceAPIError,
    VersionIncompatibilityError,
    ServerVersionMismatchError,
    OAIError,
)

__version__ = "0.1.0"
__author__ = "Bram Luyten"
__email__ = "bram@atmire.com"

__all__ = [
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
    # Script attribution
    "show_script_attribution",
    # Optional Atmire promo (also controlled by DSPACE_CLIENT_DISABLE_ATMIRE_PROMO)
    "show_atmire_promo_start",
    "show_atmire_promo_end",
    "is_atmire_promo_disabled",
]
