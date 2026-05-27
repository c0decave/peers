# peers-ctl new — one-shot scaffold + init + register

## NAME
peers-ctl new — create the target directory, `git init` it with an
empty initial commit, run `peers init`, optionally write a `SPEC.md`,
and register the result. After this, `peers-ctl start <name>` works.

## SYNOPSIS
```
peers-ctl new <path> [--name NAME] [--spec TEXT_OR_PATH]
              [--driver {orchestrator,hooks,sessions}]
              [--force] [--container]
              [--modes <list>] [--lang <lang>]
```

## DESCRIPTION
End-to-end scaffold so you can go from zero to a runnable peers project
with a single command. Steps:

1. Resolve `<path>`: bare name → `$PEERS_PROJECTS_ROOT/<name>`; full
   path → verbatim.
2. If target exists and is non-empty, refuse without `--force`.
3. Write a baseline `README.md` (and optional `SPEC.md`).
4. `git init -b main` + empty initial commit (so `peers-baseline`
   has something to tag).
5. Run `peers init [--force] [--driver ...] [--modes ...] [--lang ...]`
   on the host OR inside the `peers:dev` container (with `--container`).
6. Register the project in the controller registry.

`--container` is preferred when the host doesn't have `peers`
installed: only `podman` + the `peers:dev` image are needed.

## OPTIONS
- `path` (positional, required) — target directory.
- `--name NAME` — registry name (default: directory basename).
- `--spec TEXT_OR_PATH` — `SPEC.md` content as a literal string OR a
  path to a file with that content.
- `--driver {orchestrator,hooks,sessions}` — default `orchestrator`.
- `--force` — scaffold into a non-empty directory OR overwrite an
  existing registry entry.
- `--container` — run `peers init` inside the `peers:dev` container.
- `--modes <list>` — comma-separated mode names (`audit`, `security`,
  `thorough`, ...). See `peers-ctl modes list`.
- `--lang <lang>` — `python` (default), `js`, `rust`, `go`.
- `--audit-templates` — DEPRECATED alias for `--modes=audit`.

## EXAMPLES
```
# Most common: container, audit + thorough, JS project.
peers-ctl new my-app --container --modes=audit,thorough --lang=js \
                     --spec ./my-app-spec.md

# Bare name lands in $PEERS_PROJECTS_ROOT.
peers-ctl new quick-test --modes=audit

# Force re-scaffold on top of existing state.
peers-ctl new my-app --force --modes=audit,security
```

## FILES
Created under `<path>/`:
- `README.md` — baseline ("scaffolded by peers-ctl new").
- `SPEC.md` — only with `--spec`.
- `.peers/` — via `peers init` (see `peers init --help-man`).

Registry update:
- `$XDG_CONFIG_HOME/peers-ctl/projects.json` — new entry for this name.

## ENVIRONMENT
- `PEERS_PROJECTS_ROOT` — base for bare-name resolution.
- `PODMAN_CMD` — override `podman` binary path (with `--container`).
- `PEERS_CTL_PODMAN_NETWORK` — podman network mode.

## SEE ALSO
- `peers init --help-man` — what step 5 does in detail.
- `peers-ctl add --help-man` — register without scaffolding.
- `peers-ctl modes list` — see which `--modes` you can pass.

## NOTES
- The empty initial commit anchors `peers-baseline`, the rollback tag
  `peers init` sets.
- If you pass `--spec` with no `/` and the literal is not a path,
  it's treated as inline content. Use `./spec.md` (or any path with
  a separator) to disambiguate.
