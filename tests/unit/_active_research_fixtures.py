"""Area-specific fixtures for ``test_active_research.py`` (the `peers research`
active-test plan: T1..T6).

Underscore-prefixed -> NOT a test module; imported as
``from tests.unit._active_research_fixtures import ...``.

These do NOT duplicate ``tests/unit/_research_helpers.py``; they add the
*adversarial* fakes the active plan needs that the existing helper suite lacks:

  * :func:`dispatching_run_agent` — a deterministic, prompt-routing one-shot
    ``run_agent`` so a SINGLE fake can serve the real ``LLMDecomposer`` (JSON
    array of sub-questions), the real ``LLMClaimRefuter``
    (``{"refuted": false}`` so the claim survives the k-vote), AND the real
    report renderer narrative — driving the production adapters end to end with
    no live model (T2).
  * :func:`make_repo` — a real one-commit git repo with a code token + a
    non-vacuous TOPIC.md, mirroring ``test_cli_research.py:_repo`` but reusable
    here without editing that file.
  * :class:`UnattestedCommitter` — a LYING committer that returns
    ``CommitResult(ok=True)`` over a REAL-but-UNATTESTED commit (the no-op forge
    of T4): it controls neither ``refs/notes/peers-attest`` nor the file hash,
    so the substrate authorship gate must stay False.
  * :class:`RecordingTransport` — a fake web transport recording every call so a
    pre-transport SSRF refusal is observable as ``calls == []`` (T5).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from peers.research.ports import CommitResult


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *a], capture_output=True, text=True, check=True,
    ).stdout.strip()


# A non-vacuous generic TOPIC.md (## Scope >= 60 chars + ## Questions; the
# ``authenticate``/``verify_signature`` tokens give the codebase sweeper a real
# git-grep hit against the code file written by make_repo()).
TOPIC = (
    "# T\n\n## Scope\n"
    "Investigate how the authentication subsystem verifies tokens and where "
    "signature checking happens across the codebase modules in this project.\n\n"
    "## Questions\n"
    "- How does the authenticate function verify a token end to end?\n"
    "- Which module implements verify_signature and what does it return?\n"
)


def make_repo(
    tmp_path: Path, name: str = "proj", *, topic: bool = True, code: bool = True,
) -> Path:
    """A real single-commit git repo: a code file (grep hit) + a TOPIC.md.

    Mirrors ``tests/unit/test_cli_research.py:_repo`` so the active tests drive
    the SAME intake/sweep path the seam tests prove, without editing that file.
    """
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    if code:
        (repo / "code.py").write_text(
            "def authenticate():\n    return verify_signature()\n"
            "def verify_signature():\n    return True\n",
            encoding="utf-8",
        )
    if topic:
        (repo / "TOPIC.md").write_text(TOPIC, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def attest_note(repo: Path, rev: str = "HEAD") -> str | None:
    """The raw ``refs/notes/peers-attest`` value for ``rev``, or None — the
    substrate signal the honesty gate re-derives (never trust the row field)."""
    r = subprocess.run(
        ["git", "-C", str(repo), "notes", "--ref=refs/notes/peers-attest",
         "show", rev],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def dispatching_run_agent(
    *, sub_questions: list[str], refuted: bool = False, narrative: str = "ev",
):
    """Build a deterministic ``run_agent(prompt) -> text`` that routes by the
    real adapters' prompt shapes:

      * the ``LLMDecomposer`` prompt ("Return ONLY a JSON array ...") ->
        ``json.dumps(sub_questions)``;
      * the ``LLMClaimRefuter`` prompt ('... {"refuted": true|false} ...') ->
        ``{"refuted": refuted}`` (refuted=False lets the claim survive the
        k-vote so a corroborated claim can confirm);
      * the report-renderer prompt ("Write the NARRATIVE body ...") ->
        ``narrative`` (markdown prose).

    Any other prompt returns ``"[]"`` (fail-safe: an unrouted prompt never
    fabricates evidence).
    """
    arr = json.dumps(list(sub_questions))

    def run_agent(prompt: str) -> str:
        p = prompt or ""
        if "Return ONLY a JSON array" in p:
            return arr
        if '"refuted"' in p:
            return json.dumps({"refuted": bool(refuted)})
        if "Write the NARRATIVE body" in p:
            return narrative
        return "[]"

    return run_agent


class UnattestedCommitter:
    """A LYING :class:`peers.research.ports.Committer`: reports ``ok=True`` over a
    REAL commit that carries NO ``peers-attest`` note (the T4 no-op forge).

    It returns the file's true on-disk hash path untouched (so the file-witness
    layer can pass) but it cannot mint a substrate authorship note — so
    ``append_attested`` over its ``head_sha`` resolves ``author=None`` and the
    ``authorship-attested`` gate fails closed."""

    def __init__(self, head_sha: str, branch: str = "research/x") -> None:
        self.head_sha = head_sha
        self.branch = branch

    def implement(self, report, repo):  # noqa: ANN001 — Protocol signature
        return CommitResult(ok=True, head_sha=self.head_sha, branch=self.branch)


class FabricatedShaCommitter:
    """A committer that returns ``ok=True`` with a fabricated 40-hex SHA that
    resolves to NO commit (the ``resolves_to_commit`` rejector path,
    frontend.py:313). The run must degrade to a dry round, never green."""

    def __init__(self, sha: str = "0" * 40, branch: str = "research/x") -> None:
        self.sha = sha
        self.branch = branch

    def implement(self, report, repo):  # noqa: ANN001 — Protocol signature
        return CommitResult(ok=True, head_sha=self.sha, branch=self.branch)


class RecordingTransport:
    """A fake web transport recording every URL it is asked to fetch.

    ``transport(url) -> (status, body, final_url)``. ``calls`` stays ``[]`` when
    the fetcher refuses a URL BEFORE the transport runs (the pre-transport SSRF
    guard), which is exactly the observable T5 PASS criterion. ``final_url`` lets
    a test simulate a redirect whose landed host is private (the final-URL
    re-validate layer)."""

    def __init__(self, status: int = 200, body: bytes = b"OK",
                 final_url: str | None = None) -> None:
        self.status = status
        self.body = body
        self.final_url = final_url
        self.calls: list[str] = []

    def __call__(self, url: str) -> "tuple[int, bytes, str]":
        self.calls.append(url)
        return (self.status, self.body, self.final_url or url)
