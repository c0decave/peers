"""Opt-in, allowlisted, fail-closed web fetcher for the research ``web`` modality.

The :class:`peers.research.adapters.CodebaseSweeper` runs its ``web`` modality only
when an injected ``web_search`` + ``fetch`` pair is supplied; the ``peers research``
CLI wired neither, so the modality was inert and codebase-only research stayed
honestly dry (a URL-cited report cannot be written from ``git grep`` alone). This
module supplies the two collaborators as a SAFE, opt-in capability:

* :class:`AllowlistedFetcher` — ``fetch(url) -> (bytes, origin) | None`` over an
  INJECTED transport (the network call is the seam, so unit tests never touch the
  network). It is DENY-BY-DEFAULT: a host must match the operator's anchored
  allow-list, the scheme must be http(s), and the host must not be loopback /
  private / link-local (SSRF defense-in-depth, so an allow-matching metadata or
  internal target is still refused). A blocked/failed/oversize/non-2xx fetch
  returns ``None`` -> the sweeper records an honest ``access_failure``, never a
  fabricated witness.
* :func:`make_seed_url_search` — a ``web_search(question) -> [url]`` that returns
  the operator's configured seed URLs (the operator points research at the sources
  to corroborate; this needs no third-party search-engine API/credentials).

The real network transport (:func:`urllib_transport`) is stdlib ``urllib`` and
routes through an optional proxy (the existing egress proxy when configured). It is
only ever activated when the operator explicitly enables ``research.web`` in
``.peers/config.yaml`` — deny-by-default, never on by default.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

#: ``transport(url) -> (status, body, final_url)``; raises on a network error.
Transport = Callable[[str], "tuple[int, bytes, str]"]

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB cap on a fetched body
_DEFAULT_TIMEOUT_S = 20.0

# A host is SAFE only when it is a DNS FQDN with an ALPHABETIC top-level label.
# This single rule is the SSRF guard (S4 review): it refuses `localhost`, EVERY IP
# literal (canonical loopback/private/public AND the decimal/octal/hex/short-form
# encodings that defeat a strict ipaddress() check yet resolve to loopback/cloud-
# metadata — 2130706433, 0x7f.0.0.1, 127.1, 2852039166, ...), IPv6 literals, and
# trailing-dot/embedded tricks. Research sources are domains; a raw-IP source is
# refused (use the domain). We do NOT resolve the name (a resolve is a network
# action + a TOCTOU surface); the FQDN rule + the allow-list + redirects-disabled
# are the layered controls.
_FQDN_RE = re.compile(
    r"(?=.{1,253}\Z)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}",
    re.IGNORECASE,
)


def _host_is_safe(host: str) -> bool:
    """True only for a DNS FQDN with an alphabetic TLD (see :data:`_FQDN_RE`).

    A trailing dot is refused (it is an IP-confusion / parser-differential vector
    here, not worth allowing for a research source)."""
    h = (host or "").strip().lower()
    if not h or h.endswith("."):
        return False
    return bool(_FQDN_RE.fullmatch(h))


class AllowlistedFetcher:
    """Deny-by-default web fetcher. See module docstring for the contract."""

    def __init__(
        self,
        *,
        allow: Iterable[str],
        transport: Transport,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        # Compile the host allow-list up front so a bad regex fails LOUDLY at
        # construction, not as a silent never-match at fetch time.
        self._allow = [re.compile(pattern) for pattern in allow]
        self._transport = transport
        if not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive int")
        self._max_bytes = max_bytes

    def _host_ok(self, host: str) -> bool:
        # Safe (DNS FQDN, no IP literal/encoding) AND allow-listed. fullmatch on the
        # allow entries means `example\.com` never matches `example.com.evil.test`
        # (a substring/suffix smuggle). Deny by default (empty allow -> no match).
        return _host_is_safe(host) and any(rx.fullmatch(host) for rx in self._allow)

    def _url_host_ok(self, url: str) -> bool:
        if not isinstance(url, str) or not url:
            return False
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        if parts.scheme not in ("http", "https"):
            return False
        return self._host_ok((parts.hostname or "").lower())

    def fetch(self, url: str) -> "tuple[bytes, str] | None":
        """Return ``(body, resolved_origin)`` or ``None`` (a recorded non-fetch)."""
        if not self._url_host_ok(url):
            return None
        try:
            status, body, final_url = self._transport(url)
        except Exception:  # noqa: BLE001 — any transport error is a recorded non-fetch
            return None
        if not isinstance(status, int) or not (200 <= status < 300):
            return None
        if not isinstance(body, bytes) or len(body) > self._max_bytes:
            return None
        resolved = final_url if isinstance(final_url, str) and final_url else url
        # S4 review (CRITICAL): re-validate the FINAL landed URL's host+scheme — a
        # transport that followed a redirect (or any transport) must not land us on a
        # private/metadata/unlisted host. Belt-and-suspenders atop the transport
        # disabling redirect-following: a forbidden landing is a non-fetch, not a
        # leaked body.
        if not self._url_host_ok(resolved):
            return None
        return (body, resolved)


def make_seed_url_search(seeds: Iterable[str]) -> Callable[[str], list[str]]:
    """A ``web_search(question) -> [url]`` returning the operator's seed URLs.

    The seeds are the sources the operator scopes research to; the same set is
    returned for every sub-question (the fetcher + the claim ledger's >= 2
    independent-witness rule do the corroboration). Returns a fresh list each call
    so a caller cannot mutate the shared seed set.
    """
    frozen = [s for s in seeds if isinstance(s, str) and s.strip()]
    return lambda _question: list(frozen)


def urllib_transport(
    *, proxy: str | None = None, timeout: float = _DEFAULT_TIMEOUT_S,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> Transport:
    """A real stdlib-``urllib`` transport, optionally via ``proxy`` (the egress
    proxy). Used ONLY when the operator enables ``research.web`` in config; kept a
    thin seam so :class:`AllowlistedFetcher`'s guards are the unit-tested logic.

    Returns ``(status, body, final_url)``. Redirects are NOT followed (S4 review:
    an unchecked 3xx is the SSRF vector — a legit allow-listed domain could redirect
    to a private/metadata host); a 3xx surfaces as its non-2xx status so the fetcher
    returns None. Other HTTP errors surface their code; network errors raise (the
    fetcher maps both to None).
    """
    import urllib.error
    import urllib.request

    class _NoFollowRedirects(urllib.request.HTTPRedirectHandler):
        # Never auto-follow a redirect: returning None makes urllib raise an
        # HTTPError for the 3xx, which we surface as a non-2xx status below.
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    handlers: list = [_NoFollowRedirects()]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        # Empty ProxyHandler disables environment-proxy auto-detection so the
        # caller's intent (no proxy) is explicit, not silently overridden by env.
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)

    def _transport(url: str) -> "tuple[int, bytes, str]":
        req = urllib.request.Request(url, method="GET")
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read(max_bytes + 1)
                status = getattr(resp, "status", None) or resp.getcode()
                final_url = resp.geturl()
        except urllib.error.HTTPError as e:
            # A non-followed 3xx, or a 4xx/5xx: surface the code (non-2xx) + no body
            # so the fetcher records an honest non-fetch, never a redirected body.
            return (int(getattr(e, "code", 0) or 0), b"", url)
        return (int(status), body, final_url)

    return _transport
