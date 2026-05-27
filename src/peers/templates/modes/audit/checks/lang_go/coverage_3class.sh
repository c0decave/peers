#!/bin/sh
set -eu

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
find . \
  \( -path './.git' -o -path './.peers' -o -path './vendor' \) -prune \
  -o -name '*_test.go' -type f -print > "$tmp"

test -s "$tmp" || {
  echo "coverage_3class_go FAIL: no Go test files found"
  exit 1
}

missing=""
for kind in happy edge sad; do
  case "$kind" in
    happy) rx='happy|ok|success|nominal|baseline' ;;
    edge) rx='edge|boundary|empty|max|min|long|unicode' ;;
    sad) rx='sad|fail|error|invalid|panic|timeout|broken' ;;
  esac
  if ! xargs grep -Eih "$rx" < "$tmp" >/dev/null 2>&1; then
    missing="${missing}${missing:+, }$kind"
  fi
done

if [ -n "$missing" ]; then
  echo "coverage_3class_go FAIL: missing $missing test class(es)"
  exit 1
fi
echo "coverage_3class_go: clean"
