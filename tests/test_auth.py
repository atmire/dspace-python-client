"""Tests for authentication client."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from dspace_client import DSpaceAuthClient
from dspace_client.exceptions import AuthenticationError


class TestDSpaceAuthClient:
    """Test DSpaceAuthClient functionality."""

    @pytest.mark.asyncio
    async def test_init(self):
        """Test client initialization."""
        auth = DSpaceAuthClient("https://demo.dspace.org")
        assert auth.base_url == "https://demo.dspace.org"
        assert auth.timeout == 30.0
        assert auth.csrf_token is None
        assert auth.jwt_token is None
        assert auth.client is None

    @pytest.mark.asyncio
    async def test_verify_server_success(self):
        """Test successful server verification."""
        auth = DSpaceAuthClient("https://demo.dspace.org")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_client.head.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await auth.verify_server()
            assert result is True

    @pytest.mark.asyncio
    async def test_verify_server_failure(self):
        """Test failed server verification."""
        auth = DSpaceAuthClient("https://demo.dspace.org")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.head.side_effect = httpx.RequestError("Connection failed")
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await auth.verify_server()
            assert result is False

    @pytest.mark.asyncio
    async def test_get_csrf_token_success(self):
        """Test successful CSRF token retrieval."""
        auth = DSpaceAuthClient("https://demo.dspace.org")

        # Mock the client and response
        mock_client = AsyncMock()
        mock_client.cookies = MagicMock()
        mock_client.cookies.get.return_value = None
        mock_response = AsyncMock()
        mock_response.headers = {"dspace-xsrf-token": "test-csrf-token"}
        mock_client.head.return_value = mock_response
        auth.client = mock_client

        token, cookie = await auth.get_csrf_token()

        assert token == "test-csrf-token"
        assert cookie == "test-csrf-token"
        assert auth.csrf_token == "test-csrf-token"
        assert auth.csrf_cookie == "test-csrf-token"

    @pytest.mark.asyncio
    async def test_get_csrf_token_missing_header(self):
        """Test CSRF token retrieval when header is missing from both HEAD and GET."""
        auth = DSpaceAuthClient("https://demo.dspace.org")

        # Mock the client: HEAD and GET both return no token (fallback also fails)
        mock_client = AsyncMock()
        mock_client.cookies = MagicMock()
        mock_client.cookies.get.return_value = None
        mock_response = AsyncMock()
        mock_response.headers = {}
        mock_response.text = ""
        mock_client.head.return_value = mock_response
        mock_client.get.return_value = mock_response
        auth.client = mock_client

        with pytest.raises(AuthenticationError, match="CSRF token not found"):
            await auth.get_csrf_token()

    @pytest.mark.asyncio
    async def test_get_csrf_token_get_fallback(self):
        """Test that GET is used when HEAD does not return the token."""
        auth = DSpaceAuthClient("https://demo.dspace.org")

        mock_client = AsyncMock()
        mock_client.cookies = MagicMock()
        mock_client.cookies.get.return_value = None
        head_response = AsyncMock()
        head_response.headers = {}  # No token on HEAD
        get_response = AsyncMock()
        get_response.headers = {"dspace-xsrf-token": "token-from-get"}
        mock_client.head.return_value = head_response
        mock_client.get.return_value = get_response
        auth.client = mock_client

        token, cookie = await auth.get_csrf_token()

        assert token == "token-from-get"
        assert cookie == "token-from-get"
        mock_client.head.assert_called_once()
        mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_csrf_token_cookie_jar_fallback(self):
        """Use DSPACE-XSRF-COOKIE from jar when response omits dspace-xsrf-token header."""
        auth = DSpaceAuthClient("https://demo.dspace.org")
        mock_client = AsyncMock()
        mock_cookies = MagicMock()
        mock_cookies.get.side_effect = [None, "token-from-jar"]
        mock_client.cookies = mock_cookies
        mock_response = AsyncMock()
        mock_response.headers = {}
        mock_response.text = ""
        mock_client.head.return_value = mock_response
        mock_client.get.return_value = mock_response
        auth.client = mock_client

        token, cookie = await auth.get_csrf_token()

        assert token == "token-from-jar"
        assert cookie == "token-from-jar"

    @pytest.mark.asyncio
    async def test_refresh_jwt_success(self):
        """JWT refresh POST returns new bearer token."""
        auth = DSpaceAuthClient("https://demo.dspace.org")
        auth.jwt_token = "old-jwt"
        auth.csrf_token = "csrf-val"
        mock_client = AsyncMock()
        mock_client.cookies = MagicMock()
        mock_client.cookies.get.return_value = None
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Authorization": "Bearer refreshed-jwt"}
        mock_response.text = ""
        mock_client.post.return_value = mock_response
        auth.client = mock_client

        jwt = await auth.refresh_jwt()

        assert jwt == "refreshed-jwt"
        assert auth.jwt_token == "refreshed-jwt"
        mock_client.post.assert_called_once()
        args, kwargs = mock_client.post.call_args
        assert "authn/login" in args[0]
        assert kwargs["headers"]["Authorization"] == "Bearer old-jwt"
        assert kwargs["headers"]["X-XSRF-TOKEN"] == "csrf-val"
        assert "data" not in kwargs and not kwargs.get("data")

    @pytest.mark.asyncio
    async def test_refresh_jwt_requires_tokens(self):
        auth = DSpaceAuthClient("https://demo.dspace.org")
        auth.jwt_token = None
        auth.csrf_token = "c"
        with pytest.raises(AuthenticationError, match="missing jwt_token"):
            await auth.refresh_jwt()

    @pytest.mark.asyncio
    async def test_refresh_jwt_non_200_raises(self):
        auth = DSpaceAuthClient("https://demo.dspace.org")
        auth.jwt_token = "j"
        auth.csrf_token = "c"
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.headers = {}
        mock_response.text = "forbidden"
        mock_client.post.return_value = mock_response
        auth.client = mock_client

        with pytest.raises(AuthenticationError, match="JWT refresh failed"):
            await auth.refresh_jwt()

    @pytest.mark.asyncio
    async def test_ensure_session_stale_uses_refresh_jwt(self):
        auth = DSpaceAuthClient("https://demo.dspace.org")
        auth.jwt_token = "old"
        auth.csrf_token = "csrf"
        auth._last_auth_time = 0.0
        auth.max_session_age_seconds = 60.0

        with (
            patch.object(auth, "authenticate", new_callable=AsyncMock) as mock_auth,
            patch.object(auth, "refresh_jwt", new_callable=AsyncMock) as mock_refresh,
        ):
            mock_refresh.return_value = "refreshed"
            jwt = await auth.ensure_session("user", "pass")
            assert jwt == "refreshed"
            mock_refresh.assert_called_once()
            mock_auth.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_session_refresh_failure_falls_back_to_authenticate(self):
        auth = DSpaceAuthClient("https://demo.dspace.org")
        auth.jwt_token = "old"
        auth.csrf_token = "csrf"
        auth._last_auth_time = 0.0
        auth.max_session_age_seconds = 60.0

        with (
            patch.object(auth, "authenticate", new_callable=AsyncMock) as mock_auth,
            patch.object(auth, "refresh_jwt", new_callable=AsyncMock) as mock_refresh,
        ):
            mock_refresh.side_effect = AuthenticationError("refresh failed")
            mock_auth.return_value = ("from-full-auth", {})
            jwt = await auth.ensure_session("user", "pass")
            assert jwt == "from-full-auth"
            mock_refresh.assert_called_once()
            mock_auth.assert_called_once_with("user", "pass")

    @pytest.mark.asyncio
    async def test_ensure_session_stale_without_csrf_skips_refresh(self):
        auth = DSpaceAuthClient("https://demo.dspace.org")
        auth.jwt_token = "old"
        auth.csrf_token = None
        auth._last_auth_time = 0.0
        auth.max_session_age_seconds = 60.0

        with patch.object(auth, "authenticate", new_callable=AsyncMock) as mock_auth:
            mock_auth.return_value = ("full", {})
            jwt = await auth.ensure_session("user", "pass")
            assert jwt == "full"
            mock_auth.assert_called_once_with("user", "pass")

    @pytest.mark.asyncio
    async def test_login_success(self):
        """Test successful login."""
        auth = DSpaceAuthClient("https://demo.dspace.org")
        auth.csrf_token = "test-csrf-token"
        auth.csrf_cookie = "test-csrf-token"

        # Mock the client and response
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"Authorization": "Bearer test-jwt-token"}
        mock_client.post.return_value = mock_response
        auth.client = mock_client

        jwt_token = await auth.login("testuser", "testpass")

        assert jwt_token == "test-jwt-token"
        assert auth.jwt_token == "test-jwt-token"

    @pytest.mark.asyncio
    async def test_login_failure(self):
        """Test failed login."""
        auth = DSpaceAuthClient("https://demo.dspace.org")
        auth.csrf_token = "test-csrf-token"
        auth.csrf_cookie = "test-csrf-token"

        # Mock the client and response
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 401
        mock_response.text = "Invalid credentials"
        mock_response.headers = {"Content-Type": "text/plain"}
        mock_client.post.return_value = mock_response
        auth.client = mock_client

        with pytest.raises(AuthenticationError, match="Invalid username or password") as exc_info:
            await auth.login("testuser", "wrongpass")

        assert exc_info.value.status_code == 401
        assert exc_info.value.credential_failure is True

    @pytest.mark.asyncio
    async def test_verify_authentication_success(self):
        """Test successful authentication verification."""
        auth = DSpaceAuthClient("https://demo.dspace.org")
        auth.jwt_token = "test-jwt-token"

        # Mock the client and response
        mock_client = AsyncMock()
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"authenticated": True, "eperson": {"name": "Test User"}}
        mock_client.get.return_value = mock_response
        auth.client = mock_client

        status = await auth.verify_authentication()

        assert status["authenticated"] is True
        assert status["eperson"]["name"] == "Test User"

    @pytest.mark.asyncio
    async def test_verify_authentication_failure(self):
        """Test failed authentication verification."""
        auth = DSpaceAuthClient("https://demo.dspace.org")
        auth.jwt_token = "test-jwt-token"

        # Mock the client and response
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"
        mock_client.get.return_value = mock_response
        auth.client = mock_client

        with pytest.raises(AuthenticationError, match="Authentication verification failed"):
            await auth.verify_authentication()

    @pytest.mark.asyncio
    async def test_authenticate_complete_flow(self):
        """Test complete authentication flow."""
        auth = DSpaceAuthClient("https://demo.dspace.org")

        # Mock all the methods
        with (
            patch.object(auth, "get_csrf_token") as mock_csrf,
            patch.object(auth, "login") as mock_login,
            patch.object(auth, "verify_authentication") as mock_verify,
        ):
            mock_csrf.return_value = ("test-csrf-token", "test-csrf-token")
            mock_login.return_value = "test-jwt-token"
            mock_verify.return_value = {"authenticated": True}

            jwt, status = await auth.authenticate("testuser", "testpass")

            assert jwt == "test-jwt-token"
            assert status["authenticated"] is True
            mock_csrf.assert_called_once()
            mock_login.assert_called_once_with("testuser", "testpass")
            mock_verify.assert_called_once()

    @pytest.mark.asyncio
    async def test_is_session_valid_true(self):
        """Test session validity check when session is valid."""
        auth = DSpaceAuthClient("https://demo.dspace.org")
        auth.jwt_token = "test-jwt-token"

        # Mock the client and response
        mock_client = AsyncMock()
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"authenticated": True}
        mock_client.get.return_value = mock_response
        auth.client = mock_client

        result = await auth.is_session_valid()
        assert result is True

    @pytest.mark.asyncio
    async def test_is_session_valid_false(self):
        """Test session validity check when session is invalid."""
        auth = DSpaceAuthClient("https://demo.dspace.org")
        auth.jwt_token = "test-jwt-token"

        # Mock the client and response
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 401
        mock_client.get.return_value = mock_response
        auth.client = mock_client

        result = await auth.is_session_valid()
        assert result is False

    @pytest.mark.asyncio
    async def test_close(self):
        """Test client close."""
        auth = DSpaceAuthClient("https://demo.dspace.org")
        auth.client = AsyncMock()

        await auth.close()

        assert auth.client is None
