# hunt-open-ended

You are hunting bugs in a codebase where convergence is not meaningful.
The substrate treats soft consensus as progress only and stops on budget
or an operator/halt-class event.

Each tick:

1. Read what has already been found. Do not re-file duplicates.
2. Pick a fresh surface: parser, protocol edge, auth boundary, memory
   ownership path, concurrency edge, or fuzz coverage gap.
3. File evidence, not prose. Prefer sanitizer output, PoC, reachability
   chain, minimized input, or a failing test.
4. If a hypothesis does not hold, record the surface as explored and move
   to a new one.
5. Leave a handoff that makes the next peer faster.
