# peers run-check — resolve and invoke a check script by name

## NAME
peers run-check — locate a check script (project's `.peers/checks/`,
a mode's `checks/`, or a `mode:check` qualified name) and invoke it
via `python3`. Used by `cmd:` lines in scaffolded `goals.yaml`.

## SYNOPSIS
```
peers [-C <dir>] run-check <name>
peers [-C <dir>] run-check <mode>:<check_name>
```

## DESCRIPTION
Resolution order for an unqualified `name`:

1. `<target>/.peers/checks/<name>.py` (most common — copied at
   `peers init` time).
2. Otherwise, walks every discovered mode's `checks/<name>.py`. If
   exactly one match → invoke it. If multiple → exit 1 with a list
   of `mode:name` suggestions for disambiguation. If zero → exit 1
   with a deduped list of available check names.

For a `mode:check_name` qualified call, only that mode is searched
(falling back to `.peers/checks/<check_name>.py` as a back-compat).

Once resolved, the script is invoked as `python3 <resolved-path>`
with stdout/stderr inherited; the exit code is forwarded.

Only top-level `.py` files in each mode's `checks/` directory are
considered. Lang-specific shell scripts under `checks/lang_<lang>/`
are invoked via their own `cmd:` strings (e.g. `bash .peers/...`),
not through this shim.

## OPTIONS
- `name` (positional, required) — bare check name or `mode:name`.

## EXAMPLES
```
# Invoke the default self-review check (copied to .peers/checks/).
peers run-check verify_self_review

# Disambiguate when the same check name exists in two modes.
peers run-check audit:coverage_3class

# List what's available (run with a bogus name).
peers run-check no-such-thing 2>&1 | grep available
```

## FILES
- Reads: `<target>/.peers/checks/*.py` + bundled-mode `checks/`.

## ENVIRONMENT
- `PEERS_MODES_DIR` — extra mode-discovery directory.

## SEE ALSO
- `peers-ctl modes show <name>` — view a mode's checks/ directory.
- `peers verify --help-man` — re-run goals (not individual scripts).

## NOTES
- Designed for `cmd:` lines in `goals.yaml`: it lets you write
  `peers -C . run-check coverage_3class` instead of
  `python3 -m peers.templates.modes.audit.checks.coverage_3class`.
- Ambiguity errors are intentional — silently picking one of two
  same-named checks would be surprising; force the user to qualify.
