#!/bin/sh
set -eu
if [ "${1:-}" = "--dump" ]; then
  find . -maxdepth 3 \( -name 'index.js' -o -name 'index.ts' -o -name '*.d.ts' \) -type f -print
  exit 0
fi
test -f .peers/api-baseline.txt || {
  echo "api_stable_js: missing .peers/api-baseline.txt; run --dump first"
  exit 1
}
tmp="$(mktemp)"
find . -maxdepth 3 \( -name 'index.js' -o -name 'index.ts' -o -name '*.d.ts' \) -type f -print > "$tmp"
diff -u .peers/api-baseline.txt "$tmp" || {
  rm -f "$tmp"
  echo "api_stable_js FAIL: exported entrypoint set changed"
  exit 1
}
rm -f "$tmp"
echo "api_stable_js: clean"
