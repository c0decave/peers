#!/bin/sh
set -eu
python3 .peers/checks/diff_size_per_resolve.py "${1:-.}"
