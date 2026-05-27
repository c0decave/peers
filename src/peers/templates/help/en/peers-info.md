# peers info — print configured peers, goals, budget, and health

## NAME
peers info — dump the resolved `.peers/config.yaml` + `.peers/goals.yaml`
without running anything. Useful for sanity-checking a fresh init or
diffing configurations across projects.

## SYNOPSIS
```
peers [-C <dir>] info
```

## DESCRIPTION
Loads and validates the project's config + goals, then prints:

- Resolved target path.
- Driver (`orchestrator`/`hooks`/`sessions`).
- Comm channel (`git`/`hybrid`).
- Peers: count + per-peer `(tool, prompt_mode)` line.
- Budget: iterations, runtime, optional tokens + USD caps. When
  `max_usd` is set, also prints the effective `max_usd_mode`
  (`auto`/`hard`/`warn`/`off`) and the reason that mode was chosen.
- Health: idle timeout, absolute max runtime, buffer cap.
- Goals: total / hard / soft counts plus per-goal id, reviewer mode,
  consensus needed, and quorum (where applicable).

Exits non-zero on any validation failure (bad YAML, missing health
keys, invalid regex in `error_patterns`, ...).

## OPTIONS
None.

## EXAMPLES
```
peers info
peers -C ~/c0de/peers-c0de/my-app info
```

## FILES
- Reads: `.peers/config.yaml`, `.peers/goals.yaml`.

## ENVIRONMENT
None.

## SEE ALSO
- `peers status --help-man` — runtime state instead of config.
- `peers verify --help-man` — re-run hard gates.
- `peers-ctl modes show <name>` — inspect a mode's bundled config.

## NOTES
- `info` validates the same schema `peers run` does — if `info` exits
  non-zero, `run` will too. Use it as a fast sanity check.
- For OAuth subscription peers (claude / codex CLI), `max_usd_mode`
  defaults to `warn` (no hard cap) since per-token billing isn't
  meaningful there.
