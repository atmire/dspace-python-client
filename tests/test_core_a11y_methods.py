"""Tests for the DSpaceClient methods added for the PDF accessibility example.

Covers bitstream lookup and download, paging, the admin check, workspace/workflow listings
and the metadata registry. Download tests run against respx so the streaming path is real.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from dspace_client import DSpaceClient
from dspace_client.exceptions import DSpaceAPIError, DSpaceClientError

BASE = "https://repo.example.org"


def _response(data, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = data
    return response


@pytest.mark.asyncio
async def test_get_bitstream(mock_dspace_client):
    mock_dspace_client._request = AsyncMock(return_value=_response({"uuid": "bs-1"}))
    assert await mock_dspace_client.get_bitstream("bs-1") == {"uuid": "bs-1"}
    mock_dspace_client._request.assert_called_once_with(
        "GET", "core/bitstreams/bs-1", method_name="get_bitstream"
    )


@pytest.mark.asyncio
async def test_get_bitstream_item_follows_bundle(mock_dspace_client):
    mock_dspace_client._request = AsyncMock(
        side_effect=[
            _response({"uuid": "bundle-1", "name": "ORIGINAL"}),
            _response({"uuid": "item-1"}),
        ]
    )
    item = await mock_dspace_client.get_bitstream_item("bs-1")
    assert item == {"uuid": "item-1", "_bundle_name": "ORIGINAL"}
    calls = [c.args[1] for c in mock_dspace_client._request.call_args_list]
    assert calls == ["core/bitstreams/bs-1/bundle", "core/bundles/bundle-1/item"]


@pytest.mark.asyncio
async def test_iter_pages_walks_all_pages(mock_dspace_client):
    pages = [
        {"_embedded": {"things": [{"id": 1}, {"id": 2}]}, "page": {"totalPages": 2}},
        {"_embedded": {"things": [{"id": 3}]}, "page": {"totalPages": 2}},
    ]
    mock_dspace_client._request = AsyncMock(side_effect=[_response(p) for p in pages])
    got = [
        obj["id"]
        async for obj in mock_dspace_client.iter_pages(
            "x/things", "things", {"embed": "item"}, method_name="m", page_size=2
        )
    ]
    assert got == [1, 2, 3]
    second = mock_dspace_client._request.call_args_list[1]
    assert second.kwargs["params"] == {"embed": "item", "page": 1, "size": 2}


@pytest.mark.asyncio
async def test_iter_pages_empty_result(mock_dspace_client):
    mock_dspace_client._request = AsyncMock(return_value=_response({"page": {"totalPages": 0}}))
    assert [o async for o in mock_dspace_client.iter_pages("x", "y", method_name="m")] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("authorizations", "expected"), [([{"id": "a"}], True), ([], False)])
async def test_is_site_admin(mock_dspace_client, authorizations, expected):
    mock_dspace_client._request = AsyncMock(
        side_effect=[
            _response({"_embedded": {"sites": [{"uuid": "site-1"}]}}),
            _response({"_embedded": {"authorizations": authorizations}}),
        ]
    )
    assert await mock_dspace_client.is_site_admin("person-1") is expected
    authz_call = mock_dspace_client._request.call_args_list[1]
    assert authz_call.kwargs["params"] == {
        "uri": "https://demo.dspace.org/server/api/core/sites/site-1",
        "feature": "administratorOf",
        "eperson": "person-1",
    }


@pytest.mark.asyncio
async def test_get_current_eperson_from_embed(mock_dspace_client):
    mock_dspace_client._request = AsyncMock(
        return_value=_response({"authenticated": True, "_embedded": {"eperson": {"uuid": "p1"}}})
    )
    assert await mock_dspace_client.get_current_eperson() == {"uuid": "p1"}


@pytest.mark.asyncio
async def test_get_current_eperson_anonymous(mock_dspace_client):
    mock_dspace_client._request = AsyncMock(return_value=_response({"authenticated": False}))
    assert await mock_dspace_client.get_current_eperson() is None


@pytest.mark.asyncio
async def test_iter_workspace_items_all_vs_mine(mock_dspace_client):
    mock_dspace_client._request = AsyncMock(return_value=_response({"page": {"totalPages": 0}}))
    _ = [w async for w in mock_dspace_client.iter_workspace_items()]
    _ = [w async for w in mock_dspace_client.iter_workspace_items("me")]
    first, second = mock_dspace_client._request.call_args_list
    assert first.args[1] == "submission/workspaceitems"
    assert second.args[1] == "submission/workspaceitems/search/findBySubmitter"
    assert second.kwargs["params"]["uuid"] == "me"


@pytest.mark.asyncio
async def test_find_metadata_field(mock_dspace_client):
    mock_dspace_client._request = AsyncMock(
        return_value=_response({"_embedded": {"metadatafields": [{"id": 7}]}})
    )
    assert await mock_dspace_client.find_metadata_field("local.a.b") == {"id": 7}
    assert mock_dspace_client._request.call_args.kwargs["params"] == {"exactName": "local.a.b"}


@pytest.mark.asyncio
async def test_find_metadata_field_missing(mock_dspace_client):
    mock_dspace_client._request = AsyncMock(return_value=_response({"_embedded": {}}))
    assert await mock_dspace_client.find_metadata_field("local.a.b") is None


@pytest.mark.asyncio
async def test_find_metadata_schema(mock_dspace_client):
    mock_dspace_client._request = AsyncMock(
        return_value=_response(
            {
                "_embedded": {
                    "metadataschemas": [{"id": 1, "prefix": "dc"}, {"id": 5, "prefix": "local"}]
                },
                "page": {"totalPages": 1},
            }
        )
    )
    assert (await mock_dspace_client.find_metadata_schema("local"))["id"] == 5
    assert await mock_dspace_client.find_metadata_schema("nope") is None


@pytest.mark.asyncio
async def test_create_metadata_field(mock_dspace_client):
    mock_dspace_client._request = AsyncMock(return_value=_response({"id": 9}))
    await mock_dspace_client.create_metadata_field(5, "accessibility", "report", scope_note="n")
    call = mock_dspace_client._request.call_args
    assert call.args[:2] == ("POST", "core/metadatafields")
    assert call.kwargs["params"] == {"schemaId": 5}
    assert call.kwargs["json_data"] == {
        "element": "accessibility",
        "qualifier": "report",
        "scopeNote": "n",
    }


@pytest.mark.asyncio
async def test_search_items_scope(mock_dspace_client):
    mock_dspace_client._request = AsyncMock(return_value=_response({}))
    await mock_dspace_client.search_items(scope="col-1", sort=None)
    assert mock_dspace_client._request.call_args.kwargs["params"]["scope"] == "col-1"


def _real_client(http: httpx.AsyncClient) -> DSpaceClient:
    return DSpaceClient(
        base_url=BASE, jwt_token="jwt", csrf_token="csrf", http_client=http, courtesy_delay=0
    )


@pytest.mark.asyncio
@respx.mock
async def test_download_bitstream_writes_file(tmp_path):
    respx.get(f"{BASE}/server/api/core/bitstreams/bs-1/content").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.7 hello")
    )
    async with httpx.AsyncClient() as http:
        written = await _real_client(http).download_bitstream("bs-1", tmp_path / "f.pdf")
    assert written == 14
    assert (tmp_path / "f.pdf").read_bytes() == b"%PDF-1.7 hello"


@pytest.mark.asyncio
@respx.mock
async def test_download_bitstream_over_limit_removes_file(tmp_path):
    respx.get(f"{BASE}/server/api/core/bitstreams/bs-1/content").mock(
        return_value=httpx.Response(200, content=b"x" * 100)
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(DSpaceClientError, match="larger than"):
            await _real_client(http).download_bitstream("bs-1", tmp_path / "f.pdf", max_bytes=10)
    assert not (tmp_path / "f.pdf").exists()


@pytest.mark.asyncio
@respx.mock
async def test_download_bitstream_http_error(tmp_path):
    respx.get(f"{BASE}/server/api/core/bitstreams/bs-1/content").mock(
        return_value=httpx.Response(403, text="forbidden")
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(DSpaceAPIError) as excinfo:
            await _real_client(http).download_bitstream("bs-1", tmp_path / "f.pdf")
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
@respx.mock
async def test_move_bitstream_sends_uri_list():
    route = respx.put(f"{BASE}/server/api/core/bitstreams/bs-1/bundle").mock(
        return_value=httpx.Response(200, json={})
    )
    async with httpx.AsyncClient() as http:
        await _real_client(http).move_bitstream("bs-1", "bundle-2")
    request = route.calls.last.request
    assert request.headers["content-type"] == "text/uri-list"
    assert request.content == f"{BASE}/server/api/core/bundles/bundle-2".encode()


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(("current", "method"), [(204, "POST"), (200, "PUT")])
async def test_set_primary_bitstream_posts_or_puts(current, method):
    respx.get(f"{BASE}/server/api/core/bundles/b1/primaryBitstream").mock(
        return_value=httpx.Response(current, json={"uuid": "old"} if current == 200 else None)
    )
    route = respx.route(
        method=method, url=f"{BASE}/server/api/core/bundles/b1/primaryBitstream"
    ).mock(return_value=httpx.Response(200, json={}))
    async with httpx.AsyncClient() as http:
        await _real_client(http).set_primary_bitstream("b1", "new")
    assert route.called


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(("authorizations", "expected"), [([{"id": "x"}], True), ([], False)])
async def test_anonymous_can_download_sends_no_credentials(authorizations, expected):
    route = respx.get(f"{BASE}/server/api/authz/authorizations/search/object").mock(
        return_value=httpx.Response(200, json={"_embedded": {"authorizations": authorizations}})
    )
    async with httpx.AsyncClient() as http:
        assert await _real_client(http).anonymous_can_download("bs-1") is expected
    request = route.calls.last.request
    assert "authorization" not in request.headers
    assert request.url.params["feature"] == "canDownload"
    assert "eperson" not in request.url.params
