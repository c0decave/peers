#!/bin/sh
set -eu

repo="${1:-.}"
if [ "$repo" = "--dump" ]; then
  repo="."
  mode="dump"
else
  mode="check"
fi

dump_api() {
  find "$repo/src" \
    \( -path '*/target/*' -o -path '*/.peers/*' \) -prune \
    -o -name '*.rs' -type f -print 2>/dev/null \
    | xargs grep -Eh '^[[:space:]]*pub([[:space:]]|\()' 2>/dev/null \
    | sed 's/[[:space:]][[:space:]]*/ /g' \
    | sort -u
}

if [ "$mode" = "dump" ]; then
  dump_api
  exit 0
fi

baseline="$repo/.peers/api-baseline.txt"
test -f "$baseline" || {
  echo "api_stable_rust: missing $baseline; run --dump first"
  exit 1
}
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
dump_api > "$tmp"
diff -u "$baseline" "$tmp" || {
  echo "api_stable_rust FAIL: public Rust API changed"
  exit 1
}
echo "api_stable_rust: clean"
