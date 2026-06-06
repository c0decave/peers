"""Auth-proxy request forwarding and token-refresh tests."""
from __future__ import annotations

import io
import errno
import json
import os
import stat
import urllib.error
from pathlib import Path

from auth_proxy import oauth_refresh
from auth_proxy.oauth_refresh import refresh_claude_config
from auth_proxy.server import forward_request


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers=None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self, n: int | None = None) -> bytes:
        if n is None or n >= len(self._body):
            return self._body
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class MemoryTokenStore:
    def __init__(self) -> None:
        self.token = "old-token"
        self.refreshes = 0

    def access_token(self) -> str:
        return self.token

    def refresh(self) -> bool:
        self.refreshes += 1
        self.token = "new-token"
        return True


def test_forward_request_handles_empty_request_body_edge() -> None:
    # edge: zero-length body must still forward with the bearer attached
    # and not raise. urllib.request.Request rejects `data=b""` (would
    # send Content-Length: 0 GET), so the server-side helper has to
    # normalise empty bytes to None before constructing the request.
    captured = {}

    def opener(request, *, timeout):
        captured["body"] = request.data
        return FakeResponse(204, b"", {})

    response = forward_request(
        "GET", "/v1/messages", {}, b"", MemoryTokenStore(),
        opener=opener, timeout=5.0,
    )
    assert response.status == 204
    assert captured["body"] is None  # b"" must NOT be forwarded as a body


def test_forward_request_strips_duplicate_authorization_header_edge() -> None:
    # edge: a client may pass its own (stale) `Authorization` AND a
    # lowercased `authorization`. Both are dropped before the bearer
    # is injected; the outgoing request must carry exactly the
    # token_store's bearer.
    captured = {}

    def opener(request, *, timeout):
        captured["auth"] = request.get_header("Authorization")
        return FakeResponse(200, b"{}", {})

    forward_request(
        "POST", "/v1/messages",
        {"Authorization": "Bearer stale-1", "authorization": "Bearer stale-2"},
        b"{}", MemoryTokenStore(),
        opener=opener, timeout=5.0,
    )
    assert captured["auth"] == "Bearer old-token"


def test_refresh_claude_config_handles_unicode_in_token_url_edge(
    tmp_path: Path,
) -> None:
    # edge: an IDN-style hostname (punycoded loopback variants) MUST be
    # rejected — the parsed.hostname check normalizes case but unicode
    # lookalikes are not the literal "localhost"/"127.0.0.1" we accept.
    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "old",
            "refreshToken": "refresh-secret",
            # NOTE: U+FF11 (FULLWIDTH DIGIT ONE) — visually 127.0.0.1
            "tokenUrl": "http://１２７.0.0.1/token",
        }
    }), encoding="utf-8")

    import pytest as _pytest

    def opener(request, *, timeout):  # pragma: no cover - must not fire
        raise AssertionError("unicode lookalike must not connect")

    with _pytest.raises(oauth_refresh.OAuthRefreshError):
        refresh_claude_config(token_file, opener=opener)


def test_forward_request_injects_bearer_and_strips_host_auth() -> None:
    captured = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        captured["host"] = request.get_header("Host")
        captured["body"] = request.data
        captured["timeout"] = timeout
        return FakeResponse(200, b"ok", {"Content-Type": "application/json"})

    response = forward_request(
        "POST",
        "/v1/messages?x=1",
        {"Host": "attacker", "Authorization": "Bearer stolen"},
        b'{"hello": true}',
        MemoryTokenStore(),
        upstream_base="https://api.anthropic.test",
        opener=opener,
        timeout=3,
    )

    assert response.status == 200
    assert response.body == b"ok"
    assert captured == {
        "url": "https://api.anthropic.test/v1/messages?x=1",
        "auth": "Bearer old-token",
        "host": None,
        "body": b'{"hello": true}',
        "timeout": 3,
    }


def test_forward_request_strips_hop_headers_from_both_directions() -> None:
    captured = {}

    def opener(request, *, timeout):
        captured["connection"] = request.get_header("Connection")
        captured["proxy_auth"] = request.get_header("Proxy-Authorization")
        return FakeResponse(
            200,
            b"ok",
            {
                "Connection": "close",
                "Transfer-Encoding": "chunked",
                "Content-Length": "999",
                "X-Trace": "kept",
            },
        )

    response = forward_request(
        "GET",
        "/v1/messages",
        {
            "Connection": "keep-alive",
            "Proxy-Authorization": "secret",
        },
        b"",
        MemoryTokenStore(),
        upstream_base="https://api.anthropic.test",
        opener=opener,
    )

    assert captured == {"connection": None, "proxy_auth": None}
    assert response.headers == {"X-Trace": "kept"}


def test_forward_request_returns_502_on_upstream_connection_failure() -> None:
    def opener(request, *, timeout):
        raise urllib.error.URLError("connection refused")

    response = forward_request(
        "GET", "/v1/messages", {}, b"", MemoryTokenStore(),
        upstream_base="https://api.anthropic.test", opener=opener,
    )

    assert response.status == 502
    assert b"upstream request failed" in response.body


def test_forward_request_refreshes_once_on_401() -> None:
    store = MemoryTokenStore()
    seen_auth = []

    def opener(request, *, timeout):
        seen_auth.append(request.get_header("Authorization"))
        if len(seen_auth) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "expired",
                {},
                io.BytesIO(b"expired"),
            )
        return FakeResponse(200, b"retried")

    response = forward_request(
        "GET", "/v1/messages", {}, b"", store,
        upstream_base="https://api.anthropic.test", opener=opener,
    )

    assert response.status == 200
    assert response.body == b"retried"
    assert store.refreshes == 1
    assert seen_auth == ["Bearer old-token", "Bearer new-token"]


def test_refresh_claude_config_updates_nested_token_file(tmp_path: Path) -> None:
    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "old",
            "refreshToken": "refresh",
            "tokenUrl": "https://auth.example/token",
            "clientId": "client-1",
            "expiresAt": 1,
        }
    }), encoding="utf-8")
    token_file.chmod(0o644)
    captured = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode("utf-8")
        return FakeResponse(
            200,
            json.dumps({
                "access_token": "new",
                "refresh_token": "refresh-2",
                "expires_in": 60,
            }).encode("utf-8"),
        )

    token = refresh_claude_config(token_file, opener=opener, now=lambda: 10)

    data = json.loads(token_file.read_text(encoding="utf-8"))
    assert token == "new"
    assert captured["url"] == "https://auth.example/token"
    assert "grant_type=refresh_token" in captured["body"]
    assert "refresh_token=refresh" in captured["body"]
    assert data["oauthAccount"]["accessToken"] == "new"
    assert data["oauthAccount"]["refreshToken"] == "refresh-2"
    assert data["oauthAccount"]["expiresAt"] == 70
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_refresh_claude_config_rewrites_file_bind_mount_when_replace_busy(
    tmp_path: Path, monkeypatch,
) -> None:
    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "old",
            "refreshToken": "refresh",
            "tokenUrl": "https://auth.example/token",
        }
    }), encoding="utf-8")

    def opener(request, *, timeout):
        return FakeResponse(
            200,
            json.dumps({
                "access_token": "new",
                "refresh_token": "refresh-2",
            }).encode("utf-8"),
        )

    def busy_replace(src, dst):
        raise OSError(errno.EBUSY, "mountpoint is busy")

    monkeypatch.setattr(oauth_refresh.os, "replace", busy_replace)

    token = refresh_claude_config(token_file, opener=opener)

    data = json.loads(token_file.read_text(encoding="utf-8"))
    assert token == "new"
    assert data["oauthAccount"]["accessToken"] == "new"
    assert data["oauthAccount"]["refreshToken"] == "refresh-2"
    assert not (tmp_path / ".claude.json.tmp").exists()


def test_refresh_claude_config_rejects_http_token_url_BUG_203(
    tmp_path: Path,
) -> None:
    """BUG-203 reproducer: refresh_claude_config must NOT POST the
    refresh_token to a plain http:// endpoint. A misconfigured tokenUrl (or
    a compromised token file) would otherwise exfiltrate the long-lived
    refresh credential in cleartext. Expected: any non-https scheme is
    rejected with OAuthRefreshError BEFORE the request fires."""
    import pytest

    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "old",
            "refreshToken": "refresh-secret",
            "tokenUrl": "http://attacker.example/token",
            "clientId": "client-1",
        }
    }), encoding="utf-8")

    captured_body: dict = {}

    def opener(request, *, timeout):  # pragma: no cover - must not fire
        captured_body["url"] = request.full_url
        captured_body["body"] = request.data.decode("utf-8")
        return FakeResponse(
            200, json.dumps({"access_token": "new"}).encode("utf-8"),
        )

    with pytest.raises(oauth_refresh.OAuthRefreshError, match="https://"):
        refresh_claude_config(token_file, opener=opener)

    assert "refresh_token=refresh-secret" not in captured_body.get("body", ""), (
        "BUG-203: refresh_token was POSTed over cleartext http:// to "
        f"{captured_body.get('url')!r}"
    )


def test_refresh_claude_config_rejects_http_env_token_url_BUG_203(
    tmp_path: Path, monkeypatch,
) -> None:
    import pytest

    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "old",
            "refreshToken": "refresh-secret",
            "tokenUrl": "https://auth.example/token",
        }
    }), encoding="utf-8")
    monkeypatch.setenv(
        "AUTH_PROXY_OAUTH_TOKEN_URL", "http://attacker.example/token",
    )

    def opener(request, *, timeout):  # pragma: no cover - must not fire
        raise AssertionError("refresh request must be rejected before network IO")

    with pytest.raises(oauth_refresh.OAuthRefreshError, match="https://"):
        refresh_claude_config(token_file, opener=opener)


def test_refresh_claude_config_rejects_https_url_without_host_BUG_203(
    tmp_path: Path,
) -> None:
    import pytest

    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "old",
            "refreshToken": "refresh-secret",
            "tokenUrl": "https:///token",
        }
    }), encoding="utf-8")

    def opener(request, *, timeout):  # pragma: no cover - must not fire
        raise AssertionError("malformed refresh endpoint must not be requested")

    with pytest.raises(oauth_refresh.OAuthRefreshError, match="https://"):
        refresh_claude_config(token_file, opener=opener)


def test_refresh_claude_config_accepts_loopback_http_token_url(
    tmp_path: Path, monkeypatch,
) -> None:
    """RFC 8252 §7.3 — http:// to a loopback host stays on the local
    machine, so it does not exfiltrate the refresh_token. The exception is
    required for local dev / integration tests; the BUG-203 protection
    remains for any non-loopback http URL.

    BUG-155: the loopback bypass now requires
    AUTH_PROXY_OAUTH_ALLOW_LOOPBACK=1 to be set, because the auth-proxy
    sidecar joins the egress-proxy netns alongside the main peers
    container in production and loopback there is shared with peer code.
    Tests opt in explicitly."""
    monkeypatch.setenv("AUTH_PROXY_OAUTH_ALLOW_LOOPBACK", "1")
    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "old",
            "refreshToken": "refresh-secret",
            "tokenUrl": "http://127.0.0.1:65535/oauth/token",
        }
    }), encoding="utf-8")

    seen_url: dict = {}

    def opener(request, *, timeout):
        seen_url["url"] = request.full_url
        return FakeResponse(
            200, json.dumps({"access_token": "new"}).encode("utf-8"),
        )

    token = refresh_claude_config(token_file, opener=opener)
    assert token == "new"
    assert seen_url["url"] == "http://127.0.0.1:65535/oauth/token"


def test_refresh_claude_config_accepts_loopback_localhost_http_token_url(
    tmp_path: Path, monkeypatch,
) -> None:
    """The localhost hostname (case-insensitive) is also a loopback alias.
    BUG-155: opt-in env still required."""
    monkeypatch.setenv("AUTH_PROXY_OAUTH_ALLOW_LOOPBACK", "1")
    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "old",
            "refreshToken": "refresh-secret",
            "tokenUrl": "http://LocalHost:9000/oauth/token",
        }
    }), encoding="utf-8")

    def opener(request, *, timeout):
        return FakeResponse(
            200, json.dumps({"access_token": "new"}).encode("utf-8"),
        )

    token = refresh_claude_config(token_file, opener=opener)
    assert token == "new"


def test_refresh_claude_config_refuses_loopback_without_opt_in(
    tmp_path: Path, monkeypatch,
) -> None:
    """BUG-155 sad-path: without AUTH_PROXY_OAUTH_ALLOW_LOOPBACK=1, a
    loopback http:// token URL is treated like any other http URL and
    refused. This is the production default for the auth-proxy sidecar
    container, which shares the egress-proxy netns with peer code."""
    import pytest
    monkeypatch.delenv("AUTH_PROXY_OAUTH_ALLOW_LOOPBACK", raising=False)
    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "old",
            "refreshToken": "refresh-secret",
            "tokenUrl": "http://127.0.0.1:65535/oauth/token",
        }
    }), encoding="utf-8")

    def opener(request, *, timeout):  # pragma: no cover - must not fire
        raise AssertionError("loopback endpoint must not be requested")

    with pytest.raises(oauth_refresh.OAuthRefreshError, match="https://"):
        refresh_claude_config(token_file, opener=opener)


def test_refresh_claude_config_loopback_opt_in_rejects_wrong_value(
    tmp_path: Path, monkeypatch,
) -> None:
    """BUG-155 edge: only ``1``/``true``/``yes`` enable the loopback
    bypass — any other value (including the empty string) is treated as
    not-opted-in. Defends against accidental leakage like setting the env
    to ``0`` and assuming it disables the check."""
    import pytest
    monkeypatch.setenv("AUTH_PROXY_OAUTH_ALLOW_LOOPBACK", "0")
    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "old",
            "refreshToken": "refresh-secret",
            "tokenUrl": "http://127.0.0.1:65535/oauth/token",
        }
    }), encoding="utf-8")

    def opener(request, *, timeout):  # pragma: no cover - must not fire
        raise AssertionError("loopback endpoint must not be requested")

    with pytest.raises(oauth_refresh.OAuthRefreshError, match="https://"):
        refresh_claude_config(token_file, opener=opener)


def test_refresh_claude_config_handles_malformed_ipv6_token_url_BUG_302(
    tmp_path: Path,
) -> None:
    """BUG-302 reproducer: urllib.parse.urlsplit() raises ValueError on a
    malformed IPv6 bracket sequence (e.g. ``http://[invalid/token``). The
    BUG-203 hardening at _token_url calls urlsplit without a try/except, so
    the ValueError propagates instead of becoming the controlled
    OAuthRefreshError every other bad-URL path produces. The ClaudeTokenStore
    in auth_proxy.server catches OAuthRefreshError but NOT ValueError, so
    the auth-proxy would return an opaque HTTP 500 instead of the structured
    502 + diagnostic. Expected: ValueError is caught and re-raised as
    OAuthRefreshError with the same 'must be an https:// URL' wording, so
    the failure shape is consistent across all malformed-URL inputs."""
    import pytest

    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "old",
            "refreshToken": "refresh-secret",
            "tokenUrl": "http://[invalid/token",
        }
    }), encoding="utf-8")

    def opener(request, *, timeout):  # pragma: no cover - must not fire
        raise AssertionError("malformed IPv6 endpoint must not be requested")

    with pytest.raises(oauth_refresh.OAuthRefreshError, match="https://"):
        refresh_claude_config(token_file, opener=opener)


def test_refresh_claude_config_rejects_http_to_loopback_lookalike(
    tmp_path: Path,
) -> None:
    """Defense-in-depth: only the exact loopback aliases bypass the https
    requirement. A hostname that merely *contains* ``localhost`` or sits
    inside the ``127.0.0.0/8`` net but routes off-box must still be
    rejected — the loopback exception is a string-equality check, not a
    substring or CIDR match."""
    import pytest

    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "old",
            "refreshToken": "refresh-secret",
            "tokenUrl": "http://localhost.attacker.example/token",
        }
    }), encoding="utf-8")

    def opener(request, *, timeout):  # pragma: no cover - must not fire
        raise AssertionError("non-loopback http endpoint must not be requested")

    with pytest.raises(oauth_refresh.OAuthRefreshError, match="https://"):
        refresh_claude_config(token_file, opener=opener)


def test_refresh_claude_config_rejects_http_loopback_userinfo_bypass(
    tmp_path: Path,
) -> None:
    """Regression lock for the userinfo-spoofing vector: in
    ``http://127.0.0.1@evil.tld/token`` the authority's *host* is
    ``evil.tld`` (the part before ``@`` is userinfo), so urlopen would
    connect off-box. The loopback check uses ``parsed.hostname`` — the
    same field urlopen dials — so the spoof is rejected. This test exists
    precisely because the code is already correct: it fails closed and
    pins the behavior so a future refactor to substring-match on
    ``netloc`` cannot silently open the bypass."""
    import pytest

    for spoof in (
        "http://127.0.0.1@evil.tld/token",
        "http://localhost@evil.tld/token",
    ):
        token_file = tmp_path / ".claude.json"
        token_file.write_text(json.dumps({
            "oauthAccount": {
                "accessToken": "old",
                "refreshToken": "refresh-secret",
                "tokenUrl": spoof,
            }
        }), encoding="utf-8")

        def opener(request, *, timeout):  # pragma: no cover - must not fire
            raise AssertionError(
                f"userinfo-spoofed endpoint must not be requested: {spoof}"
            )

        with pytest.raises(oauth_refresh.OAuthRefreshError, match="https://"):
            refresh_claude_config(token_file, opener=opener)


# --- BUG-156: bounded request/response body + concurrency ---------------

def test_forward_request_caps_upstream_response_body_at_502() -> None:
    """BUG-156: a mocked upstream returning more bytes than the response
    cap must yield a 502 (with a short error body) instead of returning
    the whole payload to the caller."""
    big = b"x" * (1024 * 1024 + 1)

    def opener(request, *, timeout):
        return FakeResponse(200, big, {"Content-Type": "application/json"})

    resp = forward_request(
        "POST", "/v1/messages", {}, b"{}", MemoryTokenStore(),
        opener=opener, timeout=5.0, response_cap=1024 * 1024,
    )
    assert resp.status == 502
    assert b"exceeded" in resp.body
    assert len(resp.body) < 1024


def test_handler_rejects_oversized_request_body_with_413(
    tmp_path: Path,
) -> None:
    """BUG-156: the handler must refuse a body whose Content-Length
    header exceeds max_request_bytes with 413 instead of buffering it."""
    import io
    from auth_proxy.server import AuthProxyHandler

    class FakeServer:
        auth_proxy_semaphore = None
        auth_proxy_verbose = False

    class FakeStore:
        def access_token(self) -> str: return "tok"
        def refresh(self) -> bool: return False

    handler = AuthProxyHandler.__new__(AuthProxyHandler)
    handler.server = FakeServer()
    handler.rfile = io.BytesIO(b"x" * 5000)
    handler.wfile = io.BytesIO()
    handler.headers = {"Content-Length": "5000"}
    handler.command = "POST"
    handler.path = "/v1/messages"
    handler.client_address = ("127.0.0.1", 0)
    handler.request_version = "HTTP/1.1"
    handler.protocol_version = "HTTP/1.1"
    handler.requestline = "POST / HTTP/1.1"
    handler.token_store = FakeStore()
    handler.upstream_base = "https://api.anthropic.com"
    handler.timeout = 5.0
    handler.max_request_bytes = 1000
    handler.max_response_bytes = 1_000_000

    handler._handle_proxy()
    out = handler.wfile.getvalue()
    assert b"413" in out, f"expected 413 in response, got {out[:200]!r}"


def test_handler_refuses_excess_concurrency_with_503(tmp_path: Path) -> None:
    """BUG-156: the bound semaphore caps concurrent requests; the next
    request beyond the cap gets 503 instead of queuing forever."""
    import io
    import threading as _t
    from auth_proxy.server import AuthProxyHandler

    class FakeServer:
        # Cap of 1, already exhausted to simulate one in-flight request.
        auth_proxy_semaphore = _t.BoundedSemaphore(1)
        auth_proxy_verbose = False

    class FakeStore:
        def access_token(self) -> str: return "tok"
        def refresh(self) -> bool: return False

    server = FakeServer()
    # Hold the only slot.
    server.auth_proxy_semaphore.acquire()

    handler = AuthProxyHandler.__new__(AuthProxyHandler)
    handler.server = server
    handler.rfile = io.BytesIO(b"{}")
    handler.wfile = io.BytesIO()
    handler.headers = {"Content-Length": "2"}
    handler.command = "POST"
    handler.path = "/v1/messages"
    handler.client_address = ("127.0.0.1", 0)
    handler.request_version = "HTTP/1.1"
    handler.protocol_version = "HTTP/1.1"
    handler.requestline = "POST / HTTP/1.1"
    handler.token_store = FakeStore()
    handler.upstream_base = "https://api.anthropic.com"
    handler.timeout = 5.0
    handler.max_request_bytes = 1024
    handler.max_response_bytes = 1024

    handler._handle_proxy()
    out = handler.wfile.getvalue()
    assert b"503" in out, f"expected 503 in response, got {out[:200]!r}"


def test_make_server_honors_env_caps(tmp_path: Path, monkeypatch) -> None:
    """BUG-156: env vars override the default request/response/concurrency
    caps so the operator can tune sidecar limits without code edits."""
    from auth_proxy.server import make_server
    monkeypatch.setenv("AUTH_PROXY_MAX_REQUEST_BYTES", "12345")
    monkeypatch.setenv("AUTH_PROXY_MAX_RESPONSE_BYTES", "67890")
    monkeypatch.setenv("AUTH_PROXY_MAX_CONCURRENT", "3")
    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "t", "refreshToken": "r",
            "tokenUrl": "https://auth.example/token",
        }
    }), encoding="utf-8")
    srv = make_server(
        host="127.0.0.1", port=0,
        token_file=token_file, upstream_base="https://api.example",
    )
    try:
        cls = srv.RequestHandlerClass
        assert cls.max_request_bytes == 12345
        assert cls.max_response_bytes == 67890
        # BoundedSemaphore has no public capacity attr; release once and
        # confirm it raises (i.e. it was at full capacity to begin with).
        sem = srv.auth_proxy_semaphore
        for _ in range(3):
            assert sem.acquire(blocking=False)
        assert not sem.acquire(blocking=False), (
            "cap should have been exhausted after 3 acquires"
        )
    finally:
        srv.server_close()


def test_refresh_access_token_rejects_oversized_response_BUG_158(
    tmp_path: Path,
) -> None:
    """BUG-158: refresh_access_token must enforce a byte cap on the
    OAuth response body. A malicious or misconfigured token endpoint
    that streams an unbounded body would otherwise exhaust the
    auth-proxy sidecar's memory."""
    import pytest

    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "old",
            "refreshToken": "refresh",
            "tokenUrl": "https://auth.example/token",
        }
    }), encoding="utf-8")

    class HugeResponse:
        """Simulates an unbounded token endpoint: a `read(n)` call
        returns exactly `n` bytes, so `response.read()` (no cap) would
        be unbounded. Records the cap used by the caller."""
        def __init__(self) -> None:
            self.status = 200
            self.headers = {}
            self.reads: list[int | None] = []

        def read(self, n: int | None = None):
            self.reads.append(n)
            if n is None:
                # Pretend to oblige and dump a huge chunk
                return b"x" * (200 * 1024 * 1024)
            return b"x" * n

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    huge = HugeResponse()

    def opener(request, *, timeout):
        return huge

    with pytest.raises(oauth_refresh.OAuthRefreshError) as ei:
        refresh_claude_config(token_file, opener=opener)
    # Caller must have passed a bounded read cap; the fallback
    # `read()` (None) leaks unbounded memory.
    assert huge.reads, "read() should have been invoked"
    assert all(n is not None for n in huge.reads), (
        f"refresh_access_token must bound read(); saw {huge.reads}"
    )
    assert "too large" in str(ei.value).lower() or \
        "exceed" in str(ei.value).lower() or \
        "size" in str(ei.value).lower()


def test_token_store_read_serializes_with_bind_mount_rewrite_BUG_167(
    tmp_path: Path, monkeypatch,
) -> None:
    """BUG-167: when refresh_claude_config falls back to in-place
    rewrite (bind-mounted token file), a concurrent
    ClaudeTokenStore.access_token() must NOT see torn JSON written
    mid-rewrite. The store has to serialize reads with the refresh
    write so the fallback path can't expose partial contents."""
    import threading
    from auth_proxy.server import ClaudeTokenStore

    token_file = tmp_path / ".claude.json"
    big_old = "x" * 200_000  # padded so a partial overwrite is detectable
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": big_old,
            "refreshToken": "refresh",
            "tokenUrl": "https://auth.example/token",
        }
    }), encoding="utf-8")

    store = ClaudeTokenStore(token_file)
    seen: list[str] = []
    errors: list[Exception] = []

    def opener(request, *, timeout):
        return FakeResponse(
            200,
            json.dumps({"access_token": "new"}).encode("utf-8"),
        )

    # Force the bind-mount fallback path by making os.replace fail.
    def busy_replace(src, dst):
        raise OSError(errno.EBUSY, "mountpoint is busy")
    monkeypatch.setattr(oauth_refresh.os, "replace", busy_replace)

    # Make the rewrite step pause mid-rewrite so the reader has a
    # window to attempt access_token() while the file is torn. The
    # wrapper still calls the production _rewrite_in_place_from_tmp
    # so its flock(LOCK_EX) is exercised — we just sneak a pause in
    # AFTER the lock is acquired by replacing path.open with a hook.
    real_open = Path.open
    holding = threading.Event()
    release = threading.Event()
    has_paused = {"once": False}

    def hooked_open(self, *a, **kw):
        # Only hook the very first r+b open on the token file (the one
        # _rewrite_in_place_from_tmp issues for the bind-mount path).
        if (
            self == token_file
            and a and a[0] == "r+b"
            and not has_paused["once"]
        ):
            has_paused["once"] = True
            fp = real_open(self, *a, **kw)
            # The production _rewrite_in_place_from_tmp will take a
            # LOCK_EX next; we then yield control to the reader, which
            # should block on its LOCK_SH until we exit the with-block.
            class _Pausing:
                def __init__(self, inner):
                    self.inner = inner
                def __enter__(self):
                    return self
                def __exit__(self, *exc):
                    return self.inner.__exit__(*exc)
                def __getattr__(self, name):
                    return getattr(self.inner, name)
                def write(self, b):
                    # Pause AFTER the first partial write so a buggy
                    # reader without LOCK_SH would see the half-state.
                    if not release.is_set():
                        half = len(b) // 2
                        self.inner.write(b[:half])
                        self.inner.flush()
                        holding.set()
                        release.wait(timeout=5.0)
                        return self.inner.write(b[half:])
                    return self.inner.write(b)
            return _Pausing(fp.__enter__())
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", hooked_open)
    rewrite_holding = holding
    rewrite_release = release

    def reader():
        try:
            rewrite_holding.wait(timeout=5.0)
            for _ in range(3):
                seen.append(store.access_token())
        except Exception as e:
            errors.append(e)
        finally:
            rewrite_release.set()

    t_r = threading.Thread(target=reader)
    t_r.start()
    # Drive the refresh path directly so we can pass opener=opener
    # (ClaudeTokenStore.refresh() uses urlopen default, which hits DNS).
    oauth_refresh.refresh_claude_config(token_file, opener=opener)
    t_r.join(timeout=10.0)
    # Pre-fix: the reader saw the torn file (incomplete JSON) and
    # raised OAuthRefreshError. Post-fix: access_token() takes the
    # refresh lock and returns either the old or new token.
    assert not errors, f"concurrent read saw a torn file: {errors!r}"
    assert all(t in (big_old, "new") for t in seen), seen
    _ = os  # silence unused-import in some checkers


def test_handler_has_short_socket_timeout_BUG_159(tmp_path: Path) -> None:
    """BUG-159: the per-connection socket timeout must be short enough
    to defeat slowloris. The previous 120s timeout left the door open
    for trickle-the-headers attacks. A cap of <= 60s is the contract."""
    from auth_proxy.server import make_server
    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "t", "refreshToken": "r",
            "tokenUrl": "https://auth.example/token",
        }
    }), encoding="utf-8")
    srv = make_server(host="127.0.0.1", port=0, token_file=token_file)
    try:
        cls = srv.RequestHandlerClass
        # `socket_timeout` (or the StreamRequestHandler `timeout`) is what
        # gates header reads. We require a short cap.
        sock_t = getattr(cls, "socket_timeout", None) or cls.timeout
        assert sock_t is not None and sock_t <= 60, (
            f"socket-level timeout must be <= 60s (got {sock_t}) to "
            "defeat slowloris"
        )
    finally:
        srv.server_close()


def test_server_caps_accepted_connections_BUG_159(tmp_path: Path) -> None:
    """BUG-159: even with slow headers, the server-level accept cap
    must refuse new connections beyond the configured maximum. The
    in-handler semaphore is too late; it's only acquired after headers
    parse, leaving slowloris a free path to exhaust thread count."""
    from auth_proxy.server import make_server
    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "t", "refreshToken": "r",
            "tokenUrl": "https://auth.example/token",
        }
    }), encoding="utf-8")
    srv = make_server(
        host="127.0.0.1", port=0, token_file=token_file,
        max_concurrent=2,
    )
    try:
        # The server must expose a thread-acquisition gate that runs
        # at accept time, not inside the handler.
        accept_sem = getattr(srv, "auth_proxy_accept_semaphore", None)
        assert accept_sem is not None, (
            "make_server must attach an accept-time semaphore so "
            "slowloris cannot bypass the in-handler concurrency cap"
        )
        # Cap should match max_concurrent.
        for _ in range(2):
            assert accept_sem.acquire(blocking=False)
        assert not accept_sem.acquire(blocking=False), (
            "accept-time semaphore must enforce the configured cap"
        )
    finally:
        srv.server_close()


def test_refresh_access_token_accepts_normal_response_BUG_158(
    tmp_path: Path,
) -> None:
    """BUG-158 happy path: a normal small JSON response still works."""
    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "old",
            "refreshToken": "refresh",
            "tokenUrl": "https://auth.example/token",
        }
    }), encoding="utf-8")

    def opener(request, *, timeout):
        return FakeResponse(
            200,
            json.dumps({"access_token": "new"}).encode("utf-8"),
        )
    token = refresh_claude_config(token_file, opener=opener)
    assert token == "new"


# --- BUG-180: auth proxy must not forward stale Content-Length --------------

def test_forward_request_strips_caller_content_length_BUG_180() -> None:
    """BUG-180: a caller-supplied Content-Length must NOT be forwarded
    upstream — when the value is stale, urllib will either compute the
    correct one from the body or leave the field absent (so the underlying
    HTTPConnection adds it from len(body)). Today the caller's stale
    value is passed through verbatim, which can tie an upstream worker
    waiting for bytes that never arrive."""
    captured: dict[str, object] = {}

    def opener(request, *, timeout):
        captured["content_length"] = request.get_header("Content-length")
        captured["body"] = request.data
        return FakeResponse(200, b"ok")

    forward_request(
        "POST",
        "/v1/messages",
        # Stale length much larger than the actual body.
        {"Content-Length": "9999"},
        b"short",
        MemoryTokenStore(),
        upstream_base="https://api.anthropic.test",
        opener=opener,
    )
    assert captured["body"] == b"short"
    # After the fix the stale value is dropped: either Content-Length is
    # absent (so urllib's HTTPConnection re-derives it from len(data))
    # or, if some future urllib version eagerly populates it on Request,
    # it matches the real body. The pre-fix code forwards "9999".
    cl = captured["content_length"]
    assert cl in (None, str(len(b"short"))), (
        f"upstream saw stale Content-Length: {cl!r}"
    )


def test_handler_rejects_duplicate_content_length_BUG_180(
    monkeypatch,
) -> None:
    """BUG-180: an inbound request with two conflicting Content-Length
    headers is a smuggling/error condition (RFC 9112 §6.3). The handler
    must reject with 400 instead of accepting the duplicate and forwarding
    it upstream where it ties a worker waiting for the larger of the
    two declared lengths."""
    import io
    from http.client import HTTPMessage
    from auth_proxy import server as auth_proxy_server
    from auth_proxy.server import AuthProxyHandler

    # If the handler reaches forward_request the test should fail loud
    # without doing real network I/O. Replace forward_request with a
    # stub that records the call.
    forwarded = []

    def _stub_forward(*args, **kwargs):
        forwarded.append((args, kwargs))
        from auth_proxy.server import ProxyResponse
        return ProxyResponse(status=200, headers={}, body=b"")
    monkeypatch.setattr(auth_proxy_server, "forward_request", _stub_forward)

    class FakeServer:
        auth_proxy_semaphore = None
        auth_proxy_verbose = False

    class FakeStore:
        def access_token(self) -> str: return "tok"
        def refresh(self) -> bool: return False

    headers = HTTPMessage()
    headers.add_header("Content-Length", "5")
    headers.add_header("Content-Length", "10")

    handler = AuthProxyHandler.__new__(AuthProxyHandler)
    handler.server = FakeServer()
    handler.rfile = io.BytesIO(b"abcde")
    handler.wfile = io.BytesIO()
    handler.headers = headers
    handler.command = "POST"
    handler.path = "/v1/messages"
    handler.client_address = ("127.0.0.1", 0)
    handler.request_version = "HTTP/1.1"
    handler.protocol_version = "HTTP/1.1"
    handler.requestline = "POST / HTTP/1.1"
    handler.token_store = FakeStore()
    handler.upstream_base = "https://api.anthropic.com"
    handler.timeout = 5.0
    handler.max_request_bytes = 1024
    handler.max_response_bytes = 1024

    handler._handle_proxy()
    out = handler.wfile.getvalue()
    assert b"400" in out, (
        f"expected 400 for duplicate Content-Length, got {out[:200]!r}"
    )
    # And the request must NOT have reached upstream.
    assert forwarded == [], (
        f"handler forwarded a duplicate-Content-Length request: {forwarded!r}"
    )
