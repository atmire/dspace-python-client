"""Test configuration and fixtures."""

import os

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
import httpx

from dspace_client import DSpaceAuthClient, DSpaceClient


@pytest.fixture(autouse=True)
def _disable_atmire_promo():
    """Avoid Atmire marketing panels during tests."""
    key = "DSPACE_CLIENT_DISABLE_ATMIRE_PROMO"
    previous = os.environ.get(key)
    os.environ[key] = "1"
    yield
    if previous is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = previous


@pytest.fixture
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_http_client():
    """Mock HTTP client for testing."""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.cookies = MagicMock()
    client.cookies.jar = []
    return client


@pytest.fixture
def mock_auth_client():
    """Mock authentication client."""
    auth = AsyncMock(spec=DSpaceAuthClient)
    auth.base_url = "https://demo.dspace.org"
    auth.csrf_token = "mock-csrf-token"
    auth.jwt_token = "mock-jwt-token"
    auth.client = AsyncMock(spec=httpx.AsyncClient)
    return auth


@pytest.fixture
def mock_dspace_client(mock_auth_client):
    """Mock DSpace client."""
    client = DSpaceClient(
        base_url="https://demo.dspace.org",
        jwt_token="mock-jwt-token",
        csrf_token="mock-csrf-token",
        http_client=mock_auth_client.client,
        target_versions="bleeding-edge",
        courtesy_delay=0.0  # No delay for testing
    )
    return client


@pytest.fixture
def sample_community_data():
    """Sample community data for testing."""
    return {
        "uuid": "12345678-1234-1234-1234-123456789012",
        "name": "Test Community",
        "metadata": {
            "dc.title": [{"value": "Test Community", "language": None, "authority": None, "confidence": -1}]
        }
    }


@pytest.fixture
def sample_collection_data():
    """Sample collection data for testing."""
    return {
        "uuid": "87654321-4321-4321-4321-210987654321",
        "name": "Test Collection",
        "metadata": {
            "dc.title": [{"value": "Test Collection", "language": None, "authority": None, "confidence": -1}]
        }
    }


@pytest.fixture
def sample_item_data():
    """Sample item data for testing."""
    return {
        "uuid": "11111111-2222-3333-4444-555555555555",
        "name": "Test Item",
        "metadata": {
            "dc.title": [{"value": "Test Item", "language": None, "authority": None, "confidence": -1}]
        }
    }
