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


def load_claude_oauth(path: Path) -> OAuthConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
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


def _token_url(config: OAuthConfig) -> str:
    url = os.environ.get("AUTH_PROXY_OAUTH_TOKEN_URL") or config.token_url
    if not url:
        raise OAuthRefreshError(
            "OAuth refresh endpoint is not configured; set "
            "AUTH_PROXY_OAUTH_TOKEN_URL or tokenUrl in the token file"
        )
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise OAuthRefreshError(
            "OAuth refresh endpoint must be an https:// URL"
        )
    return url


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
    try:
        with opener(request, timeout=timeout) as response:
            data = response.read()
    except Exception as e:
        raise OAuthRefreshError(f"OAuth refresh request failed: {e}") from e
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
        fp.seek(0)
        fp.write(data)
        fp.truncate()
        fp.flush()
        os.fsync(fp.fileno())
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
