#!/bin/sh
set -eu
if [ "${1:-}" = "--snapshot" ]; then
  mkdir -p .peers
  npm test -- --listTests > .peers/passing-baseline.txt
  echo "no_regression_js: snapshot saved"
  exit 0
fi
test -f .peers/passing-baseline.txt || {
  echo "no_regression_js: missing .peers/passing-baseline.txt; run --snapshot"
  exit 1
}
npm test
