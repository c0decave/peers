"""Area-specific fixtures for the `peers develop` ACTIVE test file
(``tests/unit/test_active_develop.py``).

NOT a test module (underscore prefix) — imported as
``from tests.unit._active_develop_fixtures import ...``.

These drive the REAL develop pipeline deterministically via the documented
``cmd_develop(_make_frontend=...)`` seam (cli.py:2571) + the real
``make_develop_frontend`` assembly (assembly.py:53) with DETERMINISTIC python
agent callables in place of a live LLM/container. The agents emit exactly the
JSON the real ``LLMAuditor`` / ``LLMAuthor`` / ``LLMRefuter`` adapters parse
(adapters.py) and a real file edit for the IMPLEMENT convergence turn
(convergence.py) — so the FULL audit -> verify -> author -> freeze -> converge
-> commit -> attest -> confirmed-work -> spine-gates chain runs for real, with
no live model.

The single thing varied per honesty case is the IMPLEMENT turn (the
``impl_run_agent`` callable): it does / does-not produce a real diff, tampers /
does-not-tamper the frozen acceptance.sh, etc. Everything upstream stays a
genuine confirm so the honesty seam is the only thing under test.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
from pathlib import Path

from peers.develop.assembly import make_develop_frontend

# A parser-valid PLAN.md (## Meta surfaces:[cli] + acceptance + a [STEP-N] with
# touches:) — mirrors tests/unit/test_develop_assembly.VALID_PLAN. The acceptance
# `test -f fix.txt` is RED at base (no fix.txt) and GREEN once the implement turn
# writes fix.txt — exactly the plan's "fails before the edit, passes after".
VALID_PLAN = (
    "# Fix the missing guard\n\n"
    "## Meta\nsurfaces: [cli]\nacceptance: test -f fix.txt\n\n"
    "## Steps\n- [ ] [STEP-1] create the guard marker\n  - touches: fix.txt\n"
)

# One real finding the LLMAuditor will parse (all _FINDING_FIELDS present, the
# dimension is one the test requests).
_FINDING_JSON = json.dumps([{
    "id": "AUD-1",
    "dimension": "correctness",
    "severity": "low",
    "location": "app.py:1",
    "summary": "missing guard",
    "fix": "add guard",
    "fail_first": "test_guard",
}])

# The AUTHOR object the LLMAuthor will parse into an AuthoredContract.
_AUTHOR_JSON = json.dumps({
    "plan_md": VALID_PLAN,
    "acceptance": "test -f fix.txt",
    "e2e": None,
})

# The REFUTE object: refuted=false -> the finding survives the verify gate.
_REFUTE_JSON = json.dumps({"refuted": False})


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True).stdout.strip()


def make_repo(tmp_path: Path, *, name: str = "proj", with_bar: bool = True) -> Path:
    """A throwaway git repo: init + identity + gpgsign=false + one commit + (by
    default) a present quality bar (pyproject.toml) so ``infer_bar`` classifies
    the bar PRESENT (without it DevelopFrontend.prepare blocks every round)."""
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    if with_bar:
        (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n",
                                             encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo


def _one_shot_agent(text: str):
    """An AUDIT/AUTHOR/REFUTE one-shot runner that branches on the prompt the real
    adapters build (adapters.py:_build_prompt) and returns the matching JSON.

    ``text`` is unused beyond signalling; the real branching keys off prompt
    content so the SAME callable serves all three non-implement seams."""
    def run_agent(prompt: str) -> str:
        if "Try to REFUTE" in prompt:
            return _REFUTE_JSON
        if "authoring an implement contract" in prompt:
            return _AUTHOR_JSON
        # default: the AUDIT prompt ("Audit the repository ...").
        return _FINDING_JSON
    return run_agent


def _make_frontend_builder(impl_run_agent, *, budget: int):
    """Return a ``_make_frontend(repo)`` that assembles the REAL develop frontend
    with the deterministic one-shot agent for audit/author/refute and the given
    ``impl_run_agent`` for the IMPLEMENT convergence turn. ``run_tests`` is
    injected so the bar is the repo's real pyproject (present), not re-run."""
    def builder(repo: Path):
        return make_develop_frontend(
            repo,
            run_agent=_one_shot_agent("seam"),
            impl_run_agent=impl_run_agent,
            dimensions=["correctness"],
            run_tests=lambda cmd: (0, "1 passed"),  # bar present + green
            convergence_budget=budget,
            attest_peer="claude",
        )
    return builder


# ---- IMPLEMENT turns (the ONE thing each honesty case varies) ---------------

def impl_real_edit(prompt: str, workdir) -> str:
    """HAPPY: write the file the contract's acceptance checks for -> a real,
    non-empty diff -> acceptance passes -> the convergence runner commits+attests."""
    (Path(workdir) / "fix.txt").write_text("guarded\n", encoding="utf-8")
    return "implemented the guard"


def impl_noop(prompt: str, workdir) -> str:
    """NO-OP / lying agent: reports success but edits NOTHING. With a vacuously
    green acceptance the runner's `git diff --cached --quiet` sees an empty diff
    -> (False, None, branch) -> no commit, no confirm (convergence.py:113-115)."""
    return "done (changed nothing)"


def impl_tamper_acceptance(prompt: str, workdir) -> str:
    """TAMPER: rewrite the frozen contracts/acceptance.sh to `exit 0` and make NO
    real edit. run_acceptance verify_contracts() catches the sha drift BEFORE
    trusting the script -> fail CLOSED (assembly.py:82-86)."""
    for accp in glob.glob(str(Path(workdir) / "peers-develop-impl-*" /
                               "contracts" / "acceptance.sh")):
        os.chmod(accp, 0o644)
        Path(accp).write_text("exit 0\n", encoding="utf-8")
    return "tampered the oracle, made no real fix"


def impl_never_converges(prompt: str, workdir) -> str:
    """NO-CONVERGE: makes an edit that does NOT satisfy acceptance (never writes
    fix.txt, just touches an unrelated path). Acceptance stays RED every attempt
    -> budget exhaustion -> (False, None, branch)."""
    (Path(workdir) / "unrelated.txt").write_text("noise\n", encoding="utf-8")
    return "edited something irrelevant"


# ---- builders the tests call ------------------------------------------------

def happy_frontend_builder(*, budget: int = 3):
    return _make_frontend_builder(impl_real_edit, budget=budget)


def noop_frontend_builder(*, budget: int = 2):
    return _make_frontend_builder(impl_noop, budget=budget)


def tamper_frontend_builder(*, budget: int = 2):
    return _make_frontend_builder(impl_tamper_acceptance, budget=budget)


def noconverge_frontend_builder(*, budget: int = 2):
    return _make_frontend_builder(impl_never_converges, budget=budget)


def nobar_frontend_builder(*, budget: int = 2):
    """A frontend whose audit WOULD yield a finding, but the bar is ABSENT
    (run_tests returns None) so DevelopFrontend.prepare blocks every round."""
    def builder(repo: Path):
        return make_develop_frontend(
            repo,
            run_agent=_one_shot_agent("seam"),
            impl_run_agent=impl_real_edit,
            dimensions=["correctness"],
            run_tests=lambda cmd: None,  # absent bar
            convergence_budget=budget,
            attest_peer="claude",
        )
    return builder
