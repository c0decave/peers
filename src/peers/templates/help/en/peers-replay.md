# peers replay — print log entries for a given iteration

## NAME
peers replay — re-print the JSONL log entry/entries for a given
iteration. Useful for post-mortem debugging.

## SYNOPSIS
```
peers [-C <dir>] replay <iteration>
```

## DESCRIPTION
Reads `.peers/log/runs.jsonl`, finds every entry where
`iteration == <N>`, and prints each as indented JSON to stdout. Skips
malformed lines defensively (warns once with a count at the end).

Each tick produces at least one log entry (peer name, tool,
classification, duration, tokens, USD). Iteration 0 typically holds
the initial-scaffold entry; failures and consecutive retries may
yield multiple entries with the same `iteration`.

## OPTIONS
- `iteration` (positional, required) — the integer iteration to replay.

## EXAMPLES
```
# Print everything that happened during tick 7.
peers replay 7

# Spot-check the very first tick.
peers -C ./my-app replay 1
```

## FILES
- Reads: `.peers/log/runs.jsonl`.

## ENVIRONMENT
None.

## SEE ALSO
- `peers report --help-man` — full Markdown rollup.
- `peers status --help-man` — current state snapshot.

## NOTES
- `peers replay` only inspects the substrate's own JSONL log. The
  full per-peer stdout/stderr lives under `.peers/log/peers/tick-NNNN-<peer>/`
  — open that directory for the raw transcript.
- If no entry exists for the requested iteration, exit code is 1 with
  an explanatory stderr message.
