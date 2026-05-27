#!/bin/sh
# Emit a non-UTF-8 byte to stderr to exercise HealthGuard's decoder.
printf '\xff\xfe broken bytes\n' >&2
echo "ok stdout"
exit 0
