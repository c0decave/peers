"""Auth-proxy request forwarding and token-refresh tests."""
from __future__ import annotations

import io
import errno
import json
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

    def read(self) -> bytes:
        return self._body

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
    tmp_path: Path,
) -> None:
    """RFC 8252 §7.3 — http:// to a loopback host stays on the local
    machine, so it does not exfiltrate the refresh_token. The exception is
    required for local dev / integration tests; the BUG-203 protection
    remains for any non-loopback http URL."""
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
    tmp_path: Path,
) -> None:
    """The localhost hostname (case-insensitive) is also a loopback alias."""
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
