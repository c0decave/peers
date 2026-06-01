#!/bin/sh
set -eu

runtime_filter=/tmp/tinyproxy-filter
cp /etc/tinyproxy/filter "$runtime_filter"

if [ "${PEERS_EGRESS_EXTRA_HOSTS:-}" ]; then
  printf '\n# Runtime project-specific allow-list additions.\n' >> "$runtime_filter"
  printf '%s' "$PEERS_EGRESS_EXTRA_HOSTS" | tr ',' '\n' | while IFS= read -r host_re; do
    [ "$host_re" ] || continue
    printf '%s\n' "$host_re" >> "$runtime_filter"
  done
fi

exec tinyproxy -d -c /etc/tinyproxy/tinyproxy.conf
