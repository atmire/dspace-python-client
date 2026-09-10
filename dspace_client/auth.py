"""Authentication client for DSpace REST API."""

import inspect
import logging
import os
import time

import httpx
from rich.console import Console

from .exceptions import AuthenticationError
from .promo import show_atmire_promo_end

console = Console()
logger = logging.getLogger(__name__)

_CSRF_COOKIE_NAME = "DSPACE-XSRF-COOKIE"
_BODY_PREVIEW_LEN = 200


def _body_preview(text: object, limit: int = _BODY_PREVIEW_LEN) -> str:
    if text is None or not isinstance(text, str):
        return ""
    if not text:
        return ""
    t = text.strip().replace("\n", " ")
    if len(t) > limit:
        return t[:limit] + "..."
    return t


class DSpaceAuthClient:
    """Handles DSpace authentication flow: CSRF → Login → JWT."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        """
        Initialize auth client.

        Args:
            base_url: DSpace server base URL (e.g., https://demo.dspace.org)
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.csrf_token: str | None = None
        self.csrf_cookie: str | None = None
        self.jwt_token: str | None = None
        # Use a persistent client to maintain cookies across requests
        self.client: httpx.AsyncClient | None = None
        # Track session timing for proactive refresh
        self._last_auth_time: float | None = None
        # Max session age before we proactively refresh, in seconds
        # Default ~25 minutes; configurable via env for tuning.
        self.max_session_age_seconds: float = float(
            os.environ.get("DSPACE_SESSION_MAX_AGE_SECONDS", str(25 * 60))
        )
        self.show_atmire_promo: bool = False

    @property
    def _http(self) -> httpx.AsyncClient:
        """The live HTTP client. Call :meth:`_ensure_client` before using it."""
        if self.client is None:
            msg = "HTTP client not started; call _ensure_client() first."
            raise AuthenticationError(msg)
        return self.client

    async def _ensure_client(self) -> None:
        """
        Ensure we have a persistent HTTP client.

        CRITICAL: We must use ONE client for the entire auth flow because:
        - DSpace sets DSPACE-XSRF-COOKIE during CSRF request
        - httpx stores cookies in the client instance
        - New client = lost cookies = 403 errors

        See API_GOTCHAS.md for details.
        """
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)

    def _csrf_token_from_cookie_jar(self) -> str | None:
        """Read CSRF value from jar when proxies strip DSPACE-XSRF-TOKEN header."""
        if not self.client:
            return None
        try:
            val = self.client.cookies.get(_CSRF_COOKIE_NAME)
        except (AttributeError, TypeError, ValueError):
            return None
        if val is None or inspect.isawaitable(val) or not isinstance(val, str):
            return None
        return val or None

    def _apply_csrf_from_response(self, response: httpx.Response) -> None:
        """Update stored CSRF if server sent a new dspace-xsrf-token (login / JWT refresh)."""
        new_csrf = response.headers.get("dspace-xsrf-token")
        if new_csrf and new_csrf != self.csrf_token:
            self.csrf_token = new_csrf
            self.csrf_cookie = new_csrf

    def _log_csrf_fetch_failure(
        self,
        response: httpx.Response,
        method: str,
        url: str,
        cookie_in_jar: bool,
    ) -> None:
        logger.warning(
            "get_csrf_token failed: method=%s url=%s status=%s content_type=%s "
            "has_dspace_xsrf_token_header=%s dspace_xsrf_cookie_in_jar=%s body_preview=%r",
            method,
            url,
            response.status_code,
            response.headers.get("content-type", ""),
            bool(response.headers.get("dspace-xsrf-token")),
            cookie_in_jar,
            _body_preview(response.text),
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("get_csrf_token response header keys: %s", list(response.headers.keys()))

    def _log_jwt_refresh_failure(self, response: httpx.Response) -> None:
        logger.warning(
            "refresh_jwt failed: status=%s content_type=%s has_dspace_xsrf_token=%s "
            "has_authorization=%s body_preview=%r",
            response.status_code,
            response.headers.get("content-type", ""),
            bool(response.headers.get("dspace-xsrf-token")),
            bool(response.headers.get("Authorization")),
            _body_preview(response.text),
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("refresh_jwt response header keys: %s", list(response.headers.keys()))

    def _log_login_failure(self, response: httpx.Response) -> None:
        logger.warning(
            "login failed: status=%s content_type=%s has_dspace_xsrf_token=%s "
            "has_authorization=%s body_preview=%r",
            response.status_code,
            response.headers.get("content-type", ""),
            bool(response.headers.get("dspace-xsrf-token")),
            bool(response.headers.get("Authorization")),
            _body_preview(response.text),
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("login response header keys: %s", list(response.headers.keys()))

    def _log_verify_failure(self, response: httpx.Response) -> None:
        logger.warning(
            "verify_authentication failed: status=%s content_type=%s body_preview=%r",
            response.status_code,
            response.headers.get("content-type", ""),
            _body_preview(response.text),
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        had_client = self.client is not None
        if self.client:
            await self.client.aclose()
            self.client = None
        if had_client and self._last_auth_time is not None and self.show_atmire_promo:
            show_atmire_promo_end()

    async def verify_server(self) -> bool:
        """
        Verify that the DSpace server is reachable.

        Returns:
            True if server responds, False otherwise
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.head(f"{self.base_url}/server/api")
                return response.status_code in (200, 302, 404)  # 404 is OK, means API exists
        except (httpx.RequestError, httpx.HTTPStatusError):
            return False

    async def get_csrf_token(self) -> tuple[str, str]:
        """
        Step 1: Get CSRF token from DSpace.

        According to CSRF documentation:
        - Server sends token in DSPACE-XSRF-TOKEN header
        - Server sets DSPACE-XSRF-COOKIE cookie
        - Client must send token in X-XSRF-TOKEN header for modifying requests
        - Server compares header value with cookie value

        Returns:
            Tuple of (csrf_token, csrf_cookie_value)

        Raises:
            AuthenticationError: If CSRF token cannot be obtained
        """
        csrf_url = f"{self.base_url}/server/api/security/csrf"
        try:
            await self._ensure_client()

            # Try HEAD first; some servers/proxies only return dspace-xsrf-token on GET
            response = await self._http.head(csrf_url)
            csrf_token = response.headers.get("dspace-xsrf-token")
            if not csrf_token:
                csrf_token = self._csrf_token_from_cookie_jar()
            if not csrf_token:
                response = await self._http.get(csrf_url)
                csrf_token = response.headers.get("dspace-xsrf-token")
            if not csrf_token:
                csrf_token = self._csrf_token_from_cookie_jar()
            if not csrf_token:
                self._log_csrf_fetch_failure(
                    response,
                    "GET",
                    csrf_url,
                    bool(self._csrf_token_from_cookie_jar()),
                )
                console.print("[red]Available headers:[/red]")
                for key, value in response.headers.items():
                    console.print(f"  {key}: {value}")
                raise AuthenticationError("CSRF token not found in response headers")

            self.csrf_token = csrf_token
            self.csrf_cookie = csrf_token
            return csrf_token, csrf_token

        except httpx.RequestError as e:
            raise AuthenticationError(f"Failed to get CSRF token: {e}")

    async def refresh_jwt(self) -> str:
        """
        Refresh JWT using existing credentials (authentication.md: POST /authn/login
        with Authorization Bearer + X-XSRF-TOKEN, no form body).
        """
        if not self.jwt_token or not self.csrf_token:
            raise AuthenticationError("Cannot refresh JWT: missing jwt_token or csrf_token")

        await self._ensure_client()
        try:
            response = await self._http.post(
                f"{self.base_url}/server/api/authn/login",
                headers={
                    "Authorization": f"Bearer {self.jwt_token}",
                    "X-XSRF-TOKEN": self.csrf_token,
                },
            )
        except httpx.RequestError as e:
            logger.warning("refresh_jwt request error: %s", e)
            raise AuthenticationError(f"JWT refresh request failed: {e}") from e

        if response.status_code != 200:
            self._log_jwt_refresh_failure(response)
            raise AuthenticationError(
                f"JWT refresh failed with status {response.status_code}: {response.text}"
            )

        self._apply_csrf_from_response(response)

        auth_header = response.headers.get("Authorization")
        if not auth_header:
            self._log_jwt_refresh_failure(response)
            console.print("[red]No Authorization header in JWT refresh response![/red]")
            console.print("[red]Available headers:[/red]")
            for key, value in response.headers.items():
                console.print(f"  {key}: {value}")
            raise AuthenticationError("JWT token not found in response after refresh")

        if not auth_header.startswith("Bearer "):
            raise AuthenticationError(f"Invalid Authorization header format: {auth_header}")

        jwt_token = auth_header.replace("Bearer ", "")
        self.jwt_token = jwt_token
        self._last_auth_time = time.time()
        result: str = jwt_token
        return result

    async def login(self, username: str, password: str) -> str:
        """
        Step 2: Login with username and password to get JWT.

        According to authentication.md:
        - POST to /api/authn/login with form-encoded user/password
        - Must include X-XSRF-TOKEN header and DSPACE-XSRF-COOKIE cookie
        - Returns JWT in Authorization header

        Args:
            username: DSpace admin username
            password: DSpace admin password

        Returns:
            JWT bearer token

        Raises:
            AuthenticationError: If login fails
        """
        if not self.csrf_token or not self.csrf_cookie:
            await self.get_csrf_token()

        try:
            await self._ensure_client()

            # Omit X-XSRF-TOKEN rather than sending None: httpx rejects a None header
            # value with a TypeError that gives no hint that CSRF retrieval failed.
            login_headers = {"Content-Type": "application/x-www-form-urlencoded"}
            if self.csrf_token:
                login_headers["X-XSRF-TOKEN"] = self.csrf_token

            response = await self._http.post(
                f"{self.base_url}/server/api/authn/login",
                data={
                    "user": username,
                    "password": password,
                },
                headers=login_headers,
            )

            if response.status_code == 200:
                console.print(f"[dim]← Status: {response.status_code}[/dim]")

            if response.status_code != 200:
                self._log_login_failure(response)
                if response.status_code in (401, 403):
                    raise AuthenticationError(
                        "Invalid username or password.",
                        status_code=response.status_code,
                    )
                raise AuthenticationError(
                    f"Login failed with status {response.status_code}.",
                    status_code=response.status_code,
                )

            self._apply_csrf_from_response(response)

            auth_header = response.headers.get("Authorization")
            if not auth_header:
                self._log_login_failure(response)
                console.print("[red]No Authorization header in response![/red]")
                console.print("[red]Available headers:[/red]")
                for key, value in response.headers.items():
                    console.print(f"  {key}: {value}")
                raise AuthenticationError("JWT token not found in response")

            if not auth_header.startswith("Bearer "):
                raise AuthenticationError(f"Invalid Authorization header format: {auth_header}")

            jwt_token = auth_header.replace("Bearer ", "")

            self.jwt_token = jwt_token
            self._last_auth_time = time.time()
            result: str = jwt_token
            return result

        except httpx.RequestError as e:
            raise AuthenticationError(f"Login request failed: {e}")

    async def verify_authentication(self) -> dict:
        """
        Step 3: Verify authentication status.

        Returns:
            Status information from DSpace

        Raises:
            AuthenticationError: If verification fails
        """
        if not self.jwt_token:
            raise AuthenticationError("No JWT token available. Please login first.")

        try:
            await self._ensure_client()

            response = await self._http.get(
                f"{self.base_url}/server/api/authn/status",
                headers={
                    "Authorization": f"Bearer {self.jwt_token}",
                },
            )

            if response.status_code != 200:
                self._log_verify_failure(response)
                console.print("[red]Verification failed![/red]")
                console.print(f"[red]Response:[/red] {response.text[:500]}")
                raise AuthenticationError(
                    f"Authentication verification failed: {response.status_code}"
                )

            status = response.json()

            if not status.get("authenticated"):
                logger.warning(
                    "verify_authentication: status 200 but authenticated=false payload_keys=%s",
                    list(status.keys()) if isinstance(status, dict) else type(status).__name__,
                )
                console.print("[red]Status response indicates not authenticated:[/red]")
                console.print(f"  {status}")
                raise AuthenticationError("Authentication verification failed: not authenticated")

            result: dict = status
            return result

        except httpx.RequestError as e:
            raise AuthenticationError(f"Authentication verification request failed: {e}")

    async def authenticate(self, username: str, password: str) -> tuple[str, dict]:
        """
        Complete authentication flow: CSRF → Login → Verify.

        Args:
            username: DSpace admin username
            password: DSpace admin password

        Returns:
            Tuple of (jwt_token, status_info)

        Raises:
            AuthenticationError: If any step fails
        """
        await self.get_csrf_token()
        jwt_token = await self.login(username, password)
        status = await self.verify_authentication()
        return jwt_token, status

    async def ensure_session(self, username: str, password: str, force: bool = False) -> str:
        """
        Ensure there is a valid, reasonably fresh authenticated session.

        This is designed for long-running jobs:
        - Uses a configurable max session age (default ~25 minutes).
        - Can be forced to re-authenticate unconditionally (e.g. after a 401).

        Args:
            username: DSpace admin username
            password: DSpace admin password
            force: If True, always re-authenticate, ignoring cached state.

        Returns:
            Current JWT token (possibly refreshed)
        """
        if force or not self.jwt_token or self._last_auth_time is None:
            jwt, _ = await self.authenticate(username, password)
            return jwt

        now = time.time()
        age = now - self._last_auth_time
        if age >= self.max_session_age_seconds:
            console.print(
                f"[dim]Refreshing DSpace session (age {int(age)}s ≥ "
                f"max {int(self.max_session_age_seconds)}s)[/dim]"
            )
            if self.csrf_token:
                try:
                    return await self.refresh_jwt()
                except AuthenticationError as e:
                    logger.warning(
                        "JWT refresh failed; falling back to full authenticate: %s",
                        e,
                    )
            jwt, _ = await self.authenticate(username, password)
            return jwt

        return self.jwt_token

    async def is_session_valid(self) -> bool:
        """
        Check if the current session (JWT token) is still valid.

        Returns:
            True if session is valid, False if expired or invalid
        """
        if not self.jwt_token:
            return False

        try:
            await self._ensure_client()

            response = await self._http.get(
                f"{self.base_url}/server/api/authn/status",
                headers={
                    "Authorization": f"Bearer {self.jwt_token}",
                },
            )

            if response.status_code != 200:
                return False

            status = response.json()
            result: bool = status.get("authenticated", False)
            return result

        except (httpx.RequestError, ValueError, KeyError):
            return False
