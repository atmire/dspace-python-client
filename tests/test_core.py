"""Tests for core DSpace client."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from dspace_client import DSpaceClient
from dspace_client.exceptions import (
    AuthenticationError,
    DSpaceAPIError,
    VersionIncompatibilityError,
)
from dspace_client.rest_pdf_cache import RestPDFCountCache


class TestDSpaceClient:
    """Test DSpaceClient functionality."""
    
    @pytest.mark.asyncio
    async def test_init(self, mock_http_client):
        """Test client initialization."""
        client = DSpaceClient(
            base_url="https://demo.dspace.org",
            jwt_token="test-jwt-token",
            csrf_token="test-csrf-token",
            http_client=mock_http_client,
            target_versions="bleeding-edge"
        )
        
        assert client.base_url == "https://demo.dspace.org"
        assert client.jwt_token == "test-jwt-token"
        assert client.csrf_token == "test-csrf-token"
        assert client.client == mock_http_client
        assert client.timeout == 30.0
        assert client.max_retries == 3
        assert client.validator is not None
        assert client.docs_fetcher is not None
    
    @pytest.mark.asyncio
    async def test_get_headers_without_csrf(self, mock_dspace_client):
        """Test getting headers without CSRF token."""
        headers = mock_dspace_client._get_headers(include_csrf=False)
        
        expected = {
            "Authorization": "Bearer mock-jwt-token",
            "Content-Type": "application/json"
        }
        assert headers == expected
    
    @pytest.mark.asyncio
    async def test_get_headers_with_csrf(self, mock_dspace_client):
        """Test getting headers with CSRF token."""
        headers = mock_dspace_client._get_headers(include_csrf=True)

        expected = {
            "Authorization": "Bearer mock-jwt-token",
            "Content-Type": "application/json",
            "X-XSRF-TOKEN": "mock-csrf-token"
        }
        assert headers == expected

    @pytest.mark.asyncio
    async def test_get_headers_anonymous_omits_authorization(self, mock_http_client):
        """Anonymous clients (no JWT) must not emit the Authorization header."""
        client = DSpaceClient(
            base_url="https://demo.dspace.org",
            jwt_token=None,
            csrf_token=None,
            http_client=mock_http_client,
            target_versions="bleeding-edge",
            courtesy_delay=0.0,
        )

        headers = client._get_headers(include_csrf=False)
        assert "Authorization" not in headers
        assert headers == {"Content-Type": "application/json"}

        # Even when CSRF is requested, an absent token leaves the header off.
        headers_csrf = client._get_headers(include_csrf=True)
        assert "X-XSRF-TOKEN" not in headers_csrf

    @pytest.mark.asyncio
    async def test_request_blocks_mutators_when_anonymous(self, mock_http_client):
        """Mutating verbs raise AuthenticationError before dispatch on anonymous clients."""
        client = DSpaceClient(
            base_url="https://demo.dspace.org",
            jwt_token=None,
            csrf_token=None,
            http_client=mock_http_client,
            target_versions="bleeding-edge",
            courtesy_delay=0.0,
        )

        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with pytest.raises(AuthenticationError, match="anonymous mode"):
                await client._request(method, "core/communities")

        # No HTTP call should have been attempted.
        mock_http_client.request.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_request_success(self, mock_dspace_client):
        """Test successful request."""
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"uuid": "test-uuid", "name": "Test"}
        mock_dspace_client.client.request.return_value = mock_response
        
        response = await mock_dspace_client._request("GET", "core/communities")
        
        assert response == mock_response
        mock_dspace_client.client.request.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_request_error(self, mock_dspace_client):
        """Test request with error response."""
        mock_response = AsyncMock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"
        mock_response.headers = {"Content-Type": "application/json"}
        mock_dspace_client.client.request.return_value = mock_response
        
        with pytest.raises(DSpaceAPIError, match="GET.*failed with status 400"):
            await mock_dspace_client._request("GET", "core/communities")
    
    @pytest.mark.asyncio
    async def test_create_community_success(self, mock_dspace_client, sample_community_data):
        """Test successful community creation."""
        # Create a mock response object with synchronous json() method
        from unittest.mock import MagicMock
        mock_response = MagicMock()
        mock_response.json.return_value = sample_community_data
        mock_dspace_client._request = AsyncMock(return_value=mock_response)
        
        result = await mock_dspace_client.create_community("Test Community")
        
        assert result == sample_community_data
        mock_dspace_client._request.assert_called_once_with(
            "POST", "core/communities", json_data={
                "name": "Test Community",
                "metadata": {
                    "dc.title": [{"value": "Test Community", "language": None, "authority": None, "confidence": -1}]
                }
            }
        )
    
    @pytest.mark.asyncio
    async def test_create_community_with_metadata(self, mock_dspace_client, sample_community_data):
        """Test community creation with custom metadata."""
        custom_metadata = {
            "dc.title": [{"value": "Custom Title", "language": None, "authority": None, "confidence": -1}],
            "dc.description": [{"value": "Custom Description", "language": None, "authority": None, "confidence": -1}]
        }
        
        from unittest.mock import MagicMock
        mock_response = MagicMock()
        mock_response.json.return_value = sample_community_data
        mock_dspace_client._request = AsyncMock(return_value=mock_response)
        
        result = await mock_dspace_client.create_community(
            "Test Community",
            metadata=custom_metadata
        )
        
        assert result == sample_community_data
        mock_dspace_client._request.assert_called_once_with(
            "POST", "core/communities", json_data={
                "name": "Test Community",
                "metadata": custom_metadata
            }
        )
    
    @pytest.mark.asyncio
    async def test_create_community_with_parent(self, mock_dspace_client, sample_community_data):
        """Test community creation with parent."""
        from unittest.mock import MagicMock
        mock_response = MagicMock()
        mock_response.json.return_value = sample_community_data
        mock_dspace_client._request = AsyncMock(return_value=mock_response)
        
        result = await mock_dspace_client.create_community(
            "Test Subcommunity",
            parent_uuid="parent-uuid"
        )
        
        assert result == sample_community_data
        mock_dspace_client._request.assert_called_once_with(
            "POST", "core/communities?parent=parent-uuid", json_data={
                "name": "Test Subcommunity",
                "metadata": {
                    "dc.title": [{"value": "Test Subcommunity", "language": None, "authority": None, "confidence": -1}]
                }
            }
        )
    
    @pytest.mark.asyncio
    async def test_delete_community(self, mock_dspace_client):
        """Test community deletion."""
        mock_dspace_client._request = AsyncMock()
        
        await mock_dspace_client.delete_community("test-uuid")
        
        mock_dspace_client._request.assert_called_once_with(
            "DELETE", "core/communities/test-uuid"
        )
    
    @pytest.mark.asyncio
    async def test_create_collection_success(self, mock_dspace_client, sample_collection_data):
        """Test successful collection creation."""
        from unittest.mock import MagicMock
        mock_response = MagicMock()
        mock_response.json.return_value = sample_collection_data
        mock_dspace_client._request = AsyncMock(return_value=mock_response)
        
        result = await mock_dspace_client.create_collection(
            "Test Collection",
            "parent-community-uuid"
        )
        
        assert result == sample_collection_data
        mock_dspace_client._request.assert_called_once_with(
            "POST", "core/collections?parent=parent-community-uuid", json_data={
                "name": "Test Collection",
                "metadata": {
                    "dc.title": [{"value": "Test Collection", "language": None, "authority": None, "confidence": -1}]
                }
            }
        )
    
    @pytest.mark.asyncio
    async def test_create_item_success(self, mock_dspace_client, sample_item_data):
        """Test successful item creation."""
        from unittest.mock import MagicMock
        mock_response = MagicMock()
        mock_response.json.return_value = sample_item_data
        mock_dspace_client._request = AsyncMock(return_value=mock_response)
        
        result = await mock_dspace_client.create_item(
            "Test Item",
            "owning-collection-uuid"
        )
        
        assert result == sample_item_data
        mock_dspace_client._request.assert_called_once_with(
            "POST", "core/items?owningCollection=owning-collection-uuid", json_data={
                "name": "Test Item",
                "metadata": {
                    "dc.title": [{"value": "Test Item", "language": None, "authority": None, "confidence": -1}]
                },
                "inArchive": True,
                "discoverable": True,
                "withdrawn": False,
                "type": "item"
            }
        )
    
    @pytest.mark.asyncio
    async def test_create_bundle_success(self, mock_dspace_client):
        """Test successful bundle creation."""
        bundle_data = {"uuid": "bundle-uuid", "name": "ORIGINAL"}
        from unittest.mock import MagicMock
        mock_response = MagicMock()
        mock_response.json.return_value = bundle_data
        mock_dspace_client._request = AsyncMock(return_value=mock_response)
        
        result = await mock_dspace_client.create_bundle("item-uuid", "ORIGINAL")
        
        assert result == bundle_data
        mock_dspace_client._request.assert_called_once_with(
            "POST", "core/items/item-uuid/bundles", json_data={
                "name": "ORIGINAL",
                "metadata": {}
            }
        )
    
    @pytest.mark.asyncio
    async def test_upload_bitstream_success(self, mock_dspace_client):
        """Test successful bitstream upload."""
        bitstream_data = {"uuid": "bitstream-uuid", "name": "test.txt"}
        from unittest.mock import MagicMock
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = bitstream_data
        mock_dspace_client.client.post.return_value = mock_response
        
        result = await mock_dspace_client.upload_bitstream(
            "bundle-uuid",
            "test.txt",
            b"test content"
        )
        
        assert result == bitstream_data
        mock_dspace_client.client.post.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_upload_bitstream_error(self, mock_dspace_client):
        """Test bitstream upload with error."""
        mock_response = AsyncMock()
        mock_response.status_code = 400
        mock_response.text = "Upload failed"
        mock_dspace_client.client.post.return_value = mock_response
        
        with pytest.raises(DSpaceAPIError, match="Bitstream upload failed"):
            await mock_dspace_client.upload_bitstream(
                "bundle-uuid",
                "test.txt",
                b"test content"
            )
    
    @pytest.mark.asyncio
    async def test_create_eperson_success(self, mock_dspace_client):
        """Test successful EPerson creation."""
        eperson_data = {"uuid": "eperson-uuid", "email": "test@example.com"}
        from unittest.mock import MagicMock
        mock_response = MagicMock()
        mock_response.json.return_value = eperson_data
        mock_dspace_client._request = AsyncMock(return_value=mock_response)
        
        result = await mock_dspace_client.create_eperson(
            "test@example.com",
            "John",
            "Doe"
        )
        
        assert result == eperson_data
        mock_dspace_client._request.assert_called_once_with(
            "POST", "eperson/epersons", json_data={
                "email": "test@example.com",
                "metadata": {
                    "eperson.firstname": [{"value": "John", "language": None, "authority": None, "confidence": -1}],
                    "eperson.lastname": [{"value": "Doe", "language": None, "authority": None, "confidence": -1}]
                },
                "canLogIn": True,
                "requireCertificate": False,
                "type": "eperson"
            }
        )
    
    @pytest.mark.asyncio
    async def test_create_group_success(self, mock_dspace_client):
        """Test successful group creation."""
        group_data = {"uuid": "group-uuid", "name": "Test Group"}
        from unittest.mock import MagicMock
        mock_response = MagicMock()
        mock_response.json.return_value = group_data
        mock_dspace_client._request = AsyncMock(return_value=mock_response)
        
        result = await mock_dspace_client.create_group("Test Group", "Test Description")
        
        assert result == group_data
        mock_dspace_client._request.assert_called_once_with(
            "POST", "eperson/groups", json_data={
                "name": "Test Group",
                "metadata": {
                    "dc.description": [{"value": "Test Description", "language": None, "authority": None, "confidence": -1}]
                }
            }
        )

    # ----- Item bundles, bitstreams, formats, PDF count -----

    @pytest.mark.asyncio
    async def test_get_item_bundles_success(self, mock_dspace_client):
        """Test get_item_bundles returns bundles list."""
        bundles_data = {"bundles": [{"uuid": "bundle-uuid-1", "name": "ORIGINAL"}]}
        mock_response = MagicMock()
        mock_response.json.return_value = bundles_data
        mock_dspace_client._request = AsyncMock(return_value=mock_response)
        result = await mock_dspace_client.get_item_bundles("item-uuid-123")
        assert result == bundles_data
        mock_dspace_client._request.assert_called_once_with(
            "GET", "core/items/item-uuid-123/bundles"
        )

    @pytest.mark.asyncio
    async def test_get_bundle_bitstreams_with_embed(self, mock_dspace_client):
        """Test get_bundle_bitstreams with embed_format=True."""
        bitstreams_data = {
            "_embedded": {
                "bitstreams": [
                    {"uuid": "bs-1", "_embedded": {"format": {"id": 3, "shortDescription": "PDF"}}}
                ]
            }
        }
        mock_response = MagicMock()
        mock_response.json.return_value = bitstreams_data
        mock_dspace_client._request = AsyncMock(return_value=mock_response)
        result = await mock_dspace_client.get_bundle_bitstreams("bundle-uuid", embed_format=True)
        assert result == bitstreams_data
        mock_dspace_client._request.assert_called_once_with(
            "GET", "core/bundles/bundle-uuid/bitstreams", params={"embed": "format"}
        )

    @pytest.mark.asyncio
    async def test_get_bitstream_format_success(self, mock_dspace_client):
        """Test get_bitstream_format returns format object."""
        format_data = {"id": 3, "shortDescription": "PDF", "mimetype": "application/pdf"}
        mock_response = MagicMock()
        mock_response.json.return_value = format_data
        mock_dspace_client._request = AsyncMock(return_value=mock_response)
        result = await mock_dspace_client.get_bitstream_format("bitstream-uuid")
        assert result == format_data
        mock_dspace_client._request.assert_called_once_with(
            "GET", "core/bitstreams/bitstream-uuid/format"
        )

    @pytest.mark.asyncio
    async def test_get_bitstream_formats_success(self, mock_dspace_client):
        """Test get_bitstream_formats returns paginated formats."""
        formats_data = {
            "_embedded": {
                "bitstreamformats": [
                    {"id": 3, "shortDescription": "PDF", "mimetype": "application/pdf"}
                ]
            }
        }
        mock_response = MagicMock()
        mock_response.json.return_value = formats_data
        mock_dspace_client._request = AsyncMock(return_value=mock_response)
        result = await mock_dspace_client.get_bitstream_formats(page=0, size=100)
        assert result == formats_data
        mock_dspace_client._request.assert_called_once_with(
            "GET", "core/bitstreamformats", params={"page": 0, "size": 100}
        )

    @pytest.mark.asyncio
    async def test_resolve_pdf_format_id_override(self, mock_dspace_client):
        """Test resolve_pdf_format_id with override returns override."""
        result = await mock_dspace_client.resolve_pdf_format_id(override_format_id=3)
        assert result == 3

    @pytest.mark.asyncio
    async def test_resolve_pdf_format_id_from_registry(self, mock_dspace_client):
        """Test resolve_pdf_format_id resolves from registry."""
        formats_data = {
            "_embedded": {
                "bitstreamformats": [
                    {"id": 5, "shortDescription": "XML"},
                    {"id": 3, "shortDescription": "PDF", "mimetype": "application/pdf"},
                ]
            }
        }
        mock_dspace_client.get_bitstream_formats = AsyncMock(return_value=formats_data)
        result = await mock_dspace_client.resolve_pdf_format_id(override_format_id=None)
        assert result == 3

    @pytest.mark.asyncio
    async def test_count_items_with_bitstream_format(self, mock_dspace_client):
        """Test count_items_with_bitstream_format returns count and total."""
        # Page 1: two items
        search_page1 = {
            "_embedded": {
                "searchResult": {
                    "_embedded": {
                        "objects": [
                            {"_embedded": {"indexableObject": {"uuid": "item-1"}}},
                            {"_embedded": {"indexableObject": {"uuid": "item-2"}}},
                        ]
                    }
                }
            }
        }
        # Page 2: empty (stop)
        search_page2 = {"_embedded": {"searchResult": {"_embedded": {"objects": []}}}}
        mock_dspace_client.search_items = AsyncMock(side_effect=[search_page1, search_page2])
        mock_dspace_client.get_item_bundles = AsyncMock(
            return_value={"bundles": [{"uuid": "bundle-1"}]}
        )
        # Item 1: one bitstream with format id 3 (PDF)
        # Item 2: no PDF
        bitstreams_with_pdf = {
            "_embedded": {
                "bitstreams": [
                    {"uuid": "bs-1", "_embedded": {"format": {"id": 3}}}
                ]
            }
        }
        bitstreams_no_pdf = {
            "_embedded": {
                "bitstreams": [
                    {"uuid": "bs-2", "_embedded": {"format": {"id": 5}}}
                ]
            }
        }
        mock_dspace_client.get_bundle_bitstreams = AsyncMock(
            side_effect=[bitstreams_with_pdf, bitstreams_no_pdf]
        )
        # Fallback path may call get_bitstream_format when format not embedded
        mock_dspace_client.get_bitstream_format = AsyncMock(return_value={"id": 5})
        result = await mock_dspace_client.count_items_with_bitstream_format(
            format_id=3, page_size=100, delay_between_pages=0, delay_between_items=0
        )
        assert result["count"] == 1
        assert result["total_items_processed"] == 2

    @pytest.mark.asyncio
    async def test_count_items_with_pdf_bitstream(self, mock_dspace_client):
        """Test count_items_with_pdf_bitstream resolves PDF id and returns count."""
        mock_dspace_client.resolve_pdf_format_id = AsyncMock(return_value=3)
        mock_dspace_client.count_items_with_bitstream_format = AsyncMock(
            return_value={"count": 42, "total_items_processed": 100}
        )
        result = await mock_dspace_client.count_items_with_pdf_bitstream(
            pdf_format_id=None, delay_between_pages=0
        )
        assert result["count"] == 42
        assert result["total_items_processed"] == 100
        assert result["pdf_format_id"] == 3
        mock_dspace_client.resolve_pdf_format_id.assert_called_once_with(override_format_id=None)
        mock_dspace_client.count_items_with_bitstream_format.assert_called_once()

    @pytest.mark.asyncio
    async def test_count_items_with_bitstream_format_uses_cache(self, mock_dspace_client, tmp_path):
        """When cache has entries and force_rerun is False, cached items are skipped (no bundle API calls)."""
        search_page1 = {
            "_embedded": {
                "searchResult": {
                    "_embedded": {
                        "objects": [
                            {"_embedded": {"indexableObject": {"uuid": "item-1"}}},
                            {"_embedded": {"indexableObject": {"uuid": "item-2"}}},
                        ]
                    }
                }
            }
        }
        search_page2 = {"_embedded": {"searchResult": {"_embedded": {"objects": []}}}}
        mock_dspace_client.search_items = AsyncMock(side_effect=[search_page1, search_page2])
        mock_dspace_client.get_item_bundles = AsyncMock()  # should never be called when cache hits

        cache = RestPDFCountCache(base_url="https://test.edu", cache_dir=tmp_path)
        cache.update("item-1", True)   # has PDF
        cache.update("item-2", False)  # no PDF

        result = await mock_dspace_client.count_items_with_bitstream_format(
            format_id=3,
            page_size=100,
            delay_between_pages=0,
            delay_between_items=0,
            cache=cache,
            force_rerun=False,
        )
        assert result["count"] == 1
        assert result["total_items_processed"] == 2
        # No get_item_bundles calls because both items were in cache
        mock_dspace_client.get_item_bundles.assert_not_called()

    @pytest.mark.asyncio
    async def test_detect_dspace_version_single_property(self, mock_dspace_client):
        """Test detect_dspace_version uses config/properties/dspace.version and parses values[0]."""
        from unittest.mock import MagicMock
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"name": "dspace.version", "values": ["7.6"]}
        mock_dspace_client.client.get = AsyncMock(return_value=mock_response)
        result = await mock_dspace_client.detect_dspace_version()
        assert result == "7.6"
        assert mock_dspace_client.last_detected_server_version == "7.6"
        mock_dspace_client.client.get.assert_called()
        first_call_url = mock_dspace_client.client.get.call_args_list[0][0][0]
        assert "config/properties/dspace.version" in first_call_url

    @pytest.mark.asyncio
    async def test_detect_dspace_version_normalize_patch(self, mock_dspace_client):
        """Test detect_dspace_version normalizes 9.0.1 to 9.0."""
        from unittest.mock import MagicMock
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"name": "dspace.version", "values": ["9.0.1"]}
        mock_dspace_client.client.get = AsyncMock(return_value=mock_response)
        result = await mock_dspace_client.detect_dspace_version()
        assert result == "9.0"

    @pytest.mark.asyncio
    async def test_detect_dspace_version_handles_prefixed_string(self, mock_dspace_client):
        """Test detect_dspace_version extracts version from 'DSpace 7.6'."""
        from unittest.mock import MagicMock
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"name": "dspace.version", "values": ["DSpace 7.6"]}
        mock_dspace_client.client.get = AsyncMock(return_value=mock_response)
        result = await mock_dspace_client.detect_dspace_version()
        assert result == "7.6"
