"""Small stdlib HTTP proxy that injects Claude OAuth bearer tokens."""
from __future__ import annotations

import argparse
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Mapping

from auth_proxy.oauth_refresh import OAuthRefreshError, load_claude_oauth
from auth_proxy.oauth_refresh import refresh_claude_config


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_DROP_REQUEST_HEADERS = _HOP_BY_HOP_HEADERS | {"host", "authorization"}
_DROP_RESPONSE_HEADERS = _HOP_BY_HOP_HEADERS | {"content-length"}


@dataclass(frozen=True)
class ProxyResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class ClaudeTokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._refresh_lock = threading.Lock()

    def access_token(self) -> str:
        return load_claude_oauth(self.path).access_token

    def refresh(self) -> bool:
        with self._refresh_lock:
            try:
                refresh_claude_config(self.path)
            except OAuthRefreshError:
                return False
            return True


def _clean_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _DROP_REQUEST_HEADERS
    }


def _clean_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _DROP_RESPONSE_HEADERS
    }


def _upstream_url(upstream_base: str, path: str) -> str:
    base = upstream_base.rstrip("/")
    parsed = urllib.parse.urlsplit(path)
    target_path = parsed.path if parsed.path.startswith("/") else f"/{parsed.path}"
    return urllib.parse.urlunsplit((
        urllib.parse.urlsplit(base).scheme,
        urllib.parse.urlsplit(base).netloc,
        target_path,
        parsed.query,
        "",
    ))


def _send_once(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    opener: Callable,
    timeout: float,
) -> ProxyResponse:
    request = urllib.request.Request(
        url,
        data=body if body else None,
        method=method,
        headers=dict(headers),
    )
    try:
        with opener(request, timeout=timeout) as response:
            return ProxyResponse(
                status=int(response.status),
                headers=_clean_response_headers(dict(response.headers)),
                body=response.read(),
            )
    except urllib.error.HTTPError as e:
        return ProxyResponse(
            status=int(e.code),
            headers=_clean_response_headers(dict(e.headers)),
            body=e.read(),
        )
    except urllib.error.URLError as e:
        message = f"upstream request failed: {e.reason}".encode(
            "utf-8", errors="replace",
        )
        return ProxyResponse(
            status=502,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            body=message,
        )
    except TimeoutError as e:
        message = f"upstream request timed out: {e}".encode(
            "utf-8", errors="replace",
        )
        return ProxyResponse(
            status=502,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            body=message,
        )


def forward_request(
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    token_store: ClaudeTokenStore,
    *,
    upstream_base: str = "https://api.anthropic.com",
    opener: Callable = urllib.request.urlopen,
    timeout: float = 120.0,
) -> ProxyResponse:
    clean_headers = _clean_request_headers(headers)
    clean_headers["Authorization"] = f"Bearer {token_store.access_token()}"
    url = _upstream_url(upstream_base, path)
    first = _send_once(
        method, url, clean_headers, body, opener=opener, timeout=timeout,
    )
    if first.status != 401 or not token_store.refresh():
        return first
    clean_headers["Authorization"] = f"Bearer {token_store.access_token()}"
    return _send_once(
        method, url, clean_headers, body, opener=opener, timeout=timeout,
    )


class AuthProxyHandler(BaseHTTPRequestHandler):
    token_store: ClaudeTokenStore
    upstream_base = "https://api.anthropic.com"
    timeout = 120.0

    def _handle_proxy(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self.send_error(400, "invalid Content-Length")
            return
        if length < 0:
            self.send_error(400, "invalid Content-Length")
            return
        body = self.rfile.read(length) if length > 0 else b""
        try:
            response = forward_request(
                self.command,
                self.path,
                self.headers,
                body,
                self.token_store,
                upstream_base=self.upstream_base,
                timeout=self.timeout,
            )
        except OAuthRefreshError as e:
            message = str(e).encode("utf-8", errors="replace")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)
            return
        self.send_response(response.status)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        self.wfile.write(response.body)

    def do_GET(self) -> None:
        self._handle_proxy()

    def do_POST(self) -> None:
        self._handle_proxy()

    def do_PUT(self) -> None:
        self._handle_proxy()

    def do_PATCH(self) -> None:
        self._handle_proxy()

    def do_DELETE(self) -> None:
        self._handle_proxy()

    def log_message(self, fmt: str, *args) -> None:
        if getattr(self.server, "auth_proxy_verbose", False):
            super().log_message(fmt, *args)


def make_server(
    *,
    host: str,
    port: int,
    token_file: Path,
    upstream_base: str = "https://api.anthropic.com",
) -> ThreadingHTTPServer:
    class BoundAuthProxyHandler(AuthProxyHandler):
        bound_handler = True

    BoundAuthProxyHandler.token_store = ClaudeTokenStore(token_file)
    BoundAuthProxyHandler.upstream_base = upstream_base

    return ThreadingHTTPServer((host, port), BoundAuthProxyHandler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="auth-proxy",
        description="Inject Claude OAuth bearer tokens into Anthropic API calls.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--token-file", type=Path,
        default=Path("/auth/.claude.json"),
        help="Claude OAuth JSON mounted into the sidecar",
    )
    parser.add_argument(
        "--upstream-base", default="https://api.anthropic.com",
        help="Anthropic API base URL, testable with a local fake server",
    )
    args = parser.parse_args(argv)
    server = make_server(
        host=args.host,
        port=args.port,
        token_file=args.token_file,
        upstream_base=args.upstream_base,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
