# Phase 0: Reconnaissance

You are running TICK 0 of an implement-mode project. Your task is to
produce `RECON.md` at the project root — a structured overview of the
existing codebase so the team (you + reviewer peer) can plan well-aimed
implementation work in subsequent ticks.

## What to write into RECON.md

```markdown
# Codebase Reconnaissance

## Module Map
- src/<top-level>: <one-line purpose>
- ...

## Key Abstractions
- ClassName / FunctionName: <one-line purpose, where defined>
- ...

## Entry Points
- CLI commands: <list with files>
- HTTP routes: <list with files>
- Other invocations: <cron, hooks, etc>

## Conventions
- Test pattern: pytest / unittest / other?
- Style: pep8 / black / ruff?
- Type checking: mypy / pyright / none?
- CI: github actions / circle / none?

## External Dependencies
- Production: <list relevant packages>
- Dev: <list relevant packages>

## Implementation-Relevant Context
- Anything specific to the PLAN.md feature that the implementer needs
  to know up-front (existing partial implementations, naming
  conflicts, etc.)
```

## How to produce it

1. Use file-listing tools (e.g. `find src -name '*.py'`, `ls`) to enumerate the codebase
2. Read key files (entry points, config, test setup) to extract abstractions
3. Compare to PLAN.md to surface feature-specific context

## Verification

After writing RECON.md, run:
- `wc -l RECON.md` — should be substantive (>50 lines for non-trivial projects)
- `git add RECON.md && git commit -m "tick 0 recon: codebase overview"` — commit at end of tick

## What NOT to write

- Don't write code yet (Phase 0 is read-only-ish)
- Don't make architecture proposals (that's Tick 2)
- Don't critique the plan (that's Tick 1)
- Don't update PLAN.md
