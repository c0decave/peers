# peers-ctl — multi-project controller for peers loops

## NAME
peers-ctl — start, stop, monitor, and report on multiple peers loops
from a single host. Each project is registered once and then
addressed by short name.

## SYNOPSIS
```
peers-ctl [--config-dir <dir>] <subcommand> [options]
peers-ctl --help-man [--de|--en]
peers-ctl <subcommand> --help-man [--de|--en]
```

## DESCRIPTION
`peers-ctl` is the controller layer on top of the per-project `peers`
substrate. It keeps a small registry (`projects.json` under
`$XDG_CONFIG_HOME/peers-ctl/`) mapping names to filesystem paths,
spawns/tracks `peers run` instances (host or container), tails their
logs, prunes old logs, and renders a multi-project dashboard.

Typical workflow:

1. `peers-ctl new my-app --container --modes=audit` — scaffold + register.
2. `peers-ctl start my-app --max-ticks 50 --max-usd 5` — start the loop.
3. `peers-ctl dashboard` / `peers-ctl tail my-app` — observe.
4. `peers-ctl stop my-app` — stop cleanly when done.
5. `peers-ctl report` — write a Markdown rollup across all projects.

## OPTIONS
- `--config-dir <dir>` — override registry/log location (default
  `$XDG_CONFIG_HOME/peers-ctl/`, falling back to `~/.config/peers-ctl/`).
- `--version` — print version and exit.
- `--help-man` — print this overview (or, after a subcommand, that
  subcommand's man-page).
- `--de` / `--en` — force German or English output for `--help-man`.

## EXAMPLES
```
# Full audit run with the container image.
peers-ctl new my-app --container --modes=audit --spec ./spec.md
PEERS_CTL_PODMAN_NETWORK=host peers-ctl start my-app --container --max-ticks 50

# Health check first.
peers-ctl doctor

# Multi-project rollup.
peers-ctl dashboard

# Tear down everything older than a week.
peers-ctl prune --older-than-days 7
```

## FILES
- `$XDG_CONFIG_HOME/peers-ctl/projects.json` — registry.
- `$XDG_CONFIG_HOME/peers-ctl/logs/<project>.log` — per-project log.
- `$PEERS_PROJECTS_ROOT/<name>/` — default home for bare-name projects.

## ENVIRONMENT
- `PEERS_PROJECTS_ROOT` — base directory for bare-name `new`/`add`
  (default `~/c0de/peers-c0de/`).
- `XDG_CONFIG_HOME` — config root override.
- `PODMAN_CMD` — override `podman` binary path.
- `PEERS_CTL_PODMAN_NETWORK` — podman network mode (`host`, `none`, ...).

## SEE ALSO
- `peers --help-man` for per-project commands.
- `peers-ctl doctor` for pre-flight checks.
- `docs/HOWTO-audit-and-fix.md` for an end-to-end audit walkthrough.

## NOTES
- The registry is authoritative for `peers-ctl` operations; projects
  whose paths vanish are reconciled to state `missing` on the next
  `list`/`status` call.
- Container path (`--container`) needs only `podman` + a built
  `peers:dev` image on the host — no host `peers` install required.
