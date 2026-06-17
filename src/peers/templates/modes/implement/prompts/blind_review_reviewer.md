# Reviewer Tick

You are REVIEWING in this tick. Your job:

1. **Read the code-diff from the previous implementer tick** —
   `git diff HEAD~1..HEAD` shows what landed.

2. **DO NOT read `IMPLEMENTATION_NOTES.md` before forming your own
   summary.** This is the blind-review invariant. Read the code
   itself.

3. Write `REVIEW_NOTES.md` describing what the code does, from your
   reading alone. List: functions/classes touched, behaviors added or
   changed, test coverage observed.

4. AFTER your REVIEW_NOTES.md is written and committed, compare it to
   IMPLEMENTATION_NOTES.md. Any divergence → file a `[BLIND-REVIEW-MISMATCH]`
   entry in `CONCERNS.md` with the specific discrepancy. **Put the marker at the
   START of its own line** (optionally a heading or list item, e.g.
   `- [BLIND-REVIEW-MISMATCH] reviewer says X is absent`). The gate counts only a
   line-leading marker as a filing; merely mentioning the marker mid-sentence
   (e.g. describing this protocol) does NOT trip the gate.

5. If checkoffs are warranted (all of: code matches claim, tests
   match step touches:, no blocking concerns), update PLAN.md to mark
   the relevant steps `[x]` with the commit SHA.

## Output integrity

Your REVIEW_NOTES.md becomes the substrate's record of "what an
independent reader thinks the code does". If you cheated by reading
IMPLEMENTATION_NOTES.md first, you've reduced both peers to one
honest source and one rubber-stamp.
