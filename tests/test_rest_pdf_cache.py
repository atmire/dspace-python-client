"""Tests for RestPDFCountCache."""

from dspace_client.rest_pdf_cache import (
    RestPDFCountCache,
    _normalize_base_url,
    _repository_cache_id,
)


class TestRepositoryCacheId:
    def test_normalize_base_url(self):
        assert _normalize_base_url("https://repo.example.edu/") == "https://repo.example.edu"
        assert _normalize_base_url("https://repo.example.edu") == "https://repo.example.edu"
        assert _normalize_base_url("  https://x.y.z  ") == "https://x.y.z"

    def test_repository_cache_id_stable(self):
        assert _repository_cache_id("https://repo.example.edu") == _repository_cache_id(
            "https://repo.example.edu/"
        )
        assert _repository_cache_id("https://repo.example.edu") != "default"

    def test_repository_cache_id_safe_filename(self):
        out = _repository_cache_id("https://my-repo.example.edu:443/path")
        assert " " not in out
        assert "/" not in out
        assert ":" not in out


class TestRestPDFCountCache:
    def test_save_and_load(self, tmp_path):
        cache = RestPDFCountCache(base_url="https://test.edu", cache_dir=tmp_path)
        cache.update("uuid-1", True)
        cache.update("uuid-2", False)
        cache.save()
        assert cache.cache_path.exists()

        cache2 = RestPDFCountCache(base_url="https://test.edu", cache_dir=tmp_path)
        cache2.load()
        assert cache2.get("uuid-1") is True
        assert cache2.get("uuid-2") is False
        assert cache2.get("uuid-3") is None
        assert cache2.totals() == (2, 1)

    def test_totals(self, tmp_path):
        cache = RestPDFCountCache(base_url="https://x.edu", cache_dir=tmp_path)
        assert cache.totals() == (0, 0)
        cache.update("a", True)
        cache.update("b", True)
        cache.update("c", False)
        assert cache.totals() == (3, 2)

    def test_load_missing_file(self, tmp_path):
        cache = RestPDFCountCache(base_url="https://y.edu", cache_dir=tmp_path)
        cache.load()
        assert cache.totals() == (0, 0)
        assert cache.get("any") is None
