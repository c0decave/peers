# peers verify — re-run all hard goals + verify.commands standalone

## NAME
peers verify — run every hard goal (and any user-declared
`verify.commands`) against the current repo state without involving a
peer. Writes `.peers/VERIFY.md`; exit 0 iff every check passed.

## SYNOPSIS
```
peers [-C <dir>] verify
```

## DESCRIPTION
Idempotent post-loop or pre-handoff check: re-evaluates each hard
goal's `cmd:` via the same `GoalEngine` the live loop uses, plus
every entry under `verify.commands:` in `config.yaml`. Each check
gets a per-row state (`pass`/`fail`/`timeout`) with diagnostic + the
measured duration in ms.

Writes a Markdown table to `.peers/VERIFY.md` and echoes the same
content to stdout. Exit code is 0 only if every hard goal AND every
verify command passed.

Useful for:
- Acceptance: did the loop *really* satisfy the gates after
  `peers run` reported success?
- CI: run as a final step to catch drift.
- Audit: produce a fresh `VERIFY.md` next to the loop's `REPORT.md`.

## OPTIONS
None.

## EXAMPLES
```
# Post-loop acceptance.
peers verify

# CI integration (use the exit code).
peers -C ./my-app verify || exit 1
```

## FILES
- Reads: `.peers/config.yaml`, `.peers/goals.yaml`.
- Writes: `.peers/VERIFY.md`.

## ENVIRONMENT
None directly; per-check `cmd:` strings may consume project env.

## SEE ALSO
- `peers report --help-man` — broader project rollup (state + ticks).
- `peers info --help-man` — config dump (peers/goals/budget).
- `docs/HOWTO-audit-and-fix.md` — section "8) Abnahme".

## NOTES
- Soft goals are NOT re-evaluated here (they require peer reviews).
- Verify-command timeouts honour `verify.timeout_s` from
  `config.yaml`, falling back to `goals.timeout_s` (default 120s).
