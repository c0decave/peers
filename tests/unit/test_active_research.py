"""ACTIVE tests for `peers research` — the operator-runnable autonomous research
mode (decompose -> sweep -> verify -> classify -> synthesize a URL-cited
RESEARCH.md committed+attested on the current branch), plus the opt-in,
allowlisted, SSRF-guarded web fetcher.

Plan: docs/plans/2026-06-15-new-feature-active-test-plans.md (section 2, T1..T6).
Each test encodes that case's "Observable pass/fail" PASS criteria AND its
load-bearing HONESTY CHECK: trust is RE-DERIVED from the substrate — a real git
commit reachable from the chosen head, the author re-resolved from the
``refs/notes/peers-attest`` note (never a self-reported row field), and the
file-witness re-hashed from disk. A LYING / no-op / SSRF'd agent cannot forge any
of these.

Deterministic by construction: tmp git repos under ``tmp_path``, the documented
``cmd_research(..., _make_frontend=...)`` injection seam (cli.py:2696) over the
REAL adapters (LLMDecomposer / CodebaseSweeper / ReportSynthesizer /
ReportCommitter, the real check_report gate, the real append_attested + spine
gates), tiny budgets — NO live LLM, NO container, NO network.
"""
from __future__ import annotations

import io
import os
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

from peers.cli import cmd_research
from peers.research.assembly import make_research_frontend
from peers.research.frontend import ResearchFrontend
from peers.research.ports import CompletenessVerdict, SweepResult
from peers.research.web_fetch import AllowlistedFetcher
from peers.spine.gates import all_pass, evaluate_spine_gates
from peers.spine.ledger import RunLedger
from peers.spine.mode_run import ModeRun, drive
from peers.spine.op_config import OpConfig
from peers.spine.stop_on_dry import dry_streak

from tests.unit._active_research_fixtures import (
    TOPIC,
    FabricatedShaCommitter,
    RecordingTransport,
    UnattestedCommitter,
    attest_note,
    dispatching_run_agent,
    head,
    make_repo,
)
from tests.unit._research_helpers import (
    _AlwaysSynth,
    _FixedCritic,
    _FixedDecomposer,
    _FixedSweeper,
    _repo_with_commit,
    _src,
    _topic,
)

# A real one-shot decomposer question whose tokens (authenticate / verify /
# signature) git-grep-match the code file + TOPIC.md in make_repo().
_Q = "How does authenticate verify a token signature end to end?"


def _rows(repo: Path) -> list:
    return RunLedger(repo / ".peers" / "run.jsonl").read()


def _committed_files(repo: Path, rev: str = "HEAD") -> list[str]:
    r = subprocess.run(
        ["git", "-C", str(repo), "show", "--stat", "--name-only", "--format=", rev],
        capture_output=True, text=True, check=True,
    )
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def _is_tracked(repo: Path, pathspec: str) -> bool:
    r = subprocess.run(["git", "-C", str(repo), "ls-files", pathspec],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


def _once_search(urls: list[str]):
    """A web_search returning ``urls`` on the FIRST call only, then ``[]`` — so a
    web run confirms exactly ONE round (round 1) then sweeps nothing and stops on
    dry. A single confirmed-work round keeps the ``witness-ledgered`` gate green:
    only the LAST RESEARCH.md is on disk, so multiple confirmed rounds whose
    earlier witnesses point at overwritten content would fail the all() re-hash."""
    state = {"n": 0}

    def search(_q: str) -> list[str]:
        state["n"] += 1
        return list(urls) if state["n"] == 1 else []

    return search


def _origin_fetch():
    """A deterministic fetch: each URL resolves to the origin encoded as its host
    (so https://a.example/* -> 'a.example', https://b.example/* -> 'b.example' are
    two distinct origins that corroborate one claim)."""
    from urllib.parse import urlsplit

    def fetch(url: str):
        host = urlsplit(url).hostname or url
        # On-topic body: contains the sub-question's salient tokens (authenticate /
        # verify / token / signature) so the deterministic relevance gate keeps it
        # as a usable corroborating witness (a real corroborator mentions the topic).
        return (b"how authenticate and verify a token signature works: "
                + url.encode(), host)

    return fetch


# ---------------------------------------------------------------------------
# T1 — happy: a valid codebase-only run gathers real evidence but ends HONESTLY
# DRY (the URL-citation floor by design), with no committable/attested report.
# ---------------------------------------------------------------------------
def test_res_happy_01_codebase_honest_dry(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    base = head(repo)
    ra = dispatching_run_agent(sub_questions=[_Q], refuted=False)

    def mk(r: Path) -> ResearchFrontend:
        return make_research_frontend(
            r, run_agent=ra, modalities=["codebase"], attest_peer="claude",
            run_tests=lambda c: (0, "ok"))

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_research(repo, modalities=["codebase"], _make_frontend=mk)
    out = buf.getvalue()
    rows = _rows(repo)

    assert rc == 0
    # Real evidence WAS gathered: at least one sweep row carries code-location
    # witnesses (git-grep hits) — the run is not vacuously dry.
    sweeps = [r for r in rows if r.event == "sweep"]
    assert sweeps and max(r.witness["code_locations"] for r in sweeps) >= 1
    # ...yet NO committable report was forged from non-URL witnesses.
    assert not any(r.event in ("confirmed-work", "landing") for r in rows)
    assert rows[-1].event == "stop"
    # The legible terminal note about the URL-citation floor (cli.py:2713-2718).
    assert "NO committable report" in out and "primary-source URL" in out
    # HONESTY CHECK: no RESEARCH.md was committed and HEAD is unchanged — a no-op
    # that "reported success" would have to land an attested commit, which the
    # dry termination + unchanged substrate disproves. (RESEARCH.md MAY exist on
    # disk as a synthesize attempt that check_report rejected for lack of URLs;
    # what matters is it never entered the attested history.)
    assert head(repo) == base
    assert not _is_tracked(repo, "RESEARCH.md")


# ---------------------------------------------------------------------------
# T2 — happy: a web-modality run with two origin-independent fake sources drives
# to a REAL committed+attested RESEARCH.md that passes ALL FOUR spine gates.
# ---------------------------------------------------------------------------
def test_res_happy_02_web_confirmed_attested(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    base = head(repo)
    ra = dispatching_run_agent(
        sub_questions=[_Q], refuted=False, narrative="The auth path verifies tokens.")

    def mk(r: Path) -> ResearchFrontend:
        return make_research_frontend(
            r, run_agent=ra, modalities=["web"], attest_peer="claude",
            web_search=_once_search(["https://a.example/x", "https://b.example/y"]),
            fetch=_origin_fetch(), run_tests=lambda c: (0, "ok"))

    rc = cmd_research(repo, modalities=["web"], _make_frontend=mk)
    rows = _rows(repo)
    h = head(repo)

    assert rc == 0
    # A new commit landed and RESEARCH.md is in it.
    assert h != base
    assert "RESEARCH.md" in _committed_files(repo, h)
    # Exactly one confirmed-work unit, independence=True, author == the peer.
    cw = [r for r in rows if r.event == "confirmed-work"]
    assert len(cw) == 1
    row = cw[0]
    assert row.independence is True
    assert row.author == "claude"
    # The file witness re-hashes from disk and the attest_sha == HEAD.
    import hashlib
    disk_sha = hashlib.sha256((repo / "RESEARCH.md").read_bytes()).hexdigest()
    assert row.witness["sha256"] == disk_sha
    assert row.witness["attest_sha"] == h
    # HONESTY CHECK: trust is re-derivable from the SUBSTRATE, independently of
    # the row. (1) the git note attributes HEAD to the peer; (2) all four gates
    # pass when re-derived (author from note, attest_sha reachable from head,
    # file witness re-hashed from disk).
    assert attest_note(repo, "HEAD") == "claude"
    gates = evaluate_spine_gates(
        rows, mode_run=f"research-{repo.name}", repo=repo, head=h)
    assert gates["authorship-attested"] is True
    assert all_pass(gates) is True


def test_res_offtopic_web_sources_do_not_confirm_seed_vacuity(tmp_path: Path) -> None:
    """Regression lock for the seed-origin vacuity (deterministic relevance gate):
    web sources whose CONTENT does NOT support the sub-question must NOT corroborate
    the claim, even though >=2 DISTINCT origins were fetched. The round ends honest
    DRY with no committed RESEARCH.md — '>=2 origins' now means '>=2 sources that
    mention the answer', not '>=2 seeds were fetched'. (Inverts T2: same 2 distinct
    origins, but off-topic bodies -> the relevance gate withholds corroboration.)"""
    repo = make_repo(tmp_path)
    base = head(repo)
    ra = dispatching_run_agent(sub_questions=[_Q], refuted=False, narrative="ev")

    def offtopic_fetch():
        from urllib.parse import urlsplit

        def fetch(url: str):
            host = urlsplit(url).hostname or url      # 2 distinct origins fetched...
            return (b"a recipe for sourdough bread and cake frosting " + url.encode(),
                    host)                             # ...but neither supports _Q
        return fetch

    def mk(r: Path) -> ResearchFrontend:
        return make_research_frontend(
            r, run_agent=ra, modalities=["web"], attest_peer="claude",
            web_search=_once_search(["https://a.example/x", "https://b.example/y"]),
            fetch=offtopic_fetch(), run_tests=lambda c: (0, "ok"))

    rc = cmd_research(repo, modalities=["web"], _make_frontend=mk)
    rows = _rows(repo)
    assert rc == 0
    assert not any(r.event == "confirmed-work" for r in rows), \
        "off-topic sources must not corroborate (>=2 fetched != >=2 supporting)"
    assert head(repo) == base                          # no RESEARCH.md commit landed
    assert "RESEARCH.md" not in _committed_files(repo, "HEAD")


# ---------------------------------------------------------------------------
# T3 — sad: missing repo, missing/vacuous TOPIC.md, and empty --modalities each
# fail CLOSED with the right non-zero exit and no commit.
# ---------------------------------------------------------------------------
def test_res_sad_03_missing_and_invalid_input(tmp_path: Path) -> None:
    # (a) non-existent path -> validate_git_repo fails -> rc 1.
    assert cmd_research(tmp_path / "nope", modalities=["codebase"]) == 1

    # (b) a real git repo with NO TOPIC.md -> intake fails closed -> rc 1.
    no_topic = make_repo(tmp_path, "no_topic", topic=False)
    assert cmd_research(no_topic, modalities=["codebase"]) == 1

    # (c) a TOPIC.md whose `## Scope` body is < 60 chars (vacuous) -> rc 1.
    vac = make_repo(tmp_path, "vacuous", topic=False)
    (vac / "TOPIC.md").write_text(
        "# T\n\n## Scope\ntoo short\n\n## Questions\n- q?\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(vac), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(vac), "commit", "-qm", "topic"], check=True,
                   capture_output=True)
    assert cmd_research(vac, modalities=["codebase"]) == 1

    # (d) a valid repo but EMPTY modalities -> the explicit rc 2 contract
    # (cli.py:2676-2678; dispatch splits '' to [] at cli.py:2872).
    ok_repo = make_repo(tmp_path, "ok")
    assert cmd_research(ok_repo, modalities=[]) == 2

    # HONESTY CHECK: the mode refused to manufacture a result from absent/invalid
    # input — no RESEARCH.md was committed in the one case that has a HEAD.
    assert not _is_tracked(ok_repo, "RESEARCH.md")
    assert not _is_tracked(vac, "RESEARCH.md")


# ---------------------------------------------------------------------------
# T4 — sad: a LYING / no-op agent that returns success WITHOUT doing the work
# cannot forge a converged/attested/confirmed result.
# ---------------------------------------------------------------------------
def test_res_sad_04_lying_noop_agent_cannot_forge_confirm(tmp_path: Path) -> None:
    # A REAL commit with NO peers-attest note: the committer FAKES ok=True on it.
    sha = _repo_with_commit(tmp_path)
    _topic(tmp_path)
    fe = ResearchFrontend(
        decomposer=_FixedDecomposer(["q1"]),
        # Sweeper greens every round (two distinct origins), so only the substrate
        # authorship layer can refuse — not the corroboration count.
        sweeper=_FixedSweeper(SweepResult(
            sources=[_src("https://a.example/x", "a.example"),
                     _src("https://b.example/y", "b.example")], code_locations=[])),
        synthesizer=_AlwaysSynth(tmp_path),
        committer=UnattestedCommitter(sha),
        critic=_FixedCritic(CompletenessVerdict(state="work-done", not_checked=[])),
        modalities=["web"], run_tests=lambda c: None,
        refuter_factory=lambda c: (lambda i: False), k=2)
    run = ModeRun(tool=tmp_path, op_config=OpConfig.from_dict({"mode": "research"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1")
    drive(run, fe)
    rows = run.ledger.read()

    cw = [r for r in rows if r.event == "confirmed-work"]
    # The fake confirm wrote rows, but append_attested over an UNATTESTED sha
    # resolves author=None (ledger.py:305-307) — the row cannot self-author.
    assert cw and cw[-1].author is None
    # The author-None confirm did NOT reset stop-on-dry: the streak still reached
    # dry_n and the run terminated dry.
    assert dry_streak(rows) >= run.op_config.dry_n
    assert rows[-1].event == "stop" and rows[-1].status == "dry"
    # HONESTY CHECK: success is re-derived from the substrate note, never from the
    # row's self-reported author/independence -> the authorship gate stays False.
    assert evaluate_spine_gates(
        rows, mode_run="r1", repo=tmp_path, head=sha)["authorship-attested"] is False

    # Variant B: a FABRICATED sha that resolves to no commit -> the frontend's
    # resolves_to_commit rejector (frontend.py:313) degrades it to a dry round,
    # so NO confirmed-work row is ever written.
    tmp2 = tmp_path / "b"
    tmp2.mkdir()
    _repo_with_commit(tmp2)
    _topic(tmp2)
    fe_b = ResearchFrontend(
        decomposer=_FixedDecomposer(["q1"]),
        sweeper=_FixedSweeper(SweepResult(
            sources=[_src("https://a.example/x", "a.example"),
                     _src("https://b.example/y", "b.example")], code_locations=[])),
        synthesizer=_AlwaysSynth(tmp2),
        committer=FabricatedShaCommitter("0" * 40),
        critic=_FixedCritic(CompletenessVerdict(state="work-done", not_checked=[])),
        modalities=["web"], run_tests=lambda c: None,
        refuter_factory=lambda c: (lambda i: False), k=2)
    run_b = ModeRun(tool=tmp2, op_config=OpConfig.from_dict({"mode": "research"}),
                    ledger_path=tmp2 / "run.jsonl", mode_run="rb")
    drive(run_b, fe_b)
    rows_b = run_b.ledger.read()
    assert not any(r.event == "confirmed-work" for r in rows_b)
    assert rows_b[-1].event == "stop" and rows_b[-1].status == "dry"


# ---------------------------------------------------------------------------
# T5 — sad: the opt-in web fetcher refuses SSRF vectors and records an honest
# access_failure, never a leaked body.
# ---------------------------------------------------------------------------
def test_res_sad_05_web_fetcher_ssrf_guards(tmp_path: Path) -> None:
    # --- unit level over AllowlistedFetcher.fetch (the seam cli.py wires) ---
    transport = RecordingTransport(status=200, body=b"OK")
    fetcher = AllowlistedFetcher(allow=[r"docs\.example\.com"], transport=transport)

    # IP literals/encodings, loopback, metadata, non-http schemes -> refused
    # BEFORE the transport runs (pre-transport _url_host_ok guard).
    for blocked in (
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "http://2130706433/x",                         # decimal 127.0.0.1
        "http://127.0.0.1/x",                          # loopback literal
        "http://localhost/x",                          # localhost name
        "http://[::1]/x",                              # IPv6 loopback
        "file:///etc/passwd",                          # non-http scheme
        "http://docs.example.com.evil.test/x",         # allow-suffix smuggle
    ):
        assert fetcher.fetch(blocked) is None, blocked
    assert transport.calls == []  # transport NEVER reached for a refused host

    # The critical redirect case: an ALLOW-listed start URL whose transport lands
    # (final_url) on a private host returns None even with status 200 + a body
    # (web_fetch.py:117 final-URL re-validate). The body must NOT surface.
    redirect = RecordingTransport(
        status=200, body=b"INTERNAL-SECRET", final_url="http://127.0.0.2/meta")
    fetcher2 = AllowlistedFetcher(allow=[r"docs\.example\.com"], transport=redirect)
    assert fetcher2.fetch("http://docs.example.com/page") is None
    assert redirect.calls == ["http://docs.example.com/page"]  # transport ran...
    # ...but a clean allow-listed final URL DOES return the body (the guard is
    # not just always-deny — green for the right reason).
    clean = RecordingTransport(
        status=200, body=b"GOOD", final_url="http://docs.example.com/page")
    fetcher3 = AllowlistedFetcher(allow=[r"docs\.example\.com"], transport=clean)
    assert fetcher3.fetch("http://docs.example.com/page") == (
        b"GOOD", "http://docs.example.com/page")

    # --- end-to-end: a sweep whose only source is a refused (redirect-to-private)
    # fetch records an access_failure -> no usable witness -> the round ends dry,
    # and INTERNAL-SECRET never reaches an (uncommitted) report.
    repo = make_repo(tmp_path)
    ra = dispatching_run_agent(sub_questions=[_Q], refuted=False, narrative="ev")
    e2e_transport = RecordingTransport(
        status=200, body=b"INTERNAL-SECRET", final_url="http://127.0.0.2/meta")
    e2e_fetcher = AllowlistedFetcher(
        allow=[r"docs\.example\.com"], transport=e2e_transport)

    def mk(r: Path) -> ResearchFrontend:
        return make_research_frontend(
            r, run_agent=ra, modalities=["web"], attest_peer="claude",
            web_search=lambda q: ["http://docs.example.com/page"],
            fetch=e2e_fetcher.fetch, run_tests=lambda c: (0, "ok"))

    rc = cmd_research(repo, modalities=["web"], _make_frontend=mk)
    rows = _rows(repo)
    assert rc == 0
    assert not any(r.event == "confirmed-work" for r in rows)
    assert rows[-1].event == "stop" and rows[-1].status == "dry"
    # HONESTY CHECK: a refused fetch can never corroborate a claim, and the secret
    # body never entered any committed artifact.
    assert not _is_tracked(repo, "RESEARCH.md")
    research = repo / "RESEARCH.md"
    if research.exists():
        assert b"INTERNAL-SECRET" not in research.read_bytes()


# ---------------------------------------------------------------------------
# T6 — edge: empty repo, gitignored .peers control plane, read-only repo, and a
# single-origin (no-convergence) round all terminate honestly without crashing.
# ---------------------------------------------------------------------------
def test_res_edge_06_empty_gitignored_readonly_no_convergence(tmp_path: Path) -> None:
    ra = dispatching_run_agent(sub_questions=[_Q], refuted=False, narrative="ev")

    # (a) an EMPTY git repo (init, ZERO commits) + a TOPIC.md: validate_git_repo
    # rejects "no commits" -> rc 1, no crash/traceback, no commit produced.
    empty = tmp_path / "empty"
    empty.mkdir()
    subprocess.run(["git", "-C", str(empty), "init", "-q"], check=True)
    (empty / "TOPIC.md").write_text(TOPIC, encoding="utf-8")
    assert cmd_research(empty, modalities=["codebase"]) == 1

    # (b) gitignored .peers/: the run still reads/writes .peers/run.jsonl, and the
    # attested commit lists ONLY RESEARCH.md (RC-01 pathspec commit) — .peers is
    # never swept into the attested unit.
    inited = make_repo(tmp_path, "inited")
    (inited / ".gitignore").write_text(".peers/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(inited), "add", ".gitignore"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(inited), "commit", "-qm", "ignore .peers"],
                   check=True, capture_output=True)

    def mk(r: Path) -> ResearchFrontend:
        return make_research_frontend(
            r, run_agent=ra, modalities=["web"], attest_peer="claude",
            web_search=_once_search(["https://a.example/x", "https://b.example/y"]),
            fetch=_origin_fetch(), run_tests=lambda c: (0, "ok"))

    rc = cmd_research(inited, modalities=["web"], _make_frontend=mk)
    assert rc == 0
    assert (inited / ".peers" / "run.jsonl").exists()  # ledger under gitignored dir
    assert _committed_files(inited, "HEAD") == ["RESEARCH.md"]  # ONLY the report
    assert not _is_tracked(inited, ".peers")
    assert not _is_tracked(inited, ".peers/run.jsonl")

    # (c) a READ-ONLY repo so .peers/ cannot be created -> rc 1, clean message, no
    # crash. (Skipped under root, where chmod a-w does not block writes.)
    if os.geteuid() != 0:
        ro = make_repo(tmp_path, "readonly")
        os.chmod(ro, 0o555)
        try:
            assert cmd_research(ro, modalities=["codebase"]) == 1
        finally:
            os.chmod(ro, 0o755)

    # (d) single resolved_origin (two URLs share one host) -> the claim classifies
    # single-source (independent_origins == 1), routed to gaps, the round ends dry
    # with NO confirmed-work (the >= 2-origin convergence threshold is real).
    single = make_repo(tmp_path, "single")

    def mk_single(r: Path) -> ResearchFrontend:
        return make_research_frontend(
            r, run_agent=ra, modalities=["web"], attest_peer="claude",
            web_search=lambda q: ["https://a.example/x", "https://a.example/y"],
            fetch=_origin_fetch(), run_tests=lambda c: (0, "ok"))

    rc_s = cmd_research(single, modalities=["web"], _make_frontend=mk_single)
    rows_s = _rows(single)
    assert rc_s == 0
    claims = [r for r in rows_s if r.event == "claim"]
    assert claims and all(c.witness["status"] == "single-source"
                          and c.witness["independent_origins"] == 1 for c in claims)
    assert not any(r.event == "confirmed-work" for r in rows_s)
    assert rows_s[-1].event == "stop" and rows_s[-1].status == "dry"
    # HONESTY CHECK: no single-origin evidence ever produced a confirmed unit, and
    # no RESEARCH.md entered the attested history on the single-source repo.
    assert not _is_tracked(single, "RESEARCH.md")
