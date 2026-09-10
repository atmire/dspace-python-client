"""Tests for RestContractFetcher."""

from pathlib import Path

from dspace_client.docs import DEFAULT_CACHE_DIR, RestContractFetcher
from dspace_client.versions import REST_CONTRACT_BRANCHES


def test_default_cache_dir_is_project_relative():
    fetcher = RestContractFetcher()
    assert fetcher.cache_dir == DEFAULT_CACHE_DIR
    assert fetcher.cache_dir.is_absolute()
    assert fetcher.cache_dir.name == "dspace-rest-api"


def test_version_mapping_matches_versions_module():
    fetcher = RestContractFetcher()
    assert fetcher.VERSION_MAPPING == REST_CONTRACT_BRANCHES


def test_custom_cache_dir(tmp_path: Path):
    custom = tmp_path / "cache"
    fetcher = RestContractFetcher(cache_dir=custom)
    assert fetcher.cache_dir == custom
    assert custom.exists()
