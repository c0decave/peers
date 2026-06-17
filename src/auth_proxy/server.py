"""Small stdlib HTTP proxy that injects Claude OAuth bearer tokens."""
from __future__ import annotations

import argparse
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

from auth_proxy.oauth_refresh import OAuthRefreshError, load_claude_oauth
from auth_proxy.oauth_refresh import refresh_claude_config

# bound request body, response body, and concurrent connections
# so a peer-controlled workspace cannot exhaust sidecar memory or thread
# count via a huge POST body, a mocked upstream that returns gigabytes,
# or a fan-out of slow connections.
_DEFAULT_MAX_REQUEST_BYTES = 16 * 1024 * 1024   # 16 MiB
_DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024  # 32 MiB
_DEFAULT_MAX_CONCURRENT = 16
_REQUEST_BYTES_ENV = "AUTH_PROXY_MAX_REQUEST_BYTES"
_RESPONSE_BYTES_ENV = "AUTH_PROXY_MAX_RESPONSE_BYTES"
_CONCURRENT_ENV = "AUTH_PROXY_MAX_CONCURRENT"
# bound the socket-level read timeout so slowloris-style
# trickle-the-headers cannot hold a request handler thread for the
# entire upstream timeout (120 s). The upstream forwarder uses a
# separate, longer timeout passed via AuthProxyHandler.timeout-not.
_DEFAULT_SOCKET_TIMEOUT_SECONDS = 30.0
_SOCKET_TIMEOUT_ENV = "AUTH_PROXY_SOCKET_TIMEOUT_SECONDS"


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if v > 0 else default


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if v > 0 else default


class _ResponseTooLarge(RuntimeError):
    """Upstream / OAuth-error body exceeded the configured response cap."""


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
# strip Content-Length from forwarded request headers so urllib
# computes it from the actual body. Otherwise a duplicate or truncated
# body produces a body shorter than the declared length and ties an
# upstream worker waiting for the missing bytes until the upstream
# timeout fires.
_DROP_REQUEST_HEADERS = _HOP_BY_HOP_HEADERS | {
    "host", "authorization", "content-length",
}
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


def _header_parts(headers: Any, name: str) -> list[str]:
    values = (
        headers.get_all(name)
        if hasattr(headers, "get_all") else None
    )
    if values is None:
        raw = headers.get(name)
        values = [raw] if raw is not None else []
    return [part.strip() for value in values for part in str(value).split(",")]


def _content_length_parts(headers: Any) -> list[str]:
    return _header_parts(headers, "Content-Length")


def _request_transfer_encoding_error(headers: Any) -> str | None:
    # BaseHTTPRequestHandler does not decode chunked request bodies.
    # Any Transfer-Encoding present means the on-the-wire body would be read as
    # zero bytes (no Content-Length) and a truncated/empty request forwarded
    # upstream, so reject it up front rather than silently drop the body.
    parts = [part.lower() for part in _header_parts(headers, "Transfer-Encoding")]
    if any(part for part in parts):
        return "unsupported Transfer-Encoding"
    return None


def _parse_content_length(headers: Any) -> tuple[int, str | None]:
    parts = _content_length_parts(headers)
    if not parts:
        return 0, None
    if len(set(parts)) > 1:
        return 0, "conflicting Content-Length"
    try:
        length = int(parts[0])
    except ValueError:
        return 0, "invalid Content-Length"
    if length < 0:
        return 0, "invalid Content-Length"
    return length, None


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


def _read_bounded(stream, cap: int) -> bytes:
    """Read up to ``cap`` bytes from ``stream``; raise _ResponseTooLarge
    if more are available."""
    data = stream.read(cap + 1)
    if len(data) > cap:
        raise _ResponseTooLarge(
            f"upstream response exceeded {cap} bytes cap"
        )
    return data


def _send_once(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    opener: Callable,
    timeout: float,
    response_cap: int = _DEFAULT_MAX_RESPONSE_BYTES,
) -> ProxyResponse:
    request = urllib.request.Request(
        url,
        data=body if body else None,
        method=method,
        headers=dict(headers),
    )
    try:
        with opener(request, timeout=timeout) as response:
            try:
                payload = _read_bounded(response, response_cap)
            except _ResponseTooLarge as e:
                return ProxyResponse(
                    status=502,
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                    body=str(e).encode("utf-8", errors="replace"),
                )
            return ProxyResponse(
                status=int(response.status),
                headers=_clean_response_headers(dict(response.headers)),
                body=payload,
            )
    except urllib.error.HTTPError as e:
        try:
            err_body = _read_bounded(e, response_cap)
        except _ResponseTooLarge:
            err_body = b"upstream error body exceeded response cap"
        return ProxyResponse(
            status=int(e.code),
            headers=_clean_response_headers(dict(e.headers)),
            body=err_body,
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
    response_cap: int = _DEFAULT_MAX_RESPONSE_BYTES,
) -> ProxyResponse:
    clean_headers = _clean_request_headers(headers)
    clean_headers["Authorization"] = f"Bearer {token_store.access_token()}"
    url = _upstream_url(upstream_base, path)
    first = _send_once(
        method, url, clean_headers, body, opener=opener, timeout=timeout,
        response_cap=response_cap,
    )
    if first.status != 401 or not token_store.refresh():
        return first
    clean_headers["Authorization"] = f"Bearer {token_store.access_token()}"
    return _send_once(
        method, url, clean_headers, body, opener=opener, timeout=timeout,
        response_cap=response_cap,
    )


class AuthProxyHandler(BaseHTTPRequestHandler):
    token_store: ClaudeTokenStore
    upstream_base = "https://api.anthropic.com"
    # `timeout` is what StreamRequestHandler.setup() pushes
    # onto self.connection.settimeout(), so it caps every rfile
    # read (headers + body). Keep it short to defeat slowloris;
    # the upstream forwarder uses `upstream_timeout` separately.
    timeout = _DEFAULT_SOCKET_TIMEOUT_SECONDS
    upstream_timeout = 120.0
    max_request_bytes = _DEFAULT_MAX_REQUEST_BYTES
    max_response_bytes = _DEFAULT_MAX_RESPONSE_BYTES

    def _handle_proxy(self) -> None:
        # cap concurrent in-flight requests so a peer cannot fan
        # out enough connections to exhaust the sidecar's thread / FD
        # budget. Semaphore is shared across all threads of the bound
        # server class; if it can't be acquired without blocking we fail
        # fast with 503 rather than queue indefinitely.
        sem: threading.BoundedSemaphore | None = getattr(
            self.server, "auth_proxy_semaphore", None,
        )
        acquired = False
        if sem is not None:
            acquired = sem.acquire(blocking=False)
            if not acquired:
                self.send_error(503, "auth proxy concurrency cap reached")
                return
        try:
            # reject chunked/transfer-encoded request bodies the
            # handler cannot decode (they would forward empty/truncated).
            transfer_error = _request_transfer_encoding_error(self.headers)
            if transfer_error is not None:
                self.send_error(501, transfer_error)
                return
            # a request that arrives with two conflicting
            # Content-Length values is an HTTP smuggling / framing error
            # (RFC 9112 §6.3). self.headers is an HTTPMessage and
            # get_all() reveals duplicates; dict-like fallback (test
            # harnesses) just returns the single value.
            length, length_error = _parse_content_length(self.headers)
            if length_error is not None:
                self.send_error(400, length_error)
                return
            if length > self.max_request_bytes:
                self.send_error(
                    413,
                    f"request body exceeds {self.max_request_bytes}-byte cap",
                )
                return
            # even with a sane Content-Length header, the actual
            # bytes on the wire could exceed it. self.rfile.read(length)
            # already caps by length, so the header check above is
            # sufficient — but we re-bound the read with an explicit cap
            # in case length is set to a huge legal value (e.g. exactly
            # max_request_bytes) we still don't want to allocate beyond.
            body = (
                self.rfile.read(min(length, self.max_request_bytes))
                if length > 0 else b""
            )
            try:
                response = forward_request(
                    self.command,
                    self.path,
                    dict(self.headers.items()),
                    body,
                    self.token_store,
                    upstream_base=self.upstream_base,
                    timeout=self.upstream_timeout,
                    response_cap=self.max_response_bytes,
                )
            except OAuthRefreshError as e:
                message = str(e).encode("utf-8", errors="replace")
                # Bound the OAuth error message too — exception strings
                # can include the upstream body in obscure error paths.
                if len(message) > self.max_response_bytes:
                    message = message[:self.max_response_bytes]
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
        finally:
            if acquired and sem is not None:
                sem.release()

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


class _AuthProxyThreadingHTTPServer(ThreadingHTTPServer):
    """BUG-159: enforce the concurrency cap at accept time so
    slowloris-style attackers cannot hold one thread per stalled
    connection before the in-handler semaphore would gate them.

    `process_request` runs on the accept thread; we acquire the
    accept semaphore non-blocking and refuse the new connection
    when the cap is full. The slot is released at the end of the
    spawned handler thread.
    """

    daemon_threads = True
    # Set on the instance by ``serve()``; declared here so the assignments and
    # the getattr(...) reads type-check. Bare annotations create no class
    # attribute, so the getattr(..., None) fallbacks are unchanged at runtime.
    auth_proxy_semaphore: threading.BoundedSemaphore | None
    auth_proxy_accept_semaphore: threading.BoundedSemaphore | None

    @staticmethod
    def _release_accept_slot(
        sem: threading.BoundedSemaphore | None,
    ) -> None:
        if sem is None:
            return
        try:
            sem.release()
        except ValueError:
            # BoundedSemaphore release > capacity raises; swallow so a
            # defensive release in an exceptional path cannot crash the
            # accept loop.
            pass

    def _reject_request(self, request) -> None:
        try:
            request.sendall(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Connection: close\r\n"
                b"Content-Length: 0\r\n\r\n",
            )
        except OSError:
            pass
        self.shutdown_request(request)

    def process_request(self, request, client_address):  # type: ignore[override]
        sem = getattr(self, "auth_proxy_accept_semaphore", None)
        acquired = False
        if sem is not None and not sem.acquire(blocking=False):
            self._reject_request(request)
            return
        acquired = sem is not None
        # BUG-505 (v22 harvest): if worker-thread spawn (or any pre-thread
        # step) fails AFTER we took the accept slot, release it here — else
        # the slot leaks and the accept loop wedges after `max_concurrent`
        # such failures. process_request_thread's finally only runs once the
        # thread actually started.
        try:
            super().process_request(request, client_address)
        except Exception:
            if acquired:
                self._release_accept_slot(sem)
                self.handle_error(request, client_address)
                self._reject_request(request)
                return
            raise

    def process_request_thread(self, request, client_address):  # type: ignore[override]
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_accept_slot(
                getattr(self, "auth_proxy_accept_semaphore", None),
            )


def make_server(
    *,
    host: str,
    port: int,
    token_file: Path,
    upstream_base: str = "https://api.anthropic.com",
    max_request_bytes: int | None = None,
    max_response_bytes: int | None = None,
    max_concurrent: int | None = None,
    socket_timeout: float | None = None,
) -> ThreadingHTTPServer:
    class BoundAuthProxyHandler(AuthProxyHandler):
        bound_handler = True

    BoundAuthProxyHandler.token_store = ClaudeTokenStore(token_file)
    BoundAuthProxyHandler.upstream_base = upstream_base
    if max_request_bytes is None:
        max_request_bytes = _positive_int_env(
            _REQUEST_BYTES_ENV, _DEFAULT_MAX_REQUEST_BYTES,
        )
    if max_response_bytes is None:
        max_response_bytes = _positive_int_env(
            _RESPONSE_BYTES_ENV, _DEFAULT_MAX_RESPONSE_BYTES,
        )
    if max_concurrent is None:
        max_concurrent = _positive_int_env(
            _CONCURRENT_ENV, _DEFAULT_MAX_CONCURRENT,
        )
    if socket_timeout is None:
        socket_timeout = _positive_float_env(
            _SOCKET_TIMEOUT_ENV, _DEFAULT_SOCKET_TIMEOUT_SECONDS,
        )
    BoundAuthProxyHandler.max_request_bytes = max_request_bytes
    BoundAuthProxyHandler.max_response_bytes = max_response_bytes
    BoundAuthProxyHandler.timeout = socket_timeout

    srv = _AuthProxyThreadingHTTPServer((host, port), BoundAuthProxyHandler)
    # In-handler cap — protects against slow-handler exhaustion
    # AFTER headers parse; refuses with 503 from within _handle_proxy.
    srv.auth_proxy_semaphore = threading.BoundedSemaphore(max_concurrent)
    # Accept-time cap — protects against slowloris by refusing
    # new connections at the accept thread BEFORE a worker is spawned.
    srv.auth_proxy_accept_semaphore = threading.BoundedSemaphore(max_concurrent)
    return srv


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
