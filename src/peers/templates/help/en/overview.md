# peers — multi-peer orchestration substrate for LLM coding agents

## NAME
peers — drive two or more LLM coding agents (claude, codex, ...) against a
single Git repository, with goal-gated stop conditions, budget caps,
and a tamper-resistant control plane under `.peers/`.

## SYNOPSIS
```
peers [-C <dir>] <subcommand> [options]
peers --help-man [--de|--en]
peers <subcommand> --help-man [--de|--en]
```

## DESCRIPTION
`peers` is a CLI for running a loop of cooperating coding agents. A
project's `.peers/` directory holds the configuration (`config.yaml`),
the gate definitions (`goals.yaml`), per-peer state, run log, and any
check scripts. The substrate spawns each peer in turn, observes its
output via the health-guard (idle-timeout + error patterns), evaluates
hard/soft goals after the peer commits, and decides whether to keep
going.

Two CLIs ship together:

- **peers** — for working with a single project in the current directory.
- **peers-ctl** — for managing many projects (start/stop/dashboard) across
  a single host. See `peers-ctl --help-man` for that side.

Typical workflow: `peers init` → edit `.peers/goals.yaml` → `peers run`
→ `peers verify` and `peers report` after the run.

## OPTIONS
- `-C, --target <dir>` — operate on the project at `<dir>` instead of
  the current working directory.
- `--version` — print the substrate version and exit.
- `--help-man` — print this overview (or, when used after a subcommand,
  that subcommand's man-page).
- `--de` / `--en` — force German or English output for `--help-man`.
  Default follows `$LANG`.

## EXAMPLES
```
# Bootstrap a control plane in the current directory.
peers init

# Bootstrap with audit + security modes for a JS project.
peers -C ./my-app init --modes=audit,security --lang=js

# Run the loop for at most 50 ticks, capped at $5.
peers run --max-ticks 50 --max-usd 5

# Re-check all hard goals without involving any peer.
peers verify

# Human-readable Markdown summary.
peers report
```

## FILES
- `.peers/config.yaml` — peer roster, health, budget, comm channel.
- `.peers/goals.yaml` — hard + soft gates.
- `.peers/state.json` — turn pointer, budgets, goal status (atomic).
- `.peers/log/runs.jsonl` — append-only run log.
- `.peers/checks/` — copy of mode-supplied check scripts.

## ENVIRONMENT
- `LANG` — default language for `--help-man`.
- `PEERS_PROJECTS_ROOT` — base directory for bare-name peers-ctl projects
  (default `~/c0de/peers-c0de/`).
- `PODMAN_CMD` — override `podman` binary path (for container path).
- `PEERS_CTL_PODMAN_NETWORK` — podman network mode (`host`, `none`, ...).

## SEE ALSO
- `peers init --help-man` and the per-subcommand pages.
- `peers-ctl --help-man` for the controller-side workflow.
- `docs/HOWTO-audit-and-fix.md` for an end-to-end audit walkthrough.

## NOTES
- The substrate enforces a no-follow safe-I/O policy on `.peers/` files,
  so symlinking pieces of the control plane causes the relevant command
  to refuse rather than silently follow.
- `peers init` tags the target's current HEAD as `peers-baseline` so a
  human can always `git reset --hard peers-baseline` to roll back.
