"""Core DSpace REST API client for CRUD operations with version validation."""

import asyncio
import logging
import re
import time

import httpx
import orjson
from typing import Any, Optional, Union, List, Callable
from pathlib import Path
from rich.console import Console
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    retry_if_result,
)

from .exceptions import (
    AuthenticationError,
    DSpaceAPIError,
    ServerVersionMismatchError,
    VersionIncompatibilityError,
)
from .version import VersionCompatibility
from .docs import RestContractFetcher
from .concurrency import AdaptiveDelayController, AdaptiveDelayConfig

from .rest_pdf_cache import RestPDFCountCache

logger = logging.getLogger(__name__)
console = Console()


def should_retry_request(exception):
    """Check if a request should be retried based on the exception."""
    if isinstance(exception, (httpx.ConnectTimeout, httpx.TimeoutException)):
        return True
    
    if isinstance(exception, httpx.HTTPStatusError):
        # Retry on rate limiting and server errors
        status_code = exception.response.status_code
        return status_code in (429, 503, 502, 504)
    
    return False


class DSpaceClient:
    """Client for DSpace REST API operations with version validation."""
    
    def __init__(
        self,
        base_url: str,
        jwt_token: Optional[str] = None,
        csrf_token: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        target_versions: Union[str, List[str]] = "bleeding-edge",
        timeout: float = 30.0,
        max_retries: int = 3,
        courtesy_delay: float = 1.0,
        slow_request_threshold_seconds: float = 5.0,
        slow_request_callback: Optional[Callable[[str, str, float], None]] = None,
    ):
        """
        Initialize DSpace API client with version compatibility checking.

        For authenticated use, pass the authenticated HTTP client from
        DSpaceAuthClient (cookies must persist; creating a fresh client loses
        DSPACE-XSRF-COOKIE and yields 403s on modifying requests).

        For anonymous, read-only use, pass ``jwt_token=None`` and
        ``csrf_token=None`` along with a plain ``httpx.AsyncClient``. Mutating
        operations (POST/PUT/PATCH/DELETE) raise :class:`AuthenticationError`
        before any request is dispatched. The :func:`create_anonymous_client`
        helper wires this up.

        Args:
            base_url: DSpace server base URL
            jwt_token: JWT bearer token from authentication, or ``None`` for
                anonymous read-only access.
            csrf_token: CSRF token for modifying requests (refreshed after
                login!), or ``None`` for anonymous use.
            http_client: HTTP client. Use the authenticated client from
                ``DSpaceAuthClient`` for authenticated mode, or a plain
                ``httpx.AsyncClient`` for anonymous mode.
            target_versions: DSpace version(s) that this client is declared compatible with.
                This restricts which DSpace servers you can connect to:
                - Exact version match (e.g., 9.0 == 9.0) → OK
                - Minor version difference (e.g., 9.0 vs 9.1, same major) → Warning but allowed
                - Major version difference (e.g., 7.x vs 8.0+) → Connection rejected
                
                Values:
                - "bleeding-edge" (default): Latest main branch from RestContract
                - "7.0", "8.0", "9.0": Specific stable versions
                - ["7.6", "8.0", "9.0"]: Multiple versions (server must match one)
                
                After creating the client, call verify_server_version() to validate
                the server version against target_versions. Consider using
                create_validated_client() helper function for automatic validation.
                
                Note: This also ensures all operations work in the specified version(s).
                If an operation is not supported in any target version, a
                VersionIncompatibilityError is raised before the API call.
            timeout: Request timeout in seconds
            max_retries: Maximum number of retries for failed requests
            courtesy_delay: Delay in seconds between API calls (0 for no delay)
            slow_request_threshold_seconds: If a request takes longer than this, it is
                logged at WARNING and optional callback is invoked (default 5.0).
            slow_request_callback: Optional callback(method, endpoint, duration_seconds)
                for slow requests; use to collect or display slow-request patterns.
        
        On initialization:
        1. Validates target_versions parameter
        2. Checks if git repos exist for target version(s)
        3. If not cloned, git clone RestContract repository
        4. If exists, git fetch latest changes (if older than 24h)
        5. Checkout correct branch/tag for each version
        6. Loads compatibility rules for validation
        7. Sets up automatic update checking
        
        Note: Version validation is NOT performed during __init__. Call
        verify_server_version() after initialization, or use the
        create_validated_client() helper function.
        """
        self.base_url = base_url.rstrip("/")
        self.jwt_token = jwt_token
        self.csrf_token = csrf_token
        self.client = http_client  # ⚠️ CRITICAL: Reuse authenticated client
        self.timeout = timeout
        self.max_retries = max_retries
        self.courtesy_delay = courtesy_delay
        self._last_request_time = 0.0
        self.slow_request_threshold_seconds = slow_request_threshold_seconds
        self.slow_request_callback = slow_request_callback
        
        # Initialize version compatibility system
        self.validator = VersionCompatibility(target_versions)
        self.docs_fetcher = RestContractFetcher()
        self.target_versions = target_versions if isinstance(target_versions, list) else [target_versions]
        #: Last result of :meth:`detect_dspace_version` (also set when called from ``verify_server_version``).
        self._last_detected_server_version: Optional[str] = None

        # Fetch documentation for target versions (this will be async in real implementation)
        # For now, we'll assume docs are fetched during initialization
        console.print(f"[dim]Initializing DSpace client for versions: {target_versions}[/dim]")
    
    def _get_headers(self, include_csrf: bool = False) -> dict[str, str]:
        """
        Get standard headers for API requests.

        Authorization is omitted in anonymous mode (``jwt_token`` is ``None``);
        the X-XSRF-TOKEN header is also only set when a CSRF token is present.

        Args:
            include_csrf: Whether to include X-XSRF-TOKEN header
        """
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.jwt_token:
            headers["Authorization"] = f"Bearer {self.jwt_token}"
        if include_csrf and self.csrf_token:
            headers["X-XSRF-TOKEN"] = self.csrf_token
        return headers

    def _require_auth(self, operation: str) -> None:
        """Raise AuthenticationError if the client is in anonymous mode.

        Use at the top of any method that bypasses :meth:`_request` to dispatch
        a mutating request directly via ``self.client``.
        """
        if not self.jwt_token:
            raise AuthenticationError(
                f"{operation} requires authentication; this client was created "
                "in anonymous mode. Use create_validated_client() instead of "
                "create_anonymous_client()."
            )

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=should_retry_request,
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> httpx.Response:
        """
        Make an HTTP request to DSpace API with version validation.
        
        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint path
            json_data: JSON data for request body
            params: Query parameters
        
        Returns:
            Response object
        
        Raises:
            AuthenticationError: If a mutating request is attempted while the
                client is in anonymous mode (no JWT token was provided).
            DSpaceAPIError: If request fails
            VersionIncompatibilityError: If operation not supported in target versions
        """
        # STEP 0: Block mutators in anonymous mode before doing any work.
        if method.upper() in ("POST", "PUT", "PATCH", "DELETE") and not self.jwt_token:
            raise AuthenticationError(
                f"{method.upper()} {endpoint} requires authentication; this client "
                "was created in anonymous mode (no JWT token). Use "
                "create_validated_client() instead of create_anonymous_client()."
            )

        # STEP 1: Validate operation against target versions
        self.validator.validate_before_call(
            method_name=f"{method.lower()}_{endpoint.split('/')[-1]}",
            endpoint=endpoint,
            operation=method
        )
        
        # STEP 2: Apply courtesy delay
        if self.courtesy_delay > 0:
            import asyncio
            elapsed = time.time() - self._last_request_time
            if elapsed < self.courtesy_delay:
                await asyncio.sleep(self.courtesy_delay - elapsed)
        
        url = f"{self.base_url}/server/api/{endpoint.lstrip('/')}"
        
        # Include CSRF token for modifying requests
        # DSpace requires X-XSRF-TOKEN header on ALL modifying requests (POST, PUT, PATCH, DELETE)
        # GET requests don't need it. See csrf-tokens.md in REST contract docs.
        is_modifying = method.upper() in ("POST", "PUT", "PATCH", "DELETE")
        headers = self._get_headers(include_csrf=is_modifying)
        
        # Debug logging for modifying requests (commented out - uncomment if debugging)
        # if is_modifying:
        #     console.print(f"\n[dim]→ {method} {url}[/dim]")
        #     console.print(f"[dim]  Headers:[/dim]")
        #     console.print(f"[dim]    Authorization: Bearer {self.jwt_token[:20]}...[/dim]")
        #     console.print(f"[dim]    Content-Type: {headers.get('Content-Type')}[/dim]")
        #     if "X-XSRF-TOKEN" in headers:
        #         console.print(f"[dim]    X-XSRF-TOKEN: {headers['X-XSRF-TOKEN'][:20]}...[/dim]")
        #     else:
        #         console.print(f"[red]    X-XSRF-TOKEN: MISSING![/red]")
        #     
        #     console.print(f"[dim]  Cookies in client jar:[/dim]")
        #     for cookie in self.client.cookies.jar:
        #         console.print(f"[dim]    {cookie.name} = {cookie.value[:20]}...[/dim]")
        #     
        #     if json_data:
        #         console.print(f"[dim]  Body keys: {list(json_data.keys())}[/dim]")
        
        request_start = time.perf_counter()
        try:
            response = await self.client.request(
                method,
                url,
                headers=headers,
                json=json_data,
                params=params,
            )
            
            # if is_modifying:
            #     console.print(f"[dim]← Status: {response.status_code}[/dim]")
            
            # Check for errors
            if response.status_code >= 400:
                error_detail = response.text
                try:
                    error_json = response.json()
                    error_detail = orjson.dumps(error_json).decode()
                except:
                    pass
                
                console.print(f"[red]Error response:[/red]")
                console.print(f"[red]  Status: {response.status_code}[/red]")
                console.print(f"[red]  Response headers:[/red]")
                for key, value in response.headers.items():
                    console.print(f"[red]    {key}: {value}[/red]")
                console.print(f"[red]  Body: {error_detail[:500]}[/red]")
                
                raise DSpaceAPIError(
                    f"{method} {url} failed with status {response.status_code}: {error_detail}",
                    status_code=response.status_code,
                )
            
            # Update last request time for courtesy delay
            self._last_request_time = time.time()
            return response
        
        except httpx.RequestError as e:
            raise DSpaceAPIError(f"Request failed: {e}")
        finally:
            duration = time.perf_counter() - request_start
            if duration >= self.slow_request_threshold_seconds:
                logger.warning(
                    "Slow request: %s %s %.2fs",
                    method,
                    endpoint,
                    duration,
                )
                if self.slow_request_callback:
                    self.slow_request_callback(method, endpoint, duration)
    
    # ========== Communities ==========
    
    async def create_community(
        self,
        name: str,
        metadata: Optional[dict] = None,
        parent_uuid: Optional[str] = None,
    ) -> dict:
        """
        Create a community.
        
        Args:
            name: Community name
            metadata: Metadata dictionary (e.g., {"dc.title": [{"value": "..."}]})
            parent_uuid: UUID of parent community (for subcommunity)
        
        Returns:
            Created community object
        """
        if metadata is None:
            metadata = {}
        
        # Ensure dc.title is set
        if "dc.title" not in metadata:
            metadata["dc.title"] = [{"value": name, "language": None, "authority": None, "confidence": -1}]
        
        payload = {
            "name": name,
            "metadata": metadata,
        }
        
        endpoint = "core/communities"
        if parent_uuid:
            endpoint = f"{endpoint}?parent={parent_uuid}"
        
        response = await self._request("POST", endpoint, json_data=payload)
        return response.json()
    
    async def delete_community(self, uuid: str) -> None:
        """Delete a community by UUID."""
        await self._request("DELETE", f"core/communities/{uuid}")
    
    # ========== Collections ==========
    
    async def create_collection(
        self,
        name: str,
        parent_community_uuid: str,
        metadata: Optional[dict] = None,
    ) -> dict:
        """
        Create a collection.
        
        Args:
            name: Collection name
            parent_community_uuid: UUID of parent community
            metadata: Metadata dictionary
        
        Returns:
            Created collection object
        """
        if metadata is None:
            metadata = {}
        
        # Ensure dc.title is set
        if "dc.title" not in metadata:
            metadata["dc.title"] = [{"value": name, "language": None, "authority": None, "confidence": -1}]
        
        payload = {
            "name": name,
            "metadata": metadata,
        }
        
        response = await self._request(
            "POST",
            f"core/collections?parent={parent_community_uuid}",
            json_data=payload,
        )
        return response.json()
    
    async def delete_collection(self, uuid: str) -> None:
        """Delete a collection by UUID."""
        await self._request("DELETE", f"core/collections/{uuid}")
    
    # ========== Items ==========
    
    async def create_item(
        self,
        name: str,
        owning_collection_uuid: str,
        metadata: Optional[dict] = None,
    ) -> dict:
        """
        Create an item (archived, bypassing workflow).
        
        Args:
            name: Item name
            owning_collection_uuid: UUID of owning collection
            metadata: Metadata dictionary
        
        Returns:
            Created item object
        """
        if metadata is None:
            metadata = {}
        
        # Ensure dc.title is set
        if "dc.title" not in metadata:
            metadata["dc.title"] = [{"value": name, "language": None, "authority": None, "confidence": -1}]
        
        payload = {
            "name": name,
            "metadata": metadata,
            "inArchive": True,
            "discoverable": True,
            "withdrawn": False,
            "type": "item",
        }
        
        response = await self._request(
            "POST",
            f"core/items?owningCollection={owning_collection_uuid}",
            json_data=payload,
        )
        return response.json()
    
    async def delete_item(self, uuid: str) -> None:
        """Delete an item by UUID."""
        await self._request("DELETE", f"core/items/{uuid}")
    
    # ========== Bundles ==========
    
    async def create_bundle(self, item_uuid: str, name: str = "ORIGINAL") -> dict:
        """
        Create a bundle in an item.
        
        Args:
            item_uuid: UUID of parent item
            name: Bundle name (default: ORIGINAL)
        
        Returns:
            Created bundle object
        """
        payload = {
            "name": name,
            "metadata": {},
        }
        
        response = await self._request(
            "POST",
            f"core/items/{item_uuid}/bundles",
            json_data=payload,
        )
        return response.json()
    
    # ========== Bitstreams ==========
    
    async def upload_bitstream(
        self,
        bundle_uuid: str,
        filename: str,
        content: bytes,
        metadata: Optional[dict] = None,
    ) -> dict:
        """
        Upload a bitstream to a bundle.
        
        Args:
            bundle_uuid: UUID of parent bundle
            filename: Filename for the bitstream
            content: Binary content
            metadata: Metadata dictionary
        
        Returns:
            Created bitstream object
        """
        self._require_auth("upload_bitstream")
        url = f"{self.base_url}/server/api/core/bundles/{bundle_uuid}/bitstreams"

        if metadata is None:
            metadata = {}
        
        # Ensure dc.title is set
        if "dc.title" not in metadata:
            metadata["dc.title"] = [{"value": filename, "language": None, "authority": None, "confidence": -1}]
        
        # Debug logging (commented out - uncomment if debugging)
        # console.print(f"\n[dim]→ POST {url} (multipart upload)[/dim]")
        # console.print(f"[dim]  Headers:[/dim]")
        # console.print(f"[dim]    Authorization: Bearer {self.jwt_token[:20]}...[/dim]")
        # console.print(f"[dim]    X-XSRF-TOKEN: {self.csrf_token[:20]}...[/dim]")
        # console.print(f"[dim]  Cookies in client jar:[/dim]")
        # for cookie in self.client.cookies.jar:
        #     console.print(f"[dim]    {cookie.name} = {cookie.value[:20]}...[/dim]")
        # console.print(f"[dim]  File: {filename} ({len(content)} bytes)[/dim]")
        
        try:
            # Upload as multipart/form-data using the authenticated client
            files = {"file": (filename, content, "application/octet-stream")}
            
            # Add metadata as form fields if needed
            data = {}
            if metadata:
                data["metadata"] = orjson.dumps(metadata).decode()
            
            # Use the persistent authenticated client with CSRF token
            response = await self.client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.jwt_token}",
                    "X-XSRF-TOKEN": self.csrf_token,
                },
                files=files,
                data=data,
            )
            
            # console.print(f"[dim]← Status: {response.status_code}[/dim]")
            
            if response.status_code >= 400:
                console.print(f"[red]Error response:[/red]")
                console.print(f"[red]  Status: {response.status_code}[/red]")
                console.print(f"[red]  Body: {response.text[:500]}[/red]")
                raise DSpaceAPIError(
                    f"Bitstream upload failed with status {response.status_code}: {response.text}",
                    status_code=response.status_code,
                )
            
            return response.json()
        
        except httpx.RequestError as e:
            raise DSpaceAPIError(f"Bitstream upload request failed: {e}")
    
    async def delete_bitstream(self, uuid: str) -> None:
        """Delete a bitstream by UUID."""
        await self._request("DELETE", f"core/bitstreams/{uuid}")

    async def get_item_bundles(self, item_uuid: str) -> dict:
        """
        Get the list of bundles for an item (no full item metadata).

        GET core/items/{uuid}/bundles. Returns the bundles list (links + minimal
        representation), not the full item.

        Args:
            item_uuid: UUID of the item

        Returns:
            Response with "bundles" or "_embedded"."bundles" list
        """
        response = await self._request("GET", f"core/items/{item_uuid}/bundles")
        return response.json()

    async def get_bundle_bitstreams(
        self, bundle_uuid: str, embed_format: bool = True
    ) -> dict:
        """
        Get bitstreams in a bundle, optionally with format embedded.

        GET core/bundles/{uuid}/bitstreams. When embed_format=True, requests
        ?embed=format so each bitstream includes format (with id). If the server
        does not support embed on this endpoint, format will be absent and callers
        may need to fetch format per bitstream via get_bitstream_format().

        Args:
            bundle_uuid: UUID of the bundle
            embed_format: If True, request embed=format to get format inline

        Returns:
            Response with bitstreams list (_embedded.bitstreams or bitstreams)
        """
        params = {"embed": "format"} if embed_format else None
        response = await self._request(
            "GET", f"core/bundles/{bundle_uuid}/bitstreams", params=params
        )
        return response.json()

    async def get_bitstream_format(self, bitstream_uuid: str) -> dict:
        """
        Get the format of a bitstream.

        GET core/bitstreams/{uuid}/format. Returns format object (includes id).

        Args:
            bitstream_uuid: UUID of the bitstream

        Returns:
            Format object with id, shortDescription, mimetype, etc.
        """
        response = await self._request(
            "GET", f"core/bitstreams/{bitstream_uuid}/format"
        )
        return response.json()

    async def get_bitstream_formats(self, page: int = 0, size: int = 100) -> dict:
        """
        Get the list of bitstream formats from the registry.

        GET core/bitstreamformats. Used to resolve e.g. PDF to a format id.
        Paginated; use page/size to iterate if needed.

        Args:
            page: Page number (0-based)
            size: Page size

        Returns:
            Response with _embedded.bitstreamformats or similar list
        """
        response = await self._request(
            "GET",
            "core/bitstreamformats",
            params={"page": page, "size": size},
        )
        return response.json()

    # ========== Statistics ==========
    
    async def create_item_view(
        self,
        target_uuid: str,
        target_type: str = "item",
        referrer: Optional[str] = None,
    ) -> dict:
        """
        Create a view event for an item.
        
        Endpoint: POST /api/statistics/viewevents
        
        Args:
            target_uuid: UUID of the item being viewed
            target_type: Type of object (default: "item")
            referrer: Optional referrer URL
        
        Returns:
            Response from statistics endpoint
        """
        payload = {
            "targetId": target_uuid,
            "targetType": target_type,
        }
        
        if referrer:
            payload["referrer"] = referrer
        
        response = await self._request("POST", "statistics/viewevents", json_data=payload)
        return response.json()
    
    # ========== EPeople ==========
    
    async def create_eperson(
        self,
        email: str,
        first_name: str,
        last_name: str,
    ) -> dict:
        """
        Create an EPerson account.
        
        Endpoint: POST /api/eperson/epersons
        
        Args:
            email: Unique email address
            first_name: First name
            last_name: Last name
        
        Returns:
            Created EPerson object with UUID
        """
        payload = {
            "email": email,
            "metadata": {
                "eperson.firstname": [{
                    "value": first_name,
                    "language": None,
                    "authority": None,
                    "confidence": -1
                }],
                "eperson.lastname": [{
                    "value": last_name,
                    "language": None,
                    "authority": None,
                    "confidence": -1
                }]
            },
            "canLogIn": True,
            "requireCertificate": False,
            "type": "eperson"
        }
        
        response = await self._request("POST", "eperson/epersons", json_data=payload)
        return response.json()
    
    async def delete_eperson(self, uuid: str) -> None:
        """Delete an EPerson by UUID."""
        await self._request("DELETE", f"eperson/epersons/{uuid}")
    
    async def add_eperson_to_group(
        self,
        group_uuid: str,
        eperson_uuid: str,
    ) -> None:
        """
        Add an EPerson to a group.
        
        Uses POST /api/eperson/groups/{group_uuid}/epersons with text/uri-list.
        
        Args:
            group_uuid: UUID of the group
            eperson_uuid: UUID of the EPerson to add
        """
        self._require_auth("add_eperson_to_group")
        url = f"{self.base_url}/server/api/eperson/groups/{group_uuid}/epersons"
        
        try:
            response = await self.client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.jwt_token}",
                    "X-XSRF-TOKEN": self.csrf_token,
                    "Content-Type": "text/uri-list",
                },
                content=f"{self.base_url}/server/api/eperson/epersons/{eperson_uuid}",
            )
            
            if response.status_code >= 400:
                raise DSpaceAPIError(
                    f"Add EPerson to group failed with status {response.status_code}: {response.text}",
                    status_code=response.status_code,
                )
        
        except httpx.RequestError as e:
            raise DSpaceAPIError(f"Add EPerson to group request failed: {e}")
    
    # ========== EPerson Groups ==========
    
    async def create_group(self, name: str, description: Optional[str] = None) -> dict:
        """
        Create an EPerson group.
        
        Args:
            name: Group name
            description: Group description
        
        Returns:
            Created group object
        """
        metadata = {}
        if description:
            metadata["dc.description"] = [
                {"value": description, "language": None, "authority": None, "confidence": -1}
            ]
        
        payload = {
            "name": name,
            "metadata": metadata,
        }
        
        response = await self._request("POST", "eperson/groups", json_data=payload)
        return response.json()
    
    async def delete_group(self, uuid: str) -> None:
        """Delete a group by UUID."""
        await self._request("DELETE", f"eperson/groups/{uuid}")
    
    async def search_group_by_name(self, name: str) -> Optional[dict]:
        """
        Search for a group by name.
        
        Uses GET /api/eperson/groups/search/byMetadata?query={name}
        Searches in UUID and group name fields.
        
        Args:
            name: Group name to search for
        
        Returns:
            Group object if exact name match found, None otherwise
        """
        response = await self._request(
            "GET",
            "eperson/groups/search/byMetadata",
            params={"query": name}
        )
        
        data = response.json()
        groups = data.get("_embedded", {}).get("groups", [])
        
        # Look for exact name match
        for group in groups:
            if group.get("name") == name:
                return group
        
        return None
    
    async def find_or_create_group(self, name: str, description: Optional[str] = None) -> dict:
        """
        Find existing group by name, or create if it doesn't exist.
        
        This is useful for reusable groups that might persist across runs.
        
        Args:
            name: Group name
            description: Group description (used only if creating new group)
        
        Returns:
            Group object (existing or newly created)
        """
        # Search for existing group first
        existing = await self.search_group_by_name(name)
        if existing:
            return existing
        
        # Create new group if not found
        return await self.create_group(name, description)
    
    async def add_subgroup_to_group(
        self,
        parent_group_uuid: str,
        subgroup_uuid: str,
    ) -> None:
        """
        Add a subgroup to a parent group.
        
        Uses POST /api/eperson/groups/{parent_uuid}/subgroups with text/uri-list.
        This creates a group hierarchy where permissions inherit from subgroups.
        
        Args:
            parent_group_uuid: UUID of parent group
            subgroup_uuid: UUID of subgroup to add as member
        """
        self._require_auth("add_subgroup_to_group")
        url = f"{self.base_url}/server/api/eperson/groups/{parent_group_uuid}/subgroups"
        
        try:
            response = await self.client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.jwt_token}",
                    "X-XSRF-TOKEN": self.csrf_token,
                    "Content-Type": "text/uri-list",
                },
                content=f"{self.base_url}/server/api/eperson/groups/{subgroup_uuid}",
            )
            
            if response.status_code >= 400:
                raise DSpaceAPIError(
                    f"Add subgroup failed with status {response.status_code}: {response.text}",
                    status_code=response.status_code,
                )
        
        except httpx.RequestError as e:
            raise DSpaceAPIError(f"Add subgroup request failed: {e}")
    
    # ========== Collection Default Groups ==========
    
    async def create_collection_item_read_group(
        self,
        collection_uuid: str,
        description: Optional[str] = None,
    ) -> dict:
        """
        Create the default item READ group for a collection.
        
        This creates a collection-specific group that grants READ access to items.
        DSpace automatically names this group (you cannot specify the name).
        Items created in this collection will automatically inherit READ permissions
        from this group.
        
        Args:
            collection_uuid: UUID of the collection
            description: Optional description for the group
        
        Returns:
            Created group object
        """
        self._require_auth("create_collection_item_read_group")
        payload = {"metadata": {}}

        if description:
            payload["metadata"]["dc.description"] = [
                {
                    "value": description,
                    "language": None,
                    "authority": None,
                    "confidence": -1
                }
            ]

        url = f"{self.base_url}/server/api/core/collections/{collection_uuid}/itemReadGroup"
        
        try:
            response = await self.client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.jwt_token}",
                    "X-XSRF-TOKEN": self.csrf_token,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            
            if response.status_code >= 400:
                raise DSpaceAPIError(
                    f"Create collection item read group failed with status {response.status_code}: {response.text}",
                    status_code=response.status_code,
                )
            
            return response.json()
        
        except httpx.RequestError as e:
            raise DSpaceAPIError(f"Create collection item read group request failed: {e}")
    
    async def create_collection_bitstream_read_group(
        self,
        collection_uuid: str,
        description: Optional[str] = None,
    ) -> dict:
        """
        Create the default bitstream READ group for a collection.
        
        This creates a collection-specific group that grants READ access to bitstreams.
        DSpace automatically names this group (you cannot specify the name).
        Bitstreams in items created in this collection will automatically inherit
        READ permissions from this group.
        
        Args:
            collection_uuid: UUID of the collection
            description: Optional description for the group
        
        Returns:
            Created group object
        """
        self._require_auth("create_collection_bitstream_read_group")
        payload = {"metadata": {}}

        if description:
            payload["metadata"]["dc.description"] = [
                {
                    "value": description,
                    "language": None,
                    "authority": None,
                    "confidence": -1
                }
            ]

        url = f"{self.base_url}/server/api/core/collections/{collection_uuid}/bitstreamReadGroup"
        
        try:
            response = await self.client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.jwt_token}",
                    "X-XSRF-TOKEN": self.csrf_token,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            
            if response.status_code >= 400:
                raise DSpaceAPIError(
                    f"Create collection bitstream read group failed with status {response.status_code}: {response.text}",
                    status_code=response.status_code,
                )
            
            return response.json()
        
        except httpx.RequestError as e:
            raise DSpaceAPIError(f"Create collection bitstream read group request failed: {e}")
    
    # ========== Search and Discovery ==========
    
    async def search_items(
        self,
        query: Optional[str] = None,
        filters: Optional[dict] = None,
        sort: str = "dc.date.accessioned,desc",
        page: int = 0,
        size: int = 20,
        configuration: Optional[str] = None,
    ) -> dict:
        """
        Search for items using the discovery endpoint.
        
        Args:
            query: Search query string (Solr/Lucene syntax)
            filters: Dict of filter_name: (value, operator) pairs
                e.g., {"submitter": ("uuid-here", "authority")}
            sort: Sort parameter (e.g., "dc.date.accessioned,desc")
            page: Page number
            size: Page size (max 100)
            configuration: Discovery configuration name (e.g., "workflow")
        
        Returns:
            Search results with embedded items
        """
        params = {
            "dsoType": "item",
            "sort": sort,
            "page": page,
            "size": min(size, 100),
        }
        
        if query:
            params["query"] = query
        
        if configuration:
            params["configuration"] = configuration
        
        if filters:
            for filter_name, (value, operator) in filters.items():
                params[f"f.{filter_name}"] = f"{value},{operator}"
        
        response = await self._request("GET", "discover/search/objects", params=params)
        return response.json()
    
    async def get_item(self, uuid: str) -> dict:
        """Get full item details by UUID."""
        response = await self._request("GET", f"core/items/{uuid}")
        return response.json()

    async def resolve_pdf_format_id(
        self, override_format_id: Optional[int] = None
    ) -> Optional[int]:
        """
        Resolve the bitstream format id for PDF from the registry.

        Calls GET core/bitstreamformats and finds the format where shortDescription
        or mimetype indicates PDF (e.g. "application/pdf"). If override_format_id
        is set, returns that value without calling the API (useful when the
        registry shape differs or PDF id is known to be e.g. 3).

        Args:
            override_format_id: If set, return this id and skip registry lookup

        Returns:
            Format id for PDF, or None if not found
        """
        if override_format_id is not None:
            return override_format_id
        data = await self.get_bitstream_formats(page=0, size=200)
        formats = data.get("_embedded", {}).get("bitstreamformats", [])
        if not formats and "bitstreamformats" in data:
            formats = data["bitstreamformats"]
        for fmt in formats:
            desc = (fmt.get("shortDescription") or "").upper()
            mime = (fmt.get("mimetype") or "").lower()
            if "PDF" in desc or mime == "application/pdf":
                return fmt.get("id")
        return None

    async def count_items_with_bitstream_format(
        self,
        format_id: int,
        page_size: int = 100,
        delay_between_pages: float = 1.0,
        delay_between_items: float = 0.0,
        progress_callback: Optional[Callable[[int, int, Optional[int]], None]] = None,
        adaptive_delay: bool = False,
        adaptive_delay_config: Optional[AdaptiveDelayConfig] = None,
        debug_page_callback: Optional[
            Callable[[int, float, float, Optional[float]], None]
        ] = None,
        cache: Optional[RestPDFCountCache] = None,
        force_rerun: bool = False,
    ) -> dict:
        """
        Count items that have at least one bitstream with the given format id.

        Pages through discovery (UUIDs only), then for each item fetches only
        bundles and bitstreams (with format). Does not load full item metadata.
        Intended for moderate-sized repos (e.g. ~10k items); use delays to avoid
        straining the server.

        If cache is provided and force_rerun is False, items present in the cache
        are skipped (no API calls); their cached has_pdf is used. Assumes items
        are immutable after creation. Use force_rerun=True to re-check all items.

        Args:
            format_id: Bitstream format id (e.g. 3 for PDF in default DSpace)
            page_size: Number of item UUIDs per discovery page (max 100)
            delay_between_pages: Seconds to wait between discovery pages
            delay_between_items: Seconds to wait between processing each item
            cache: Optional RestPDFCountCache to skip already-known items
            force_rerun: If True, ignore cache and re-fetch all items; cache is
                still updated for future runs

        Returns:
            Dict with "count" (items with ≥1 bitstream of this format) and
            "total_items_processed" (total items considered).
        """
        count = 0
        total_processed = 0
        page = 0
        page_size = min(page_size, 100)
        total_known: Optional[int] = None

        delay_controller: Optional[AdaptiveDelayController] = None
        if adaptive_delay:
            cfg = adaptive_delay_config or AdaptiveDelayConfig()
            # If caller explicitly set a delay_between_pages, treat it as initial
            if delay_between_pages != 1.0:
                cfg.initial_delay = delay_between_pages
            delay_controller = AdaptiveDelayController(cfg)

        while True:
            # Inter-page delay
            if page > 0:
                if adaptive_delay and delay_controller is not None:
                    d = delay_controller.get_delay()
                    if d > 0:
                        await asyncio.sleep(d)
                elif delay_between_pages > 0:
                    await asyncio.sleep(delay_between_pages)

            page_start = time.perf_counter()
            try:
                results = await self.search_items(
                    query="*",
                    sort="dc.date.accessioned,desc",
                    page=page,
                    size=page_size,
                )
            except DSpaceAPIError as e:
                if adaptive_delay and delay_controller is not None:
                    page_duration = time.perf_counter() - page_start
                    status = "rate_limited" if "429" in str(e) else "error"
                    await delay_controller.record_result(page_duration, status=status)
                raise

            search_result = results.get("_embedded", {}).get("searchResult", {})
            objects = search_result.get("_embedded", {}).get("objects", [])
            if page == 0 and search_result and total_known is None:
                page_info = search_result.get("page", {})
                if isinstance(page_info, dict):
                    total_known = page_info.get("totalElements")
            if not objects:
                page_duration = time.perf_counter() - page_start
                current_delay = (
                    delay_controller.current_delay
                    if adaptive_delay and delay_controller is not None
                    else delay_between_pages
                )
                if debug_page_callback:
                    debug_page_callback(
                        page, page_duration, float(current_delay), self.courtesy_delay
                    )
                if adaptive_delay and delay_controller is not None:
                    await delay_controller.record_result(page_duration, status="ok")
                break
            for obj in objects:
                indexable = obj.get("_embedded", {}).get("indexableObject", {})
                item_uuid = indexable.get("uuid")
                if not item_uuid:
                    continue
                if cache is not None and not force_rerun:
                    cached_has_pdf = cache.get(item_uuid)
                    if cached_has_pdf is not None:
                        total_processed += 1
                        if cached_has_pdf:
                            count += 1
                        if progress_callback:
                            progress_callback(total_processed, count, total_known)
                        continue
                if delay_between_items > 0 and total_processed > 0:
                    await asyncio.sleep(delay_between_items)
                total_processed += 1
                item_has_format = False
                try:
                    bundles_data = await self.get_item_bundles(item_uuid)
                except DSpaceAPIError:
                    continue
                bundles = bundles_data.get("bundles", [])
                if not bundles:
                    bundles = bundles_data.get("_embedded", {}).get("bundles", [])
                for bundle in bundles:
                    if item_has_format:
                        break
                    bundle_uuid = bundle.get("uuid")
                    if not bundle_uuid:
                        continue
                    try:
                        bitstreams_data = await self.get_bundle_bitstreams(
                            bundle_uuid, embed_format=True
                        )
                    except DSpaceAPIError:
                        try:
                            bitstreams_data = await self.get_bundle_bitstreams(
                                bundle_uuid, embed_format=False
                            )
                        except DSpaceAPIError:
                            continue
                    bitstreams = bitstreams_data.get("_embedded", {}).get(
                        "bitstreams", []
                    )
                    if not bitstreams:
                        bitstreams = bitstreams_data.get("bitstreams", [])
                    for bs in bitstreams:
                        fmt = bs.get("_embedded", {}).get("format") or bs.get("format")
                        if fmt is not None and fmt.get("id") == format_id:
                            item_has_format = True
                            break
                    if item_has_format:
                        break
                    for bs in bitstreams:
                        bs_uuid = bs.get("uuid")
                        if not bs_uuid:
                            continue
                        try:
                            fmt = await self.get_bitstream_format(bs_uuid)
                            if fmt.get("id") == format_id:
                                item_has_format = True
                                break
                        except DSpaceAPIError:
                            pass
                    if item_has_format:
                        break
                if item_has_format:
                    count += 1
                if cache is not None:
                    cache.update(item_uuid, item_has_format)
                if progress_callback:
                    progress_callback(total_processed, count, total_known)
            page_duration = time.perf_counter() - page_start
            current_delay = (
                delay_controller.current_delay
                if adaptive_delay and delay_controller is not None
                else delay_between_pages
            )
            if debug_page_callback:
                debug_page_callback(
                    page, page_duration, float(current_delay), self.courtesy_delay
                )
            if adaptive_delay and delay_controller is not None:
                await delay_controller.record_result(page_duration, status="ok")
            if len(objects) < page_size:
                break
            page += 1

        return {"count": count, "total_items_processed": total_processed}

    async def count_items_with_pdf_bitstream(
        self,
        pdf_format_id: Optional[int] = None,
        page_size: int = 100,
        delay_between_pages: float = 1.0,
        delay_between_items: float = 0.0,
        progress_callback: Optional[Callable[[int, int, Optional[int]], None]] = None,
        adaptive_delay: bool = False,
        adaptive_delay_config: Optional[AdaptiveDelayConfig] = None,
        debug_page_callback: Optional[
            Callable[[int, float, float, Optional[float]], None]
        ] = None,
        cache: Optional[RestPDFCountCache] = None,
        force_rerun: bool = False,
    ) -> dict:
        """
        Count items that have at least one PDF bitstream.

        Resolves the PDF format id from the bitstream format registry (or uses
        pdf_format_id if provided), then calls count_items_with_bitstream_format.

        Args:
            pdf_format_id: If set, use this as the PDF format id (e.g. 3); otherwise
                resolve from GET core/bitstreamformats
            page_size: Number of item UUIDs per discovery page
            delay_between_pages: Seconds between discovery pages
            delay_between_items: Seconds between items
            cache: Optional RestPDFCountCache to skip already-known items
            force_rerun: If True, ignore cache and re-check all items

        Returns:
            Dict with "count", "total_items_processed", and "pdf_format_id" (id used).
        """
        resolved_id = await self.resolve_pdf_format_id(
            override_format_id=pdf_format_id
        )
        if resolved_id is None:
            return {
                "count": 0,
                "total_items_processed": 0,
                "pdf_format_id": None,
            }
        result = await self.count_items_with_bitstream_format(
            format_id=resolved_id,
            page_size=page_size,
            delay_between_pages=delay_between_pages,
            delay_between_items=delay_between_items,
            progress_callback=progress_callback,
            adaptive_delay=adaptive_delay,
            adaptive_delay_config=adaptive_delay_config,
            debug_page_callback=debug_page_callback,
            cache=cache,
            force_rerun=force_rerun,
        )
        result["pdf_format_id"] = resolved_id
        return result

    async def patch_item(self, uuid: str, operations: list) -> dict:
        """
        Update item with JSON Patch operations (RFC 6902).

        Args:
            uuid: Item UUID
            operations: List of patch operations, e.g. [{"op": "replace", "path": "/metadata/dc.contributor.author/0", "value": {...}}]

        Returns:
            Updated item object
        """
        response = await self._request(
            "PATCH",
            f"core/items/{uuid}",
            json_data=operations,  # type: ignore[arg-type]
        )
        return response.json()

    async def get_vocabulary_entries(
        self,
        vocabulary_name: str,
        filter_term: Optional[str] = None,
        exact: bool = False,
        page: int = 0,
        size: int = 20,
        entry_id: Optional[str] = None,
    ) -> dict:
        """
        Get entries from a controlled vocabulary (e.g. local author authority).

        Use for local authority lookup: vocabularies like CacheableAuthorAuthority
        or SolrAuthorAuthority return entries from this repository's SOLR authority
        core only (not the public ORCID registry).

        Args:
            vocabulary_name: e.g. "CacheableAuthorAuthority" or "SolrAuthorAuthority"
            filter_term: Terms to filter entries (mandatory unless vocabulary is scrollable or entry_id is set)
            exact: If True, return only entries that match the filter exactly
            page: Page number
            size: Page size
            entry_id: Get entries for a specific vocabulary entry ID (alternative to filter_term)

        Returns:
            JSON with _embedded.entries (each entry may have authority, display, value, _links.vocabularyEntryDetail)
        """
        params: dict = {"page": page, "size": size}
        if filter_term is not None:
            params["filter"] = filter_term
            params["exact"] = exact
        if entry_id is not None:
            params["entryID"] = entry_id
        response = await self._request(
            "GET",
            f"submission/vocabularies/{vocabulary_name}/entries",
            params=params,
        )
        return response.json()

    async def get_vocabulary_entry_detail(self, vocabulary_name: str, entry_id: str) -> Optional[dict]:
        """
        Get detailed info for a vocabulary entry (e.g. for ORCID display).

        Args:
            vocabulary_name: e.g. "CacheableAuthorAuthority"
            entry_id: Entry ID (e.g. authority UUID)

        Returns:
            JSON detail or None if not found / not available
        """
        try:
            response = await self._request(
                "GET",
                f"submission/vocabularyEntryDetails/{vocabulary_name}:{entry_id}",
            )
            return response.json()
        except Exception:
            return None

    async def get_eperson(self, uuid: str) -> dict:
        """Get EPerson details by UUID."""
        response = await self._request("GET", f"eperson/epersons/{uuid}")
        return response.json()
    
    async def verify_server_version(self, raise_on_mismatch: bool = True) -> Optional[str]:
        """
        Verify that the connected server version is compatible with target_versions.
        
        This method should be called after client initialization to ensure the server
        version matches the declared target versions. It will:
        - Detect the server version (config/properties/dspace.version, then root API, then actuator/info)
        - Compare against target_versions
        - Raise ServerVersionMismatchError for major version mismatches (if raise_on_mismatch=True)
        - Print warnings for minor version differences
        
        Args:
            raise_on_mismatch: If True, raise exception on major version mismatch.
                             If False, return None on mismatch and only print warnings.
        
        Returns:
            Warning message string if minor version difference, None otherwise
        
        Raises:
            ServerVersionMismatchError: If major version mismatch and raise_on_mismatch=True
        """
        from .version import VersionCompatibility

        targets_str = ", ".join(self.target_versions)
        console.print("[bold cyan]Server version check[/bold cyan]")
        console.print(
            f"[dim]Declared target version(s): [white]{targets_str}[/white]. "
            "Detecting the live server version (may query several REST endpoints; "
            "this can take a few seconds on slow networks).[/dim]"
        )

        server_version = await self.detect_dspace_version()
        
        if server_version is None:
            console.print("[yellow]⚠[/yellow]  Could not detect server version. Version validation skipped.")
            return None
        
        is_compatible, warning_msg = VersionCompatibility.check_server_version_compatibility(
            server_version,
            self.target_versions
        )
        
        if not is_compatible:
            error_msg = (
                f"Server version {server_version} is not compatible with target version(s) "
                f"{', '.join(self.target_versions)}. Major version mismatch detected."
            )
            if raise_on_mismatch:
                raise ServerVersionMismatchError(
                    server_version=server_version,
                    target_versions=self.target_versions,
                    message=error_msg
                )
            else:
                console.print(f"[red]Error:[/red] {error_msg}")
                return None
        
        if warning_msg:
            console.print(f"[yellow]⚠[/yellow]  {warning_msg}")
            return warning_msg
        
        # Exact match or compatible version
        target_versions_str = ", ".join(self.target_versions)
        console.print(f"[green]✓[/green]  Server version {server_version} is compatible with target version(s) {target_versions_str}")
        return None
    
    def _normalize_version(self, version_str: Optional[str]) -> Optional[str]:
        """Parse version string to major.minor (e.g. 9.0.1 -> 9.0). Returns None if invalid.

        This is deliberately tolerant of prefixes/suffixes like ``\"DSpace 7.6\"`` by
        extracting the first ``major.minor`` pattern it finds.
        """
        if not version_str or not isinstance(version_str, str):
            return None

        version_str = version_str.strip()

        # First, try to extract a major.minor pattern from the string.
        # Examples:
        # - "DSpace 7.6" -> "7.6"
        # - "7.6.1" -> "7.6"
        # - "Version 9.0.1" -> "9.0"
        match = re.search(r"(\d+)\.(\d+)", version_str)
        if match:
            try:
                major, minor = int(match.group(1)), int(match.group(2))
                return f"{major}.{minor}"
            except ValueError:
                # Regex guarantees digits, but be defensive
                return f"{match.group(1)}.{match.group(2)}"

        # Fallback: previous behavior for already clean strings or edge cases
        parts = version_str.split(".")
        if len(parts) >= 2:
            try:
                major, minor = int(parts[0]), int(parts[1])
                return f"{major}.{minor}"
            except ValueError:
                return f"{parts[0]}.{parts[1]}" if parts[0] and parts[1] else None
        return version_str if version_str else None

    def _parse_version_from_json(self, data: dict) -> Optional[str]:
        """Extract version from various JSON shapes (root API, actuator, etc.). Returns normalized version or None."""
        if not data or not isinstance(data, dict):
            return None
        # Direct keys
        for key in ("version", "dspaceVersion", "dspace_version"):
            v = data.get(key)
            if isinstance(v, str) and v:
                return self._normalize_version(v)
            if isinstance(v, dict) and "version" in v:
                return self._normalize_version(str(v["version"]))
        # Nested: dspace.version, info.build.version (actuator), config.version
        dspace = data.get("dspace") or data.get("config") or {}
        if isinstance(dspace, dict):
            v = dspace.get("version")
            if isinstance(v, str) and v:
                return self._normalize_version(v)
        info = data.get("info") or data.get("build") or {}
        if isinstance(info, dict):
            v = info.get("version")
            if isinstance(v, str) and v:
                return self._normalize_version(v)
        return None

    @property
    def last_detected_server_version(self) -> Optional[str]:
        """Major.minor string from the last :meth:`detect_dspace_version` run, or ``None``."""
        return self._last_detected_server_version

    def _set_last_detected_version(self, value: Optional[str]) -> Optional[str]:
        self._last_detected_server_version = value
        return value

    async def detect_dspace_version(self) -> Optional[str]:
        """
        Detect the actual DSpace server version.

        Tries in order: (1) GET /api/config/properties/dspace.version (single-property,
        per REST contract; main config/properties returns 405), (2) GET /server/api root
        HAL, (3) GET /actuator/info. Returns major.minor (e.g. 7.6, 9.0) or None.

        Updates :attr:`last_detected_server_version` so callers can reuse the result after
        :meth:`verify_server_version` without probing the server again.
        """
        self._last_detected_server_version = None
        headers = self._get_headers(include_csrf=False)
        last_error: Optional[str] = None

        # 1. Single-property endpoint (per docs/dspace-rest-api/7.6/configuration.md)
        try:
            console.print("[dim]  → Probing [white]GET …/config/properties/dspace.version[/white][/dim]")
            url = f"{self.base_url}/server/api/config/properties/dspace.version"
            response = await self.client.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                values = data.get("values") if isinstance(data, dict) else None
                if values and len(values) > 0 and values[0]:
                    normalized = self._normalize_version(str(values[0]))
                    if normalized:
                        return self._set_last_detected_version(normalized)
            elif response.status_code == 401:
                try:
                    console.print(
                        "[dim]  → Same endpoint without auth (401 from server; retry unauthenticated)…[/dim]"
                    )
                    async with httpx.AsyncClient(timeout=self.timeout) as unauthenticated_client:
                        r = await unauthenticated_client.get(url)
                        if r.status_code == 200:
                            data = r.json()
                            values = data.get("values") if isinstance(data, dict) else None
                            if values and len(values) > 0 and values[0]:
                                normalized = self._normalize_version(str(values[0]))
                                if normalized:
                                    return self._set_last_detected_version(normalized)
                except Exception:
                    pass
            last_error = f"config/properties/dspace.version (status: {response.status_code})"
        except Exception as e:
            last_error = f"config/properties/dspace.version: {e}"

        # 2. Root API (GET /server/api) – often exposes version in HAL
        try:
            console.print("[dim]  → Probing [white]GET …/server/api[/white] (root HAL)[/dim]")
            root_url = f"{self.base_url}/server/api"
            response = await self.client.get(root_url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                normalized = self._parse_version_from_json(data)
                if normalized:
                    return self._set_last_detected_version(normalized)
            if not last_error:
                last_error = f"root API (status: {response.status_code})"
        except Exception as e:
            if not last_error:
                last_error = f"root API: {e}"

        # 3. Actuator info (admin-only on some setups)
        try:
            console.print("[dim]  → Probing [white]GET …/actuator/info[/white][/dim]")
            actuator_url = f"{self.base_url}/server/actuator/info"
            response = await self.client.get(actuator_url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                normalized = self._parse_version_from_json(data)
                if normalized:
                    return self._set_last_detected_version(normalized)
            if not last_error:
                last_error = f"actuator/info (status: {response.status_code})"
        except Exception as e:
            if not last_error:
                last_error = f"actuator/info: {e}"

        console.print(f"[dim]Warning: Could not detect server version ({last_error}). Version validation skipped.[/dim]")
        return self._set_last_detected_version(None)
    
    async def get_item_submitter(self, item_uuid: str) -> Optional[dict]:
        """
        Get the submitter (EPerson) for a specific item.
        
        Note: This endpoint only exists in DSpace 9+. For DSpace 7, this will return None.
        Use the embed parameter or search workspaceitems to find submitter in DSpace 7.
        
        Args:
            item_uuid: UUID of the item
        
        Returns:
            EPerson object of the submitter, or None if not available
        """
        try:
            # Make request directly without using _request to avoid version validation
            # The submitter endpoint doesn't exist in DSpace 7
            url = f"{self.base_url}/server/api/core/items/{item_uuid}/submitter"
            headers = self._get_headers(include_csrf=False)
            
            response = await self.client.get(url, headers=headers)
            
            if response.status_code == 404:
                # Endpoint doesn't exist (DSpace 7)
                return None
            elif response.status_code == 204:
                # No content - no read access or not authenticated
                return None
            elif response.status_code == 200:
                return response.json()
            else:
                # Other error - log and return None
                console.print(f"[dim]Warning: submitter endpoint returned {response.status_code}[/dim]")
                return None
        except Exception:
            return None
