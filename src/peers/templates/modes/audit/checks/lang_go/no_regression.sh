#!/bin/sh
set -eu

if [ "${1:-}" = "--snapshot" ]; then
  mkdir -p .peers
  go test ./... -run '^$' -list . > .peers/passing-baseline.txt
  echo "no_regression_go: snapshot saved"
  exit 0
fi

test -f .peers/passing-baseline.txt || {
  echo "no_regression_go: missing .peers/passing-baseline.txt; run --snapshot"
  exit 1
}
go test ./...
