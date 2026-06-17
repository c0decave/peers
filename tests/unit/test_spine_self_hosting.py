# tests/unit/test_spine_self_hosting.py
"""STEP-1: the pure, fail-safe self-hosting detector (S1).

Every uncertain input must resolve to ``True`` (self-hosting => branch-pr =>
human review). Three independent layers force ``True``: the target-identity
layer, the empty/error layer, and the path-glob governance layer.
"""
import pytest
from peers.spine.self_hosting import is_self_hosting, GOVERNANCE_GLOBS


def test_docs_only_external_change_is_trusted(tmp_path):
    flag, reason = is_self_hosting(tmp_path, changed_paths=["README.md", "docs/g.md"])
    assert flag is False                         # trusted: no governance touch, repo != peers


@pytest.mark.parametrize("p", [
    "src/peers/spine/landing.py",                # the spine itself
    "src/peers/spine/gates.py",                  # gate definitions
    "src/peers/spine/op_config.py",              # the intake allow/deny list
    "src/peers/spine/authorship.py",             # attestation seam
    "src/peers/spine/sub/deep.py",               # NESTED spine subtree (segment-aware)
    "src/peers/attest.py",                       # the substrate attestation
    "src/peers/op_config.py",                    # the (top-level) intake config
    "src/peers/async_gate_runner.py",            # the gate runner
    "src/peers/anti_cheat_guard.py",             # anti-cheat enforcement
    "src/peers/safe_io.py",                      # the fail-closed write primitive
    "src/peers/structured_halt.py",              # the echo-immune halt
    "src/peers/goals.py",                        # gate/goal definitions
    "src/peers/goal_engine.py",                  # goal engine
    "src/peers/goal_reload.py",                  # goal reload
    "src/peers/driver_gate_pipeline.py",         # the gate pipeline
    "src/peers/driver_tick_hooks.py",            # attest tick hooks
    "src/peers/driver_soft_reviews.py",          # soft-review hooks
    "src/peers/research/checks/anchored.py",     # a bundled gate CHECK body
    "src/peers/templates/modes/audit/checks/x.py",  # nested mode-check body
    "src/peers/templates/modes/audit/goals.yaml",   # a gate-input manifest
    "src/peers/templates/checks.sha256",         # a check digest manifest
    ".gitattributes",                            # diff-behaviour config (quotePath/textconv)
    "pyproject.toml",                            # entrypoints + pytest/test config
    "setup.cfg",                                 # packaging/config
    ".peers/goals.yaml",                         # .peers governance
    ".peers/checks/no_shortcut.py",              # .peers governance subtree
])
def test_governance_touch_is_self_hosting(tmp_path, p):
    flag, reason = is_self_hosting(tmp_path, changed_paths=[p])
    assert flag is True and reason                # non-empty reason naming the cause


def test_one_governance_path_among_docs_is_self_hosting(tmp_path):
    flag, reason = is_self_hosting(
        tmp_path, changed_paths=["README.md", "src/x.py", "src/peers/spine/gates.py"])
    assert flag is True                           # any-touch -> not trusted


def test_rename_out_of_governance_is_self_hosting(tmp_path):
    # B1 regression: `git mv src/peers/spine/gates.py innocent.py` with --no-renames
    # surfaces BOTH the deleted governance path AND the innocent add. The detector
    # must fire on the surviving governance path. (The diff-side --no-renames is
    # enforced at the wire points; here we assert the detector catches the delete.)
    flag, reason = is_self_hosting(
        tmp_path, changed_paths=["innocent.py", "src/peers/spine/gates.py"])
    assert flag is True and "spine" in reason


@pytest.mark.parametrize("p", [
    '"src/peers/spine/\\303\\274nicode.py"',     # C-quoted (leading quote + octal)
    'src/peers/spine/ünïcode.py',       # raw unicode (the -z form) -> still matches
])
def test_quoted_or_unicode_governance_path_is_self_hosting(tmp_path, p):
    # B2 regression: a quotePath-C-quoted path (leading ") fails safe to True
    # (a legitimately-tracked source path never starts with a quote); the raw -z
    # unicode form matches the spine prefix directly.
    assert is_self_hosting(tmp_path, changed_paths=[p])[0] is True


def test_none_changed_paths_is_self_hosting(tmp_path):
    assert is_self_hosting(tmp_path, changed_paths=None)[0] is True


def test_empty_changed_paths_is_self_hosting(tmp_path):
    # an empty diff is "we could not determine what changed" -> fail-safe True,
    # never "a no-op is trusted" (S1). EXPLICIT regression: no "empty == no-op
    # == trusted" shortcut is ever admissible.
    flag, reason = is_self_hosting(tmp_path, changed_paths=[])
    assert flag is True and reason == "empty-diff"


@pytest.mark.parametrize("bad", ["../escape.py", "../../etc/passwd", "/etc/passwd",
                                 "src/../../../x", "a/../../b"])
def test_unnormalizable_path_is_self_hosting(tmp_path, bad):
    assert is_self_hosting(tmp_path, changed_paths=[bad])[0] is True   # uncertain -> True


def test_control_byte_path_fails_safe(tmp_path):
    # a path carrying an embedded NUL / control byte is un-normalizable (a real -z
    # diff NUL-delimits BETWEEN paths, so a NUL INSIDE a path is malformed/uncertain)
    # -> _normalize returns None -> self-hosting (S1). This is the symlink/indirection
    # fail-safe seam: a changed path the detector cannot statically canonicalise to a
    # regular tree path is treated as uncertain, never trusted.
    assert is_self_hosting(tmp_path, changed_paths=["link\x00gates.py"])[0] is True


def test_target_is_peers_root_is_self_hosting(tmp_path, monkeypatch):
    # B3: a target_repo whose REPO IDENTITY is peers -> self-hosting regardless of an
    # otherwise-trusted (docs-only) diff. Identity is by resolved git common-dir OR
    # the sentinel marker (src/peers/spine/ present + pyproject name == "peers").
    import peers.spine.self_hosting as sh
    monkeypatch.setattr(sh, "_repo_identity", lambda p: ("peers-common-dir", True))
    monkeypatch.setattr(sh, "_peers_identity", lambda: ("peers-common-dir", True))
    flag, reason = is_self_hosting(tmp_path, changed_paths=["README.md"],
                                   target_repo="/peers")
    assert flag is True and reason == "target-is-peers"


def test_target_is_peers_worktree_under_tmp_is_self_hosting(tmp_path, monkeypatch):
    # B3 (the dogfood case): in run_isolated, run.tool is a /tmp worktree of peers
    # whose path != peers' source root, but whose git common-dir IS peers'. The
    # identity layer must fire by common-dir, not literal path equality.
    import peers.spine.self_hosting as sh
    # the worktree resolves to peers' common-dir; sentinel absent in the leaf
    monkeypatch.setattr(sh, "_common_dir", lambda p: "~/peers/.git")
    monkeypatch.setattr(sh, "_peers_common_dir", lambda: "~/peers/.git")
    monkeypatch.setattr(sh, "_has_peers_sentinel", lambda p: False)
    flag, reason = is_self_hosting(tmp_path, changed_paths=["README.md"],
                                   target_repo="/tmp/peers-run-xyz/r1")
    assert flag is True and reason == "target-is-peers"


def test_target_is_peers_by_sentinel_marker(tmp_path, monkeypatch):
    # B3 (a copy of peers with a DIFFERENT git common-dir, e.g. a fresh `git init`
    # over a peers checkout) -> caught by the sentinel marker (src/peers/spine/ +
    # pyproject name == "peers"), even though the common-dir differs.
    import peers.spine.self_hosting as sh
    monkeypatch.setattr(sh, "_common_dir", lambda p: "/some/other/.git")
    monkeypatch.setattr(sh, "_peers_common_dir", lambda: "~/peers/.git")
    monkeypatch.setattr(sh, "_has_peers_sentinel", lambda p: True)
    flag, reason = is_self_hosting(tmp_path, changed_paths=["README.md"],
                                   target_repo="/tmp/copy-of-peers")
    assert flag is True and reason == "target-is-peers"


def test_target_identity_error_fails_safe(tmp_path, monkeypatch):
    # if resolving the target identity RAISES, fail-safe to self-hosting (S1 / S5).
    import peers.spine.self_hosting as sh
    def _boom(p): raise OSError("cannot resolve")
    monkeypatch.setattr(sh, "_common_dir", _boom)
    assert is_self_hosting(tmp_path, changed_paths=["README.md"],
                           target_repo="/whatever")[0] is True


def test_undeterminable_peers_identity_fails_safe(tmp_path, monkeypatch):
    # BUG-601 (defense in depth / S1, sad path): if peers' OWN git common-dir is
    # unresolvable (git broken/missing at peers' root) we cannot certify a target
    # repo is NOT a peers worktree by common-dir, because the `tgt_common ==
    # peers_common` comparison silently no-ops on a None peers_common. With no
    # sentinel marker on the target either, the detector must FAIL SAFE rather
    # than fall through to the path-glob layer and trust an otherwise-clean
    # (docs-only) diff against a genuine peers worktree.
    import peers.spine.self_hosting as sh
    monkeypatch.setattr(sh, "_common_dir", lambda p: "~/peers/.git")
    monkeypatch.setattr(sh, "_peers_common_dir", lambda: None)   # our identity unknown
    monkeypatch.setattr(sh, "_has_peers_sentinel", lambda p: False)
    flag, reason = is_self_hosting(tmp_path, changed_paths=["README.md"],
                                   target_repo="/tmp/peers-run-xyz/r1")
    assert flag is True and reason == "undeterminable-peers-identity"


def test_undeterminable_peers_identity_but_target_sentinel_still_peers(tmp_path, monkeypatch):
    # BUG-601 (edge): even with peers' own common-dir unresolvable, a POSITIVE
    # sentinel match on the target is a stronger signal than the new fail-safe.
    # The detector must still report `target-is-peers` (the accurate cause), not
    # mask the positive identification behind `undeterminable-peers-identity`.
    import peers.spine.self_hosting as sh
    monkeypatch.setattr(sh, "_common_dir", lambda p: "/some/other/.git")
    monkeypatch.setattr(sh, "_peers_common_dir", lambda: None)   # our identity unknown
    monkeypatch.setattr(sh, "_has_peers_sentinel", lambda p: True)
    flag, reason = is_self_hosting(tmp_path, changed_paths=["README.md"],
                                   target_repo="/tmp/copy-of-peers")
    assert flag is True and reason == "target-is-peers"


def test_known_peers_identity_trusted_path_preserved(tmp_path, monkeypatch):
    # BUG-601 (happy/control): the fail-safe must NOT over-fire. When peers' own
    # identity IS resolvable and the target is a genuinely-different external
    # project (distinct common-dir, no sentinel) with a docs-only diff, the
    # trusted path is preserved -- the run is auto-merge-eligible, (False, "").
    import peers.spine.self_hosting as sh
    monkeypatch.setattr(sh, "_common_dir", lambda p: "/other/project/.git")
    monkeypatch.setattr(sh, "_peers_common_dir", lambda: "~/peers/.git")
    monkeypatch.setattr(sh, "_has_peers_sentinel", lambda p: False)
    flag, reason = is_self_hosting(tmp_path, changed_paths=["README.md"],
                                   target_repo="/tmp/some-external-project")
    assert flag is False and reason == ""


def test_governance_globs_cover_the_full_enforcement_surface():
    # the registry is an auditable tuple the reviewer can read; assert every
    # gate-bearing leaf is represented (a coverage test that fails if a new
    # governance prefix/file is dropped).
    joined = " ".join(GOVERNANCE_GLOBS)
    for needle in ("src/peers/spine/", "attest", ".peers/", "anti_cheat_guard",
                   "safe_io", "structured_halt", "goal", "driver_gate_pipeline",
                   "driver_tick_hooks", "driver_soft_reviews", "/checks/",
                   "async_gate_runner", "op_config", ".gitattributes",
                   "pyproject.toml", "goals.yaml", "checks.sha256"):
        assert needle in joined, f"governance surface missing: {needle}"
