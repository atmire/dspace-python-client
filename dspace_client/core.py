"""Core DSpace REST API client for CRUD operations with version validation."""

import httpx
import orjson
from typing import Any, Optional, Union, List
from pathlib import Path
from rich.console import Console
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    retry_if_result,
)

from .exceptions import DSpaceAPIError, VersionIncompatibilityError
from .version import VersionCompatibility
from .docs import RestContractFetcher

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
        jwt_token: str,
        csrf_token: str,
        http_client: httpx.AsyncClient,
        target_versions: Union[str, List[str]] = "bleeding-edge",
        timeout: float = 30.0,
        max_retries: int = 3,
        courtesy_delay: float = 1.0,
    ):
        """
        Initialize DSpace API client with version compatibility checking.
        
        IMPORTANT: This client MUST receive the authenticated HTTP client from
        DSpaceAuthClient. Do NOT create a new HTTP client here because:
        - DSpace cookies must persist (DSPACE-XSRF-COOKIE)
        - Creating new client = losing cookies = 403 errors
        
        Args:
            base_url: DSpace server base URL
            jwt_token: JWT bearer token from authentication
            csrf_token: CSRF token for modifying requests (refreshed after login!)
            http_client: Authenticated HTTP client with cookies from auth flow
            target_versions: DSpace version(s) to validate compatibility against.
                This does NOT restrict which DSpace server you can connect to.
                Instead, it ensures all operations work in the specified version(s).
                
                - "bleeding-edge" (default): Latest main branch from RestContract
                - "7.0", "8.0", "9.0": Specific stable versions
                - ["7.6", "8.0", "9.0"]: Multiple versions (validates against ALL)
                
                When multiple versions are specified, operations must work in ALL
                of them. If an operation is not supported in any target version,
                a VersionIncompatibilityError is raised before the API call.
            timeout: Request timeout in seconds
            max_retries: Maximum number of retries for failed requests
            courtesy_delay: Delay in seconds between API calls (0 for no delay)
        
        On initialization:
        1. Validates target_versions parameter
        2. Checks if git repos exist for target version(s)
        3. If not cloned, git clone RestContract repository
        4. If exists, git fetch latest changes (if older than 24h)
        5. Checkout correct branch/tag for each version
        6. Loads compatibility rules for validation
        7. Sets up automatic update checking
        """
        self.base_url = base_url.rstrip("/")
        self.jwt_token = jwt_token
        self.csrf_token = csrf_token
        self.client = http_client  # ⚠️ CRITICAL: Reuse authenticated client
        self.timeout = timeout
        self.max_retries = max_retries
        self.courtesy_delay = courtesy_delay
        self._last_request_time = 0.0
        
        # Initialize version compatibility system
        self.validator = VersionCompatibility(target_versions)
        self.docs_fetcher = RestContractFetcher()
        
        # Fetch documentation for target versions (this will be async in real implementation)
        # For now, we'll assume docs are fetched during initialization
        console.print(f"[dim]Initializing DSpace client for versions: {target_versions}[/dim]")
    
    def _get_headers(self, include_csrf: bool = False) -> dict[str, str]:
        """
        Get standard headers for API requests.
        
        Args:
            include_csrf: Whether to include X-XSRF-TOKEN header
        """
        headers = {
            "Authorization": f"Bearer {self.jwt_token}",
            "Content-Type": "application/json",
        }
        if include_csrf:
            headers["X-XSRF-TOKEN"] = self.csrf_token
        return headers
    
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
            DSpaceAPIError: If request fails
            VersionIncompatibilityError: If operation not supported in target versions
        """
        # STEP 1: Validate operation against target versions
        self.validator.validate_before_call(
            method_name=f"{method.lower()}_{endpoint.split('/')[-1]}",
            endpoint=endpoint,
            operation=method
        )
        
        # STEP 2: Apply courtesy delay
        if self.courtesy_delay > 0:
            import time
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
                    f"{method} {url} failed with status {response.status_code}: {error_detail}"
                )
            
            # Update last request time for courtesy delay
            import time
            self._last_request_time = time.time()
            return response
        
        except httpx.RequestError as e:
            raise DSpaceAPIError(f"Request failed: {e}")
    
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
                    f"Bitstream upload failed with status {response.status_code}: {response.text}"
                )
            
            return response.json()
        
        except httpx.RequestError as e:
            raise DSpaceAPIError(f"Bitstream upload request failed: {e}")
    
    async def delete_bitstream(self, uuid: str) -> None:
        """Delete a bitstream by UUID."""
        await self._request("DELETE", f"core/bitstreams/{uuid}")
    
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
                    f"Add EPerson to group failed with status {response.status_code}: {response.text}"
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
                    f"Add subgroup failed with status {response.status_code}: {response.text}"
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
                    f"Create collection item read group failed with status {response.status_code}: {response.text}"
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
                    f"Create collection bitstream read group failed with status {response.status_code}: {response.text}"
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
    
    async def get_eperson(self, uuid: str) -> dict:
        """Get EPerson details by UUID."""
        response = await self._request("GET", f"eperson/epersons/{uuid}")
        return response.json()
    
    async def detect_dspace_version(self) -> Optional[str]:
        """
        Detect the DSpace version by testing API capabilities.
        
        Returns:
            DSpace version string (e.g., "7.6", "9.0") or None if detection fails
        """
        # Try to get a sample item to test capabilities
        try:
            # First, try to search for any items
            results = await self.search_items(query="*", size=1, page=0)
            items = results.get("_embedded", {}).get("searchResult", {}).get("_embedded", {}).get("objects", [])
            
            if not items:
                return None
            
            # Get the first item UUID
            item_uuid = items[0]["_embedded"]["indexableObject"]["uuid"]
            
            # Try the submitter endpoint - only exists in DSpace 9+
            url = f"{self.base_url}/server/api/core/items/{item_uuid}/submitter"
            headers = self._get_headers(include_csrf=False)
            response = await self.client.get(url, headers=headers)
            
            if response.status_code == 404:
                # Endpoint doesn't exist - likely DSpace 7
                return "7.6"  # Return latest 7.x
            elif response.status_code in (200, 204):
                # Endpoint exists - likely DSpace 9+
                return "9.0"
            else:
                return None
                
        except Exception as e:
            console.print(f"[dim]Warning: Could not detect DSpace version: {e}[/dim]")
            return None
    
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
