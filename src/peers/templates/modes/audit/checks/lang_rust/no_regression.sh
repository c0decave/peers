#!/bin/sh
set -eu

if [ "${1:-}" = "--snapshot" ]; then
  mkdir -p .peers
  cargo test -- --list > .peers/passing-baseline.txt
  echo "no_regression_rust: snapshot saved"
  exit 0
fi

test -f .peers/passing-baseline.txt || {
  echo "no_regression_rust: missing .peers/passing-baseline.txt; run --snapshot"
  exit 1
}
cargo test
