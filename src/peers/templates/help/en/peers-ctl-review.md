# peers-ctl review — show latest handoff self-review

## NAME
peers-ctl review — print the body of the latest commit in a project
whose subject is `Peer-Status: handoff`. Used to inspect a peer's
self-review at the end of a turn.

## SYNOPSIS
```
peers-ctl review <name>
```

## DESCRIPTION
Resolves `<name>` against the registry, then runs
`git -C <path> log --grep='^Peer-Status: handoff$' -n 1 --format=...`
to fetch the most recent handoff commit's SHA, subject, and full
body. The Self-Review section in that body is what the soft
`self-review-on-handoff` goal expects.

Useful for peer-review interactions: a human inspects the most
recent handoff before signing off on the next phase.

## OPTIONS
- `name` (positional, required) — registered project name.

## EXAMPLES
```
peers-ctl review my-app

# Pipe into less for paging.
peers-ctl review my-app | less
```

## FILES
- Reads: registry, git log of `<project>`.

## ENVIRONMENT
- `GIT_PAGER` etc. — honoured via the underlying `git log` invocation
  (output is captured and printed verbatim, so paging is up to you).

## SEE ALSO
- `peers run-check verify_self_review` — the substrate's own check.
- `peers report --help-man` — broader per-project rollup.

## NOTES
- If no `Peer-Status: handoff` commit exists yet, exits 1 with a
  short "no handoff commit found" message.
- The grep pattern is anchored — only `Peer-Status: handoff` (exact)
  matches; other variants (e.g. `Peer-Status: deferred`) are ignored.
