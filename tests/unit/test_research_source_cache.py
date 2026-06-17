from tests.unit._research_helpers import _src

from peers.research.source_cache import SourceCache


def test_cache_records_and_round_trips(tmp_path):
    cache = SourceCache(tmp_path / "sources.jsonl")
    s = _src("https://a.example/x", "a.example", content="hello")
    cache.add(s)
    got = cache.by_content_hash(s.content_hash)
    assert got is not None and got.resolved_origin == "a.example"
    assert got.access_failure is None


def test_cache_records_access_failure(tmp_path):
    cache = SourceCache(tmp_path / "sources.jsonl")
    s = _src("https://down.example/x", "down.example", failure="timeout")
    cache.add(s)
    assert cache.by_content_hash(s.content_hash).access_failure == "timeout"


def test_lookup_before_any_add_is_none(tmp_path):
    # edge: the cache file does not exist yet -> a miss, never a crash.
    cache = SourceCache(tmp_path / "sources.jsonl")
    assert cache.by_content_hash("deadbeef") is None


def test_unknown_hash_is_a_miss(tmp_path):
    # sad: a hash that was never recorded returns None.
    cache = SourceCache(tmp_path / "sources.jsonl")
    cache.add(_src("https://a.example/x", "a.example", content="hello"))
    assert cache.by_content_hash("0" * 64) is None


def test_duplicate_hash_returns_the_first_recorded(tmp_path):
    # edge: same content_hash added twice (e.g. two URLs serving identical
    # bytes) — the FIRST recorded source wins, a deterministic read.
    cache = SourceCache(tmp_path / "sources.jsonl")
    first = _src("https://a.example/x", "a.example", content="same")
    second = _src("https://b.example/y", "b.example", content="same")
    assert first.content_hash == second.content_hash  # identical bytes
    cache.add(first)
    cache.add(second)
    got = cache.by_content_hash(first.content_hash)
    assert got.url == "https://a.example/x" and got.resolved_origin == "a.example"


def test_corrupt_line_is_skipped_and_earlier_match_still_found(tmp_path):
    # sad: a torn / non-JSON line (e.g. a crash mid-append) must not blind the
    # whole lookup — a well-formed earlier row is still returned.
    cache = SourceCache(tmp_path / "sources.jsonl")
    s = _src("https://a.example/x", "a.example", content="hello")
    cache.add(s)
    with open(tmp_path / "sources.jsonl", "a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
    assert cache.by_content_hash(s.content_hash).resolved_origin == "a.example"
