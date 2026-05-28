#!/bin/bash
set -euo pipefail

echo "[pre-push] ruff..."
python3 -m ruff check src tests

echo "[pre-push] hardening audit (static)..."
scripts/hardening-audit.sh --static

echo "[pre-push] pytest..."
python3 -m pytest -q
