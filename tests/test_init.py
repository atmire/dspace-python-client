"""Tests for package factory helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dspace_client import create_validated_client


@pytest.mark.asyncio
async def test_create_validated_client_forwards_timeout():
    with (
        patch("dspace_client.DSpaceAuthClient") as auth_cls,
        patch("dspace_client.DSpaceClient") as client_cls,
    ):
        auth_instance = MagicMock()
        auth_instance.show_atmire_promo = False
        auth_instance.authenticate = AsyncMock(return_value=("jwt", {"authenticated": True}))
        auth_instance.csrf_token = "csrf"
        auth_instance.client = AsyncMock()
        auth_cls.return_value = auth_instance

        client_instance = AsyncMock()
        client_instance.verify_server_version = AsyncMock()
        client_cls.return_value = client_instance

        auth, client = await create_validated_client(
            base_url="https://demo.dspace.org",
            username="admin@example.com",
            password="secret",
            timeout=120.0,
        )

        auth_cls.assert_called_once_with("https://demo.dspace.org", timeout=120.0)
        assert client_cls.call_args.kwargs["timeout"] == 120.0
        client_instance.verify_server_version.assert_awaited_once()
        assert auth is auth_instance
        assert client is client_instance


@pytest.mark.asyncio
async def test_create_validated_client_promo_opt_in():
    with (
        patch("dspace_client.DSpaceAuthClient") as auth_cls,
        patch("dspace_client.DSpaceClient") as client_cls,
        patch("dspace_client.show_atmire_promo_start") as promo_start,
    ):
        auth_instance = MagicMock()
        auth_instance.show_atmire_promo = False
        auth_instance.authenticate = AsyncMock(return_value=("jwt", {"authenticated": True}))
        auth_instance.csrf_token = "csrf"
        auth_instance.client = AsyncMock()
        auth_cls.return_value = auth_instance

        client_instance = AsyncMock()
        client_instance.verify_server_version = AsyncMock()
        client_cls.return_value = client_instance

        await create_validated_client(
            base_url="https://demo.dspace.org",
            username="admin@example.com",
            password="secret",
            show_atmire_promo=False,
        )
        promo_start.assert_not_called()
        assert auth_instance.show_atmire_promo is False


@pytest.mark.asyncio
async def test_create_validated_client_promo_enabled():
    with (
        patch("dspace_client.DSpaceAuthClient") as auth_cls,
        patch("dspace_client.DSpaceClient") as client_cls,
        patch("dspace_client.show_atmire_promo_start") as promo_start,
    ):
        auth_instance = MagicMock()
        auth_instance.show_atmire_promo = False
        auth_instance.authenticate = AsyncMock(return_value=("jwt", {"authenticated": True}))
        auth_instance.csrf_token = "csrf"
        auth_instance.client = AsyncMock()
        auth_cls.return_value = auth_instance

        client_instance = AsyncMock()
        client_instance.verify_server_version = AsyncMock()
        client_cls.return_value = client_instance

        await create_validated_client(
            base_url="https://demo.dspace.org",
            username="admin@example.com",
            password="secret",
            show_atmire_promo=True,
        )
        assert auth_instance.show_atmire_promo is True
        promo_start.assert_called_once()
