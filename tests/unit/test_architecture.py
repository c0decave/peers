"""ARCHITECTURE.md gate: [[id]] anchor resolution + subsystem coverage + the
narrative-outline seed. The HARD, deterministic moat for the human-docs prose
(accuracy is the soft architecture-cross-review's job)."""
from __future__ import annotations

from pathlib import Path

from peers.codemap import (
    ARCH_PLACEHOLDER,
    ARCHITECTURE_FILE,
    CodeMap,
    Entry,
    check_architecture,
    parse_anchors,
)


def test_parse_anchors_finds_double_bracket_ids():
    text = "the loop [[peers.tick_loop.TickLoop.run]] drives [[peers.codemap]]."
    assert parse_anchors(text) == [
        "peers.tick_loop.TickLoop.run", "peers.codemap"]


def test_parse_anchors_ignores_fenced_code_blocks():
    text = (
        "real [[peers.codemap]]\n"
        "```\n[[peers.NOT_AN_ANCHOR]]\n```\n"
        "also [[peers.tick_loop]]\n"
    )
    assert parse_anchors(text) == ["peers.codemap", "peers.tick_loop"]


def _cm() -> CodeMap:
    # two subsystems under `pkg.`: `alpha` and `beta`
    return CodeMap((
        Entry("pkg.alpha", "module", "src/pkg/alpha.py", 1),
        Entry("pkg.alpha.foo", "function", "src/pkg/alpha.py", 1, "foo()"),
        Entry("pkg.beta", "module", "src/pkg/beta.py", 1),
        Entry("pkg._priv", "module", "src/pkg/_priv.py", 1),  # underscore → not required
    ))


def _doc(tmp_path: Path, body: str) -> Path:
    (tmp_path / ARCHITECTURE_FILE).write_text(body, encoding="utf-8")
    return tmp_path


def test_check_architecture_clean_when_all_covered(tmp_path):
    _doc(tmp_path, "alpha [[pkg.alpha.foo]] and beta [[pkg.beta]].\n")
    assert check_architecture(tmp_path, _cm()) == []


def test_check_architecture_flags_dangling_anchor(tmp_path):
    _doc(tmp_path, "[[pkg.alpha.foo]] [[pkg.beta]] [[pkg.ghost]]\n")
    v = check_architecture(tmp_path, _cm())
    assert any("dangling" in m and "pkg.ghost" in m for m in v)


def test_check_architecture_flags_uncovered_subsystem(tmp_path):
    _doc(tmp_path, "only alpha [[pkg.alpha.foo]] here.\n")  # beta missing
    v = check_architecture(tmp_path, _cm())
    assert any("not covered" in m and "beta" in m for m in v)


def test_check_architecture_ignores_underscore_subsystem(tmp_path):
    # covering alpha + beta is enough; `_priv` is never required
    _doc(tmp_path, "[[pkg.alpha.foo]] [[pkg.beta]]\n")
    assert check_architecture(tmp_path, _cm()) == []


def test_check_architecture_flags_leftover_placeholder(tmp_path):
    _doc(tmp_path, f"[[pkg.alpha.foo]] [[pkg.beta]]\n{ARCH_PLACEHOLDER}\n")
    v = check_architecture(tmp_path, _cm())
    assert any("placeholder" in m for m in v)


def test_check_architecture_flags_missing_file(tmp_path):
    v = check_architecture(tmp_path, _cm())
    assert len(v) == 1 and "missing" in v[0]


def test_check_architecture_qualifies_subsystem_by_package(tmp_path):
    # `a.cli` and `b.cli` are DIFFERENT subsystems — covering one must NOT mark
    # the other covered (no bare-name collapse across packages, e.g. the real
    # peers.cli vs peers_ctl.cli collision).
    cm = CodeMap((
        Entry("a.cli", "module", "src/a/cli.py", 1),
        Entry("a.cli.foo", "function", "src/a/cli.py", 1, "foo()"),
        Entry("b.cli", "module", "src/b/cli.py", 1),
    ))
    _doc(tmp_path, "[[a.cli.foo]] only\n")  # covers a.cli, not b.cli
    v = check_architecture(tmp_path, cm)
    assert any("not covered" in m and "b.cli" in m for m in v)


def test_architecture_grounded_script_red_then_green(tmp_path):
    """The thin check script: red on the seed, green once covered."""
    import importlib.util

    from peers.codemap_gen import seed_repo_architecture, seed_repo_codemap

    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "alpha.py").write_text(
        "def foo(a):\n    return a\n", encoding="utf-8")
    seed_repo_codemap(tmp_path)
    seed_repo_architecture(tmp_path)

    spec = importlib.util.spec_from_file_location(
        "_arch_check",
        "src/peers/templates/modes/document/checks/architecture_grounded.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.main(str(tmp_path)) == 1                       # seed → red
    (tmp_path / ARCHITECTURE_FILE).write_text(
        "alpha [[pkg.alpha.foo]] does the thing.\n", encoding="utf-8")
    assert mod.main(str(tmp_path)) == 0                       # covered → green
