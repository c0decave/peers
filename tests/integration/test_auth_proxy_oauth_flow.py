"""End-to-end local auth-proxy OAuth refresh flow."""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from auth_proxy.server import make_server


def _serve(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def test_auth_proxy_refreshes_on_401_and_retries(tmp_path):
    seen: list[tuple[str, str | None]] = []

    class UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            self.rfile.read(length)
            if self.path == "/oauth/token":
                seen.append(("refresh", None))
                body = json.dumps({
                    "access_token": "new-token",
                    "refresh_token": "refresh-token-2",
                    "expires_in": 60,
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            seen.append(("api", self.headers.get("Authorization")))
            if self.headers.get("Authorization") == "Bearer old-token":
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"expired")
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, fmt: str, *args) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    _serve(upstream)
    upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
    token_file = tmp_path / ".claude.json"
    token_file.write_text(json.dumps({
        "oauthAccount": {
            "accessToken": "old-token",
            "refreshToken": "refresh-token",
            "tokenUrl": f"{upstream_url}/oauth/token",
            "expiresAt": 1,
        }
    }), encoding="utf-8")
    proxy = make_server(
        host="127.0.0.1",
        port=0,
        token_file=token_file,
        upstream_base=upstream_url,
    )
    _serve(proxy)
    proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
    try:
        request = urllib.request.Request(
            f"{proxy_url}/v1/messages",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer caller-token"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert response.read() == b"ok"
    finally:
        proxy.shutdown()
        upstream.shutdown()

    assert seen == [
        ("api", "Bearer old-token"),
        ("refresh", None),
        ("api", "Bearer new-token"),
    ]
    data = json.loads(token_file.read_text(encoding="utf-8"))
    assert data["oauthAccount"]["accessToken"] == "new-token"
    assert data["oauthAccount"]["refreshToken"] == "refresh-token-2"
