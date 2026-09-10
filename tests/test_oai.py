"""Tests for OAI-PMH client, dc:format parsing, and PDF count cache."""

import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from dspace_client.exceptions import OAIError
from dspace_client.oai import (
    OAIClient,
    OAIPDFCountCache,
    OAIRecord,
    _check_error,
    _parse_identify,
    _parse_list_records,
    _repository_cache_id,
    get_dc_formats,
    record_has_pdf,
)

# --- XML parsing ---


def test_check_error_raises_on_error_element():
    root = ET.fromstring(
        '<?xml version="1.0"?>'
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        '<error code="noRecordsMatch">No records match the criteria.</error>'
        "</OAI-PMH>"
    )
    with pytest.raises(OAIError) as exc_info:
        _check_error(root)
    assert exc_info.value.code == "noRecordsMatch"
    assert "No records match" in exc_info.value.message


def test_check_error_passes_without_error():
    root = ET.fromstring(
        '<?xml version="1.0"?>'
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        "<Identify><repositoryName>Test</repositoryName></Identify>"
        "</OAI-PMH>"
    )
    _check_error(root)  # no raise


def test_parse_identify():
    root = ET.fromstring(
        '<?xml version="1.0"?>'
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        "<Identify>"
        "<repositoryName>My Repo</repositoryName>"
        "<baseURL>https://repo.example.org/oai</baseURL>"
        "<protocolVersion>2.0</protocolVersion>"
        "<earliestDatestamp>2020-01-01</earliestDatestamp>"
        "<deletedRecord>no</deletedRecord>"
        "<granularity>YYYY-MM-DD</granularity>"
        "</Identify>"
        "</OAI-PMH>"
    )
    result = _parse_identify(root)
    assert result.repository_name == "My Repo"
    assert result.base_url == "https://repo.example.org/oai"
    assert result.earliest_datestamp == "2020-01-01"
    assert result.deleted_record == "no"


def test_parse_list_records():
    root = ET.fromstring(
        '<?xml version="1.0"?>'
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/" '
        'xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<ListRecords>"
        "<record>"
        "<header>"
        "<identifier>oai:repo:123</identifier>"
        "<datestamp>2024-01-15</datestamp>"
        "</header>"
        "<metadata>"
        "<oai_dc:dc><dc:format>application/pdf</dc:format></oai_dc:dc>"
        "</metadata>"
        "</record>"
        "<record>"
        "<header><identifier>oai:repo:456</identifier><datestamp>2024-01-16</datestamp></header>"
        "<metadata><oai_dc:dc></oai_dc:dc></metadata>"
        "</record>"
        '<resumptionToken completeListSize="100" cursor="2">abc</resumptionToken>'
        "</ListRecords>"
        "</OAI-PMH>"
    )
    result = _parse_list_records(root)
    assert len(result.records) == 2
    assert result.records[0].identifier == "oai:repo:123"
    assert result.records[0].datestamp == "2024-01-15"
    assert result.records[1].identifier == "oai:repo:456"
    assert result.resumption_token is not None
    assert result.resumption_token.value == "abc"
    assert result.resumption_token.complete_list_size == 100


# --- dc:format and has_pdf ---


def test_get_dc_formats_from_oai_dc():
    metadata = ET.fromstring(
        "<metadata xmlns:oai_dc='http://www.openarchives.org/OAI/2.0/oai_dc/' "
        "xmlns:dc='http://purl.org/dc/elements/1.1/'>"
        "<oai_dc:dc>"
        "<dc:format>application/pdf</dc:format>"
        "<dc:format>text/plain</dc:format>"
        "</oai_dc:dc>"
        "</metadata>"
    )
    formats = get_dc_formats(metadata)
    assert "application/pdf" in formats
    assert "text/plain" in formats


def test_get_dc_formats_empty():
    assert get_dc_formats(None) == []
    metadata = ET.fromstring(
        "<metadata xmlns:dc='http://purl.org/dc/elements/1.1/'><dc></dc></metadata>"
    )
    assert get_dc_formats(metadata) == []


def test_record_has_pdf_true():
    metadata = ET.fromstring(
        "<metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>"
        "<dc><dc:format>application/pdf</dc:format></dc>"
        "</metadata>"
    )
    rec = OAIRecord(identifier="oai:x:1", datestamp="2024-01-01", metadata=metadata)
    assert record_has_pdf(rec) is True


def test_record_has_pdf_false():
    metadata = ET.fromstring(
        "<metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>"
        "<dc><dc:format>text/html</dc:format></dc>"
        "</metadata>"
    )
    rec = OAIRecord(identifier="oai:x:2", datestamp="2024-01-01", metadata=metadata)
    assert record_has_pdf(rec) is False


def test_record_has_pdf_deleted():
    rec = OAIRecord(identifier="oai:x:3", datestamp="2024-01-01", status="deleted", metadata=None)
    assert record_has_pdf(rec) is False


# --- Cache ---


def test_repository_cache_id():
    # Normalized host with dots and non-alnum replaced by underscore
    id1 = _repository_cache_id("https://demo.dspace.org")
    assert "demo" in id1 and "dspace" in id1 and "org" in id1
    id2 = _repository_cache_id("https://bradscholars.brad.ac.uk")
    assert "bradscholars" in id2 or "brad" in id2


def test_oai_pdf_count_cache_save_load_totals():
    with tempfile.TemporaryDirectory() as tmp:
        cache_dir = Path(tmp)
        cache = OAIPDFCountCache(base_url="https://test.example.org", cache_dir=cache_dir)
        cache.update("oai:test:1", "2024-01-01", True)
        cache.update("oai:test:2", "2024-01-02", False)
        cache.save()
        assert cache.cache_path.exists()
        total, with_pdf = cache.totals()
        assert total == 2
        assert with_pdf == 1

        cache2 = OAIPDFCountCache(base_url="https://test.example.org", cache_dir=cache_dir)
        cache2.load()
        total2, with_pdf2 = cache2.totals()
        assert total2 == 2
        assert with_pdf2 == 1
        assert cache2.get("oai:test:1") == {"datestamp": "2024-01-01", "has_pdf": True}
        assert cache2.get("oai:test:2")["has_pdf"] is False


@pytest.mark.asyncio
async def test_oai_client_build_url():
    client = OAIClient(base_url="https://repo.example.org")
    url = client._build_url("ListRecords", metadata_prefix="oai_dc")
    assert "verb=ListRecords" in url
    assert "metadataPrefix=oai_dc" in url
    assert "server/oai/request" in url
