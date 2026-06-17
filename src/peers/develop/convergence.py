"""R4: the real convergence runner the develop ``ContractImplementer`` injects.

``AgentConvergenceRunner`` is the ``inner(workdir) -> (ok, head_sha, branch)``
that :func:`peers.develop.adapters.worktree_convergence` was always missing: it
drives an injected agent to implement the frozen contract inside ``workdir``,
runs the contract's acceptance each attempt, and converges ONLY on a real pass
backed by a real diff — which it commits.

Honesty seam (load-bearing): a ``(True, sha, branch)`` result REQUIRES both that
``run_acceptance`` actually returned ``True`` AND that the agent produced a
non-empty diff that was committed. No passing acceptance -> no commit. A passing
acceptance with an empty diff (vacuous green) is NOT convergence. The runner
therefore cannot manufacture a confirmed-work commit.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

#: ``run_agent(prompt, workdir) -> text`` — one implement turn, editing workdir.
RunAgentInDir = Callable[[str, Path], str]
#: ``run_acceptance(workdir) -> (passed, output)`` — runs the frozen acceptance.
RunAcceptance = Callable[[Path], "tuple[bool, str]"]


def _git(workdir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(workdir), *args],
        capture_output=True, text=True, check=check)


class AgentConvergenceRunner:
    """Drive ``run_agent`` until ``run_acceptance`` passes (committing the diff)
    or ``budget`` attempts are exhausted."""

    def __init__(
        self,
        *,
        run_agent: RunAgentInDir,
        run_acceptance: RunAcceptance,
        budget: int = 5,
        commit_message: str = "develop: implement frozen contract",
        attest_peer: str | None = None,
        contract_md: str | None = None,
        exclude: tuple[str, ...] = (),
    ) -> None:
        if budget < 1:
            raise ValueError("budget must be >= 1")
        self.run_agent = run_agent
        self.run_acceptance = run_acceptance
        self.budget = budget
        self.commit_message = commit_message
        #: Pathspecs to EXCLUDE from the staged real-work diff + the commit. The
        #: caller passes any scratch dir it created inside the worktree (e.g. the
        #: implementer's frozen-contract dir) so it can neither satisfy the
        #: vacuous-green guard nor pollute the commit (HS-01/HS-03).
        self.exclude = tuple(exclude)
        #: The contract body to put in the implement prompt. When ``None`` the
        #: runner falls back to reading ``workdir/PLAN.md`` if present.
        self.contract_md = contract_md
        #: When set, the convergence commit is attributed to this peer via the
        #: substrate ``peers-attest`` note so the develop confirmed-work gate
        #: resolves a real author. The peer attests the work it actually did —
        #: independence (producer != consumer) is a SEPARATE downstream gate.
        self.attest_peer = attest_peer

    def _branch(self, workdir: Path) -> str | None:
        try:
            return _git(workdir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        except subprocess.CalledProcessError:
            return None

    def _build_prompt(self, workdir: Path, last_failure: str) -> str:
        if self.contract_md is not None:
            plan_body = self.contract_md
        else:
            plan = workdir / "PLAN.md"
            plan_body = plan.read_text(encoding="utf-8") if plan.is_file() else ""
        parts = [
            "Implement the frozen contract in this repository so its acceptance "
            "test passes. Edit only what the contract requires.",
        ]
        if plan_body:
            parts += ["", "CONTRACT (PLAN.md):", plan_body]
        if last_failure:
            parts += ["", "The acceptance test is still failing:", last_failure]
        return "\n".join(parts)

    def __call__(self, workdir) -> tuple[bool, str | None, str | None]:
        workdir = Path(workdir)
        branch = self._branch(workdir)
        base = _git(workdir, "rev-parse", "HEAD", check=False)
        base_sha = base.stdout.strip() if base.returncode == 0 else None
        last_failure = ""
        for _attempt in range(self.budget):
            try:
                self.run_agent(self._build_prompt(workdir, last_failure), workdir)
            except Exception:  # noqa: BLE001 — a transient turn failure is retried
                last_failure = "agent turn raised; retrying"
                continue
            passed, output = self.run_acceptance(workdir)
            if not passed:
                last_failure = output
                continue
            # acceptance passed — require a REAL diff before we claim convergence.
            # Exclude any caller scratch paths so they neither count as "work"
            # nor land in the commit (HS-01/HS-03).
            pathspec = ["--", ".", *(f":(exclude){e}" for e in self.exclude)]
            _git(workdir, "add", "-A", *pathspec)
            staged = _git(workdir, "diff", "--cached", "--quiet", *pathspec,
                          check=False)
            if staged.returncode == 0:
                # vacuous green: nothing changed -> not confirmed work.
                return (False, None, branch)
            _git(workdir, "commit", "-q", "-m", self.commit_message)
            sha = _git(workdir, "rev-parse", "HEAD").stdout.strip()
            if self.attest_peer:
                from peers.attest import attest_commits
                attest_commits(workdir, self.attest_peer, base_sha, sha)
            return (True, sha, branch)
        return (False, None, branch)
