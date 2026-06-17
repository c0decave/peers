#!/bin/sh
# peers-egress-proxy entrypoint.
#
# Two jobs, in order:
#   1. Materialise the runtime tinyproxy host allow-list (base filter +
#      operator-declared PEERS_EGRESS_EXTRA_HOSTS).
#   2. Install an in-netns firewall lockdown that FORCES all egress through
#      tinyproxy (uid 100). The main peers container joins this network
#      namespace via `--network=container:<proxy>`; without the lockdown that
#      netns has a working default route, so an agent could simply
#      `unset HTTP_PROXY` and reach arbitrary hosts directly, bypassing the
#      allow-list entirely. The lockdown is
#      FAIL-CLOSED: if it cannot be installed, the proxy refuses to start
#      rather than run with open egress.
#
# Paths and the tinyproxy uid are overridable via env for hermetic unit tests
# (tests/unit/test_egress_lockdown.py); the defaults are the in-image paths.
set -eu

base_filter="${PEERS_EGRESS_BASE_FILTER:-/etc/tinyproxy/filter}"
runtime_filter="${PEERS_EGRESS_RUNTIME_FILTER:-/tmp/tinyproxy-filter}"
tinyproxy_uid="${PEERS_EGRESS_TINYPROXY_UID:-100}"

cp "$base_filter" "$runtime_filter"

if [ "${PEERS_EGRESS_EXTRA_HOSTS:-}" ]; then
  printf '\n# Runtime project-specific allow-list additions.\n' >> "$runtime_filter"
  # Append a trailing newline so the final entry is not dropped: POSIX `read`
  # returns non-zero (and the loop body is skipped) for a last line with no
  # terminating newline. That EOF quirk silently truncated the allow-list and
  # 403'd the operator's last allowlisted host.
  printf '%s\n' "$PEERS_EGRESS_EXTRA_HOSTS" | tr ',' '\n' | while IFS= read -r host_re; do
    [ "$host_re" ] || continue
    printf '%s\n' "$host_re" >> "$runtime_filter"
  done
fi

# --- Egress lockdown: default-DROP OUTPUT; allow only loopback (agent ->
# 127.0.0.1:3128 / agent -> auth-proxy) and tinyproxy's own upstream fetches
# (uid 100). Applied to BOTH IPv4 and IPv6 so a v6 route cannot be used to
# bypass. Default-deny is set FIRST so a partial failure stays closed.
_lockdown() {
  fw="$1"
  "$fw" -P OUTPUT DROP || return 1
  "$fw" -A OUTPUT -o lo -j ACCEPT || return 1
  "$fw" -A OUTPUT -m owner --uid-owner "$tinyproxy_uid" -j ACCEPT || return 1
}

if ! _lockdown iptables; then
  echo "peers-egress: FATAL: could not install IPv4 egress lockdown" \
       "(need CAP_NET_ADMIN); refusing to start with open egress" >&2
  exit 97
fi
if ! _lockdown ip6tables; then
  echo "peers-egress: FATAL: could not install IPv6 egress lockdown;" \
       "refusing to start with open egress" >&2
  exit 97
fi
echo "peers-egress: egress lockdown active" \
     "(only uid $tinyproxy_uid + loopback may egress)" >&2

# Drop root -> tinyproxy for the long-lived daemon (the firewall is already
# installed; the daemon needs no caps). tinyproxy's own config also sets
# User/Group tinyproxy, so this is belt-and-suspenders least-privilege.
exec su-exec tinyproxy:tinyproxy tinyproxy -d -c /etc/tinyproxy/tinyproxy.conf
