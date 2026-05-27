# Phase 0: Architecture Intent

You are running TICK 2 of an implement-mode project. Your task is to
produce `ARCHITECTURE.intended.md` — the architecture you and the
reviewer peer agree this feature SHOULD have, given PLAN.md +
RECON.md + PLAN.aligned.md.

After this file is committed, the substrate freezes it (SHA-pin). The
final convergence gate (`architecture-coherent`) compares the actual
implementation against this intent.

## What to write into ARCHITECTURE.intended.md

```markdown
# Architecture (Intended)

## Components
- ComponentName: <responsibility, where it lives, key public API>
- ...

## Data Flow
- Component A → Component B: <what data, what protocol>
- ...

## Module Boundaries
- module_x can call: [module_a, module_b]
- module_x must NOT call: [module_c (lower-layer)]

## State / Persistence
- What state lives where (database/cache/config files/in-memory)

## Error Handling
- Where errors propagate vs. get swallowed
- User-visible error paths

## Testing Strategy
- Where unit tests live, where integration tests live
- What's covered by acceptance command (frozen contract)

## Out of Scope (Explicit)
- Things we deliberately won't build in this feature
```

## How to produce it

1. Read PLAN.md (original intent) + PLAN.aligned.md (existing-code context)
2. Read RECON.md (existing patterns to reuse / avoid)
3. Synthesize a coherent target architecture
4. Make boundary decisions explicit — what depends on what

## Verification

After writing ARCHITECTURE.intended.md:
- `wc -l ARCHITECTURE.intended.md` — substantive (>30 lines for non-trivial)
- `git add ARCHITECTURE.intended.md && git commit -m "tick 2 architecture: intended design"`
- Substrate will SHA-pin this file at end of Tick 2 (handled by driver)

## What NOT to do

- Don't write actual code (that's Tick 3+)
- Don't change PLAN.md or PLAN.original.md
- Don't propose impossible/out-of-scope architecture
- Don't be vague — be specific about boundaries and protocols
