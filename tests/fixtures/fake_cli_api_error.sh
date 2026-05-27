#!/bin/sh
# Emits an "API rate limit" pattern, then exits cleanly.
# the post-write sleep gives HealthGuard's reader thread enough wall
# time to drain the pipe into scan_buf before the child exits, so the in-loop
# scan_new() catches the pattern instead of the post-join rescan. Without it,
# the test asserting matched_error_source == "in-loop" flaked ~13%.
echo "doing work"
echo "Error: Rate limit exceeded (429). Try again in 60s." >&2
echo "more work but should be ignored"
sleep 0.1
exit 0
