"""Opt-in, allowlisted, fail-closed web fetcher for the research `web` modality.

`peers research --modalities web` needs a ``fetch(url) -> (bytes, origin)|None``
and a ``web_search(q) -> [url]``; the CLI wired neither, so the modality was inert
(codebase-only stays honestly dry). This adds a SAFE building block: a
deny-by-default, allowlisted fetcher over an INJECTED transport (no live network in
tests), with SSRF guards (scheme + private/loopback host refusal), a size cap, and
fail-closed status handling. A failed/blocked fetch returns None -> the
CodebaseSweeper records an honest ``access_failure``, never a fabricated witness.

Covers happy / sad (blocked host, bad scheme, private host, transport error,
non-2xx, oversize) / edge (empty allow = deny-all; bad regex).
"""
from __future__ import annotations

import pytest

from peers.research.web_fetch import AllowlistedFetcher, make_seed_url_search


def _ok_transport(body: bytes = b"hello", status: int = 200, final: str | None = None):
    calls = []

    def transport(url: str):
        calls.append(url)
        return (status, body, final or url)

    transport.calls = calls  # type: ignore[attr-defined]
    return transport


# --- happy ----------------------------------------------------------------
def test_happy_allowed_host_fetches() -> None:
    t = _ok_transport(b"<html>doc</html>", final="https://example.com/p")
    f = AllowlistedFetcher(allow=[r"example\.com"], transport=t)
    res = f.fetch("https://example.com/p")
    assert res == (b"<html>doc</html>", "https://example.com/p")
    assert t.calls == ["https://example.com/p"]


# --- sad: host / scheme / SSRF guards (deny BEFORE the transport runs) -----
def test_sad_disallowed_host_returns_none_without_calling_transport() -> None:
    t = _ok_transport()
    f = AllowlistedFetcher(allow=[r"example\.com"], transport=t)
    assert f.fetch("https://evil.test/x") is None
    assert t.calls == []                          # never reached the network


def test_sad_empty_allow_is_deny_all() -> None:
    t = _ok_transport()
    f = AllowlistedFetcher(allow=[], transport=t)
    assert f.fetch("https://example.com/") is None
    assert t.calls == []


def test_sad_non_http_scheme_blocked() -> None:
    t = _ok_transport()
    f = AllowlistedFetcher(allow=[r".*"], transport=t)
    assert f.fetch("file:///etc/passwd") is None
    assert f.fetch("ftp://example.com/x") is None
    assert t.calls == []


@pytest.mark.parametrize("url", [
    "http://localhost/x", "http://127.0.0.1/x", "http://10.0.0.5/x",
    "http://192.168.1.1/x", "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/x", "http://172.16.0.1/x",
])
def test_sad_private_or_loopback_host_blocked(url: str) -> None:
    # SSRF defense-in-depth: even an allow-matching private/loopback/link-local
    # target (e.g. the cloud metadata endpoint) is refused.
    t = _ok_transport()
    f = AllowlistedFetcher(allow=[r".*"], transport=t)
    assert f.fetch(url) is None
    assert t.calls == []


@pytest.mark.parametrize("url", [
    "http://2130706433/x",          # decimal 127.0.0.1
    "http://017700000001/x",        # octal 127.0.0.1
    "http://0x7f.0.0.1/x",          # hex-leading 127.0.0.1
    "http://127.1/x",               # short-form 127.0.0.1
    "http://2852039166/x",          # decimal 169.254.169.254 (cloud metadata)
    "http://127.0.0.1./x",          # trailing-dot
    "http://10.0.0.5/x",            # canonical private IP literal
    "http://93.184.216.34/x",       # canonical PUBLIC IP literal — still refused (use the domain)
])
def test_sad_ip_encoding_and_raw_ip_blocked(url: str) -> None:
    # S4 review (HIGH): ipaddress is strict but the OS resolver is permissive, so a
    # non-canonical numeric host (decimal/octal/hex/short-form) defeats an IP-only
    # guard and resolves to loopback/metadata. The fetcher accepts ONLY FQDN hosts
    # with an alphabetic TLD; ALL IP literals (canonical or encoded) are refused.
    t = _ok_transport()
    f = AllowlistedFetcher(allow=[r".*"], transport=t)
    assert f.fetch(url) is None
    assert t.calls == []                          # blocked before the transport


def test_sad_redirect_final_url_to_private_host_is_blocked() -> None:
    # S4 review (CRITICAL): a transport that lands on a different (private/metadata)
    # host after a redirect must NOT leak the body. The fetcher re-validates the
    # FINAL url's host (defense-in-depth on top of the transport not following
    # redirects), so a redirect to a forbidden host returns None.
    t = _ok_transport(b"INTERNAL-SECRET", final="http://127.0.0.2/meta")
    f = AllowlistedFetcher(allow=[r"docs\.example\.com"], transport=t)
    assert f.fetch("https://docs.example.com/p") is None


def test_sad_redirect_final_url_to_unlisted_host_is_blocked() -> None:
    # the final host must ALSO satisfy the allow-list, not just be a safe FQDN — a
    # redirect to an unrelated public host the operator never approved is refused.
    t = _ok_transport(b"x", final="https://other-host.test/landing")
    f = AllowlistedFetcher(allow=[r"docs\.example\.com"], transport=t)
    assert f.fetch("https://docs.example.com/p") is None


def test_happy_redirect_to_another_allowed_host_ok() -> None:
    # a redirect that lands on an allow-listed, safe FQDN is fine.
    t = _ok_transport(b"doc", final="https://docs.example.com/final")
    f = AllowlistedFetcher(allow=[r"docs\.example\.com", r"www\.example\.com"], transport=t)
    assert f.fetch("https://www.example.com/p") == (b"doc", "https://docs.example.com/final")


# --- sad: transport / response failures fail closed -----------------------
def test_sad_transport_error_returns_none() -> None:
    def boom(url):
        raise OSError("connection refused")
    f = AllowlistedFetcher(allow=[r"example\.com"], transport=boom)
    assert f.fetch("https://example.com/") is None


def test_sad_non_2xx_returns_none() -> None:
    f = AllowlistedFetcher(allow=[r"example\.com"], transport=_ok_transport(status=404))
    assert f.fetch("https://example.com/") is None
    f2 = AllowlistedFetcher(allow=[r"example\.com"], transport=_ok_transport(status=500))
    assert f2.fetch("https://example.com/") is None


def test_sad_oversize_body_returns_none() -> None:
    big = b"x" * 2048
    f = AllowlistedFetcher(allow=[r"example\.com"],
                           transport=_ok_transport(big), max_bytes=1024)
    assert f.fetch("https://example.com/") is None


def test_size_cap_boundary_exact_ok_over_blocked() -> None:
    # exactly max_bytes is accepted; one byte over is refused (boundary pin).
    f_ok = AllowlistedFetcher(allow=[r"example\.com"],
                              transport=_ok_transport(b"x" * 1024), max_bytes=1024)
    assert f_ok.fetch("https://example.com/") == (b"x" * 1024, "https://example.com/")
    f_over = AllowlistedFetcher(allow=[r"example\.com"],
                                transport=_ok_transport(b"x" * 1025), max_bytes=1024)
    assert f_over.fetch("https://example.com/") is None


# --- edge -----------------------------------------------------------------
def test_edge_bad_regex_in_allow_fails_at_construction() -> None:
    with pytest.raises((ValueError, Exception)):
        AllowlistedFetcher(allow=["(unclosed"], transport=_ok_transport())


def test_edge_allow_anchors_full_host_not_substring() -> None:
    # 'example.com' must not match 'example.com.evil.test' (anchored full-host).
    t = _ok_transport()
    f = AllowlistedFetcher(allow=[r"example\.com"], transport=t)
    assert f.fetch("https://example.com.evil.test/x") is None
    assert t.calls == []


# --- seed-URL search ------------------------------------------------------
def test_seed_url_search_returns_configured_seeds() -> None:
    search = make_seed_url_search(["https://a.test/", "https://b.test/"])
    assert search("any question") == ["https://a.test/", "https://b.test/"]
    assert search("another") == ["https://a.test/", "https://b.test/"]


def test_seed_url_search_empty_is_no_urls() -> None:
    assert make_seed_url_search([])("q") == []
