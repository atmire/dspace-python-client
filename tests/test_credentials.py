"""Tests for interactive credential helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from dspace_client.auth import DSpaceAuthClient
from dspace_client.credentials import prompt_and_authenticate
from dspace_client.exceptions import AuthenticationError


@pytest.mark.asyncio
async def test_prompt_and_authenticate_succeeds_on_second_attempt():
    auth = DSpaceAuthClient("https://demo.dspace.org")
    auth.authenticate = AsyncMock(
        side_effect=[
            AuthenticationError("Invalid username or password.", status_code=401),
            ("jwt-token", {"authenticated": True}),
        ]
    )

    with patch("dspace_client.credentials.getpass.getpass", return_value="right-pass"):
        jwt, status = await prompt_and_authenticate(
            auth, "admin@example.com", "wrong-pass", console=None
        )

    assert jwt == "jwt-token"
    assert status["authenticated"] is True
    assert auth.authenticate.await_count == 2
    auth.authenticate.assert_any_await("admin@example.com", "wrong-pass")
    auth.authenticate.assert_awaited_with("admin@example.com", "right-pass")


@pytest.mark.asyncio
async def test_prompt_and_authenticate_fails_after_three_wrong_passwords():
    auth = DSpaceAuthClient("https://demo.dspace.org")
    auth.authenticate = AsyncMock(
        side_effect=AuthenticationError("Invalid username or password.", status_code=401)
    )

    with patch(
        "dspace_client.credentials.getpass.getpass",
        side_effect=["bad2", "bad3"],
    ):
        with pytest.raises(AuthenticationError, match="after 3 attempts"):
            await prompt_and_authenticate(
                auth, "admin@example.com", "bad1", console=None
            )

    assert auth.authenticate.await_count == 3


@pytest.mark.asyncio
async def test_prompt_and_authenticate_does_not_retry_csrf_errors():
    auth = DSpaceAuthClient("https://demo.dspace.org")
    auth.authenticate = AsyncMock(
        side_effect=AuthenticationError("Failed to get CSRF token: boom")
    )

    with pytest.raises(AuthenticationError, match="CSRF"):
        await prompt_and_authenticate(auth, "admin@example.com", "pass", console=None)

    auth.authenticate.assert_awaited_once()
