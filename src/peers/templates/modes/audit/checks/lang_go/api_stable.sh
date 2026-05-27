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
  find "$repo" \
    \( -path '*/.git/*' -o -path '*/.peers/*' -o -path '*/vendor/*' \) -prune \
    -o -name '*.go' -type f -print \
    | xargs grep -Eh '^(type|func|var|const) [A-Z]' 2>/dev/null \
    | sed 's/[[:space:]][[:space:]]*/ /g' \
    | sort -u
}

if [ "$mode" = "dump" ]; then
  dump_api
  exit 0
fi

baseline="$repo/.peers/api-baseline.txt"
test -f "$baseline" || {
  echo "api_stable_go: missing $baseline; run --dump first"
  exit 1
}
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
dump_api > "$tmp"
diff -u "$baseline" "$tmp" || {
  echo "api_stable_go FAIL: public Go API changed"
  exit 1
}
echo "api_stable_go: clean"
