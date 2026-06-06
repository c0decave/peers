"""Refresh OAuth access tokens stored in Claude-compatible JSON files."""
from __future__ import annotations

import errno
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    import fcntl as _fcntl  # POSIX-only; falls back to no-op on Windows
except ImportError:  # pragma: no cover - non-POSIX
    _fcntl = None  # type: ignore[assignment]


_ACCESS_KEYS = ("accessToken", "access_token", "oauth_token")
_REFRESH_KEYS = ("refreshToken", "refresh_token", "oauth_refresh_token")
_EXPIRES_KEYS = ("expiresAt", "expires_at", "expires")
_TOKEN_URL_KEYS = ("tokenUrl", "token_url", "oauthTokenUrl")
_CLIENT_ID_KEYS = ("clientId", "client_id", "oauthClientId")
_BIND_MOUNT_REWRITE_ERRNOS = {
    errno.EACCES,
    errno.EBUSY,
    errno.EPERM,
    errno.EXDEV,
}


class OAuthRefreshError(RuntimeError):
    """Raised when the sidecar cannot refresh its OAuth token."""
    default_message = "OAuth refresh failed"


@dataclass(frozen=True)
class OAuthConfig:
    access_token: str
    refresh_token: str
    token_url: str | None
    client_id: str | None
    raw: dict[str, Any]
    token_section: dict[str, Any]
    access_key: str
    refresh_key: str
    expires_key: str | None


def _iter_dicts(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _first_string(mapping: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, str] | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return key, value
    return None


def _read_text_with_shared_lock(path: Path) -> str:
    """BUG-167: take fcntl.flock(LOCK_SH) before reading so a concurrent
    in-place rewrite (the bind-mount fallback) cannot expose a torn
    file. The kernel makes the reader wait until the writer's
    LOCK_EX is released."""
    with path.open("rb") as fp:
        if _fcntl is not None:
            try:
                _fcntl.flock(fp.fileno(), _fcntl.LOCK_SH)
            except OSError:
                # Best-effort: if flock isn't supported on this fd
                # (e.g. NFS without lockd), fall through to the read.
                pass
        return fp.read().decode("utf-8")


def load_claude_oauth(path: Path) -> OAuthConfig:
    try:
        raw = json.loads(_read_text_with_shared_lock(path))
    except (OSError, json.JSONDecodeError) as e:
        raise OAuthRefreshError(f"cannot read OAuth token file {path}: {e}") from e
    if not isinstance(raw, dict):
        raise OAuthRefreshError(f"OAuth token file is not a JSON object: {path}")
    for section in _iter_dicts(raw):
        access = _first_string(section, _ACCESS_KEYS)
        refresh = _first_string(section, _REFRESH_KEYS)
        if access is None or refresh is None:
            continue
        token_url = _first_string(section, _TOKEN_URL_KEYS)
        client_id = _first_string(section, _CLIENT_ID_KEYS)
        expires_key = next((key for key in _EXPIRES_KEYS if key in section), None)
        return OAuthConfig(
            access_token=access[1],
            refresh_token=refresh[1],
            token_url=token_url[1] if token_url else None,
            client_id=client_id[1] if client_id else None,
            raw=raw,
            token_section=section,
            access_key=access[0],
            refresh_key=refresh[0],
            expires_key=expires_key,
        )
    raise OAuthRefreshError(
        f"OAuth token file has no access/refresh token pair: {path}"
    )


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_LOOPBACK_OPT_IN_ENV = "AUTH_PROXY_OAUTH_ALLOW_LOOPBACK"
_OAUTH_RESPONSE_MAX_BYTES_DEFAULT = 1 * 1024 * 1024
_OAUTH_RESPONSE_MAX_BYTES_ENV = "AUTH_PROXY_OAUTH_RESPONSE_MAX_BYTES"


def _oauth_response_max_bytes() -> int:
    """BUG-158: bound the OAuth refresh response body. Default 1 MiB;
    typical responses are <1 KiB. Operators can tune via env if a
    provider returns large JWTs."""
    raw = os.environ.get(_OAUTH_RESPONSE_MAX_BYTES_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return _OAUTH_RESPONSE_MAX_BYTES_DEFAULT
        if value > 0:
            return value
    return _OAUTH_RESPONSE_MAX_BYTES_DEFAULT
_HTTPS_REQUIRED_MSG = (
    "OAuth refresh endpoint must be an https:// URL "
    "(http:// to a loopback host requires AUTH_PROXY_OAUTH_ALLOW_LOOPBACK=1)"
)


def _is_loopback_http(parsed: urllib.parse.SplitResult) -> bool:
    """RFC 8252 §7.3: http:// to a loopback host stays on the local machine
    and therefore does not expose the refresh_token over the wire UNTIL the
    process shares its loopback with peer-controlled code. BUG-155: the
    auth-proxy sidecar joins the egress-proxy network namespace alongside
    the main peers container, so loopback there is reachable from the
    workspace. Require an explicit AUTH_PROXY_OAUTH_ALLOW_LOOPBACK=1 opt-in
    (set only in local dev / integration tests) before honoring the
    loopback exception; container deployments leave it unset and loopback
    URLs are refused as if they were any other off-box http URL."""
    if parsed.scheme.lower() != "http":
        return False
    host = parsed.hostname
    if host is None or host.lower() not in _LOOPBACK_HOSTS:
        return False
    opt_in = os.environ.get(_LOOPBACK_OPT_IN_ENV, "").strip().lower()
    return opt_in in ("1", "true", "yes")


def _token_url(config: OAuthConfig) -> str:
    url = os.environ.get("AUTH_PROXY_OAUTH_TOKEN_URL") or config.token_url
    if not url:
        raise OAuthRefreshError(
            "OAuth refresh endpoint is not configured; set "
            "AUTH_PROXY_OAUTH_TOKEN_URL or tokenUrl in the token file"
        )
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        # urlsplit raises ValueError on malformed IPv6 bracket pairs
        # (e.g. ``http://[invalid/token``). Surface this as the same
        # OAuthRefreshError every other bad-URL path produces so the
        # proxy returns a structured 502, not an opaque 500.
        raise OAuthRefreshError(_HTTPS_REQUIRED_MSG) from None
    scheme = parsed.scheme.lower()
    if not parsed.netloc:
        raise OAuthRefreshError(_HTTPS_REQUIRED_MSG)
    if scheme == "https":
        return url
    if _is_loopback_http(parsed):
        return url
    raise OAuthRefreshError(_HTTPS_REQUIRED_MSG)


def refresh_access_token(
    config: OAuthConfig,
    *,
    opener: Callable = urllib.request.urlopen,
    timeout: float = 30.0,
) -> dict[str, Any]:
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": config.refresh_token,
    }
    if config.client_id:
        payload["client_id"] = config.client_id
    body = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        _token_url(config),
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    cap = _oauth_response_max_bytes()
    try:
        with opener(request, timeout=timeout) as response:
            # ask for one byte over the cap; if the body fills
            # that, the response was too large and we refuse rather
            # than parse a partial body or buffer unbounded memory.
            data = response.read(cap + 1)
    except Exception as e:
        raise OAuthRefreshError(f"OAuth refresh request failed: {e}") from e
    if len(data) > cap:
        raise OAuthRefreshError(
            f"OAuth refresh response too large (> {cap} bytes); "
            "set AUTH_PROXY_OAUTH_RESPONSE_MAX_BYTES to raise the cap"
        )
    try:
        refreshed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise OAuthRefreshError(f"OAuth refresh returned invalid JSON: {e}") from e
    if not isinstance(refreshed, dict):
        raise OAuthRefreshError("OAuth refresh response is not a JSON object")
    access = refreshed.get("access_token") or refreshed.get("accessToken")
    if not isinstance(access, str) or not access:
        raise OAuthRefreshError("OAuth refresh response has no access token")
    return refreshed


def _rewrite_in_place_from_tmp(tmp: Path, path: Path) -> None:
    data = tmp.read_bytes()
    with path.open("r+b") as fp:
        # take fcntl.flock(LOCK_EX) before the seek/write/
        # truncate window so concurrent readers (LOCK_SH in
        # _read_text_with_shared_lock) wait until the rewrite is
        # durable instead of seeing a torn file.
        if _fcntl is not None:
            try:
                _fcntl.flock(fp.fileno(), _fcntl.LOCK_EX)
            except OSError:
                pass
        fp.seek(0)
        fp.write(data)
        fp.truncate()
        fp.flush()
        os.fsync(fp.fileno())
        if _fcntl is not None:
            try:
                _fcntl.flock(fp.fileno(), _fcntl.LOCK_UN)
            except OSError:
                pass
    try:
        path.chmod(0o600)
    except OSError:
        pass
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass


def _replace_or_rewrite_token_file(tmp: Path, path: Path) -> None:
    try:
        os.replace(tmp, path)
        return
    except OSError as e:
        if e.errno not in _BIND_MOUNT_REWRITE_ERRNOS:
            raise OAuthRefreshError(
                f"cannot replace OAuth token file {path}: {e}"
            ) from e
        try:
            _rewrite_in_place_from_tmp(tmp, path)
        except OSError as rewrite_error:
            raise OAuthRefreshError(
                f"cannot rewrite OAuth token file {path}: {rewrite_error}"
            ) from rewrite_error


def refresh_claude_config(
    path: Path,
    *,
    opener: Callable = urllib.request.urlopen,
    now: Callable[[], float] = time.time,
) -> str:
    config = load_claude_oauth(path)
    refreshed = refresh_access_token(config, opener=opener)
    section = config.token_section
    access = refreshed.get("access_token") or refreshed.get("accessToken")
    refresh = refreshed.get("refresh_token") or refreshed.get("refreshToken")
    section[config.access_key] = access
    if isinstance(refresh, str) and refresh:
        section[config.refresh_key] = refresh
    expires_in = refreshed.get("expires_in") or refreshed.get("expiresIn")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        section[config.expires_key or "expires_at"] = int(now() + expires_in)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(config.raw, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        raise OAuthRefreshError(
            f"cannot write OAuth token tmp file {tmp}: {e}"
        ) from e
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    _replace_or_rewrite_token_file(tmp, path)
    return str(access)
