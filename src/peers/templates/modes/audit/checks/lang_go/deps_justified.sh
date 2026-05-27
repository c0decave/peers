#!/bin/sh
set -eu
python3 .peers/checks/deps_justified.py "${1:-.}"
