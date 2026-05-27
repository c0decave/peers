# Phase 0: Plan Alignment

You are running TICK 1 of an implement-mode project. Your task is to
produce `PLAN.aligned.md` — an annotated version of PLAN.md where each
step has been mapped to the existing codebase (using RECON.md from
Tick 0).

## What to write into PLAN.aligned.md

Copy the original PLAN.md structure verbatim, then under each `[ ]`
step, add 3 annotation sub-fields:

```markdown
- [ ] [STEP-1] Original step text
  - rationale: <preserved from PLAN.md if present>
  - touches: <existing files this step likely modifies — fill in from
    RECON.md analysis. Use forward-refs for new files.>
  - status: open | partial | conflict
  - notes: <prose: existing patterns to reuse, conflicts to resolve,
    sub-tasks needed before this step can land cleanly>
```

## Status values

- `open`: nothing existing — clean implementation
- `partial`: code already has SOME of this step. Note where in
  `notes:` (e.g. "auth middleware exists at src/auth.py:42 but lacks
  refresh-token support — STEP-1 needs to ADD refresh, not the full
  middleware")
- `conflict`: existing code conflicts with this step's design. Document
  the conflict so the implementer can resolve before proceeding.

## How to produce it

1. Read PLAN.md
2. Read RECON.md (from Tick 0)
3. For each step:
   a. Identify probable touches: files using RECON.md's module map
   b. Search for existing related code (grep for relevant names)
   c. Categorize as open/partial/conflict
   d. Write actionable notes

## Verification

After writing PLAN.aligned.md, run:
- `git diff PLAN.md PLAN.aligned.md` — should show structured annotations
- `git add PLAN.aligned.md && git commit -m "tick 1 alignment: plan vs existing code"`

## What NOT to do

- Don't modify PLAN.md (the original is frozen)
- Don't write code yet
- Don't drop steps you find inconvenient (immutable step IDs per Schicht 2)
- Don't decide architecture (that's Tick 2)
