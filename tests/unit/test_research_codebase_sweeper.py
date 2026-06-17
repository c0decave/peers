"""R5: a real research ``Sweeper`` adapter (``CodebaseSweeper``).

Honesty crown-jewel (mirrors develop's convergence seam): every witness it emits
carries a REAL ``content_hash`` = sha256 of the actually-read/fetched bytes, so
the spine's content-addressed checks re-derive to the same value. It never
fabricates a hash. The codebase modality is fully deterministic + network-free
(``git grep`` + read); the web modality runs ONLY via an injected fetcher and is
skipped honestly when none is supplied.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from peers.research.adapters import CodebaseSweeper
from peers.research.ports import Sweeper, SweepResult


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                   text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "auth.py").write_text(
        "def authenticate(token):\n    return verify_signature(token)\n", encoding="utf-8")
    (repo / "pkg" / "crypto.py").write_text(
        "def verify_signature(sig):\n    return True  # authenticate helper\n", encoding="utf-8")
    (repo / "README.md").write_text("Unrelated prose about cooking.\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return repo


# --- happy path ---------------------------------------------------------------
def test_happy_codebase_modality_finds_real_hashed_witnesses(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sw = CodebaseSweeper()
    assert isinstance(sw, Sweeper)
    out = sw.sweep("How does authenticate work?", repo, ["codebase"])
    assert isinstance(out, SweepResult)
    locs = out.code_locations
    assert locs, "expected code-location witnesses for a term present in the repo"
    assert all(w.kind == "code-location" for w in locs)
    # §5.2: all code-locations of one repo share ONE origin (the repo is one
    # source); the precise file:line lives in the witness ``uri``, not the origin.
    assert {w.resolved_origin for w in locs} == {"codebase"}
    assert any(w.uri.startswith("pkg/auth.py:") for w in locs)


def test_happy_content_hash_is_rederivable_from_disk(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    out = CodebaseSweeper().sweep("verify_signature", repo, ["codebase"])
    assert out.code_locations
    for w in out.code_locations:
        path, _line = w.uri.rsplit(":", 1)
        line_no = int(_line)
        actual = (repo / path).read_text(encoding="utf-8").splitlines()[line_no - 1]
        assert w.content_hash == hashlib.sha256(actual.encode("utf-8")).hexdigest()


# --- §5.2 regression: the repo-under-study is ONE source ----------------------
def test_codebase_witnesses_across_files_are_one_origin(tmp_path: Path) -> None:
    """§5.2 anti-self-confirmation: the repo under study is ONE source, so code
    locations across >=2 DIFFERENT files must NOT manufacture >=2 independent
    origins. Otherwise a codebase-only claim self-confirms by being echoed across
    files — exactly how a degenerate round's meta-questions reached
    ``confirmed`` with zero web citations. Same family as BUG-527/528 (origin
    over-count defeating the 5.2 rule)."""
    from peers.research.claim_ledger import classify_claim, independent_origins
    from peers.research.ports import Claim

    repo = _repo(tmp_path)
    out = CodebaseSweeper().sweep("authenticate", repo, ["codebase"])
    files = {w.uri.rsplit(":", 1)[0] for w in out.code_locations}
    assert len(files) >= 2, "fixture must hit >=2 distinct files for this test"
    # the repo is ONE source -> all code-locations share a single resolved origin.
    assert independent_origins(out.code_locations) == 1
    # consequence: a codebase-only claim is single-source (a gap), NEVER confirmed
    # (consistent with the report's URL-citation floor + 'codebase-only is dry').
    claim = Claim(id="cb", text="q", status="",
                  witnesses=list(out.code_locations), load_bearing=True)
    assert classify_claim(claim) == "single-source"


def test_codebase_does_not_suppress_real_web_confirmation(tmp_path: Path) -> None:
    """EDGE: the fix must not over-correct — two distinct WEB origins still
    confirm even when codebase locations are also present (the single codebase
    origin neither inflates nor suppresses genuine web corroboration)."""
    from peers.research.claim_ledger import CONFIRMED, classify_claim
    from peers.research.ports import Claim, Witness

    repo = _repo(tmp_path)
    out = CodebaseSweeper().sweep("authenticate", repo, ["codebase"])
    web = [Witness(kind="fetched-source", uri="https://a.example/p",
                   content_hash="h", resolved_origin="a.example"),
           Witness(kind="fetched-source", uri="https://b.example/p",
                   content_hash="h", resolved_origin="b.example")]
    claim = Claim(id="mix", text="q", status="",
                  witnesses=web + list(out.code_locations), load_bearing=True)
    assert classify_claim(claim) == CONFIRMED


# --- relevance gate: a fetched source corroborates only if it supports the claim --
def test_web_offtopic_source_is_recorded_but_yields_no_witness(tmp_path: Path) -> None:
    """Deterministic relevance gate: a fetched web source whose body does NOT
    support the sub-question (no salient-token overlap) is RECORDED for the audit
    trail but marked ``access_failure`` so it yields NO corroborating witness.
    Without this, the static seed corpus corroborates EVERY sub-question vacuously
    (the seed-origin vacuity: make_seed_url_search returns all seeds for every
    question), making the >=2-origin confirmation rule meaningless."""
    repo = _repo(tmp_path)
    off = b"<html>a recipe for sourdough bread and cake frosting</html>"
    sw = CodebaseSweeper(web_search=lambda q: ["https://ex.com/a"],
                         fetch=lambda u: (off, "ex.com"),
                         clock=lambda: "2026-06-15T00:00:00Z")
    out = sw.sweep("How does ARCH_MMAP_RND_BITS_MIN entropy work?", repo, ["web"])
    assert len(out.sources) == 1                      # recorded, not silently dropped
    s = out.sources[0]
    assert s.access_failure is not None               # off-topic -> no usable witness
    assert "off-topic" in s.access_failure.lower()


def test_web_ontopic_source_corroborates(tmp_path: Path) -> None:
    """A fetched source whose body DOES contain the sub-question's salient tokens
    corroborates the claim (``access_failure`` is None -> a real witness)."""
    repo = _repo(tmp_path)
    on = b"<html>ARCH_MMAP_RND_BITS_MIN sets the minimum mmap entropy in bits</html>"
    sw = CodebaseSweeper(web_search=lambda q: ["https://ex.com/a"],
                         fetch=lambda u: (on, "ex.com"),
                         clock=lambda: "2026-06-15T00:00:00Z")
    out = sw.sweep("How does ARCH_MMAP_RND_BITS_MIN entropy work?", repo, ["web"])
    assert len(out.sources) == 1
    assert out.sources[0].access_failure is None      # on-topic -> a usable witness


# --- sad path -----------------------------------------------------------------
def test_sad_non_utf8_matched_line_does_not_crash(tmp_path: Path) -> None:
    # HS-R2: a tracked non-UTF-8 line matched by the grep must not raise
    # UnicodeDecodeError and abort the whole research run.
    repo = _repo(tmp_path)
    (repo / "bin.txt").write_bytes(b"caf\xe9 alpha_token here\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "bin")
    out = CodebaseSweeper().sweep("alpha_token", repo, ["codebase"])
    assert isinstance(out, SweepResult)   # no raise; degrades gracefully


def test_edge_colon_in_filename_content_hash_rederives(tmp_path: Path) -> None:
    # HS-R3: a filename containing ':' must not corrupt the witness — uri,
    # resolved_origin, and the re-derivable content_hash must all be correct.
    repo = _repo(tmp_path)
    (repo / "has:colon.txt").write_text("weird verify_signature here\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "colon")
    out = CodebaseSweeper().sweep("verify_signature", repo, ["codebase"])
    ws = [w for w in out.code_locations if w.uri.startswith("has:colon.txt:")]
    assert ws, "expected a witness for the colon-named file"
    w = ws[0]
    path, line = w.uri.rsplit(":", 1)
    assert path == "has:colon.txt"
    actual = (repo / path).read_text(encoding="utf-8").splitlines()[int(line) - 1]
    assert w.content_hash == hashlib.sha256(actual.encode("utf-8")).hexdigest()


def test_sad_no_match_yields_no_fabricated_witnesses(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    out = CodebaseSweeper().sweep("quantum chromodynamics tachyon", repo, ["codebase"])
    assert out.code_locations == []
    assert out.sources == []


# --- edge cases ---------------------------------------------------------------
def test_edge_web_modality_without_fetcher_is_skipped(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    out = CodebaseSweeper().sweep("authenticate", repo, ["web"])
    assert out.sources == []   # no fetcher -> honest skip, no fabricated source


def test_edge_web_modality_with_fetcher_hashes_real_body(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    # body is on-topic for the sub-question ("authenticate") so the relevance gate
    # keeps it as a usable witness — this test pins the hash re-derivation, not the
    # relevance gate (covered separately).
    body = b"<html>real fetched bytes about authenticate</html>"

    def fake_search(_q: str) -> list[str]:
        return ["https://example.com/a"]

    def fake_fetch(url: str):
        return (body, "example.com")   # (bytes, resolved_origin)

    sw = CodebaseSweeper(web_search=fake_search, fetch=fake_fetch,
                         clock=lambda: "2026-06-14T00:00:00Z")
    out = sw.sweep("authenticate", repo, ["web"])
    assert len(out.sources) == 1
    s = out.sources[0]
    assert s.resolved_origin == "example.com"
    assert s.content_hash == hashlib.sha256(body).hexdigest()
    assert s.access_failure is None


def test_edge_failed_fetch_recorded_not_dropped_no_witness(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    def fake_fetch(url: str):
        return None   # fetch failed

    sw = CodebaseSweeper(web_search=lambda q: ["https://x/y"], fetch=fake_fetch,
                         clock=lambda: "2026-06-14T00:00:00Z")
    out = sw.sweep("authenticate", repo, ["web"])
    assert len(out.sources) == 1
    assert out.sources[0].access_failure is not None   # recorded as a failure
