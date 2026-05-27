#!/bin/sh
set -eu
python3 .peers/checks/scan_secrets.py "${1:-.}"
