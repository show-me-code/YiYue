"""缓存键单元测试。"""

from app.services import cache


def test_cache_key_stable():
    a = cache.compute_cache_key("hash1", {"lang_in": "en", "part_size": 20})
    b = cache.compute_cache_key("hash1", {"part_size": 20, "lang_in": "en"})
    assert a == b
    assert len(a) == 64


def test_cache_key_differs():
    a = cache.compute_cache_key("hash1", {"lang_in": "en"})
    b = cache.compute_cache_key("hash2", {"lang_in": "en"})
    c = cache.compute_cache_key("hash1", {"lang_in": "fr"})
    assert a != b and a != c


def test_glossary_hash_order_insensitive():
    e1 = [{"source": "a", "target": "A", "tgt_lang": "zh"},
          {"source": "b", "target": "B", "tgt_lang": "zh"}]
    e2 = list(reversed(e1))
    assert cache.glossary_hash(e1) == cache.glossary_hash(e2)


def test_index_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "INDEX_PATH", tmp_path / "index.json")
    assert cache.find_cached_task("k") is None
    cache.register_cache("k", 42)
    assert cache.find_cached_task("k") == 42
    cache.remove_task_from_cache(42)
    assert cache.find_cached_task("k") is None
