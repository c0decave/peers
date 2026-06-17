import stat as _stat
from pathlib import Path
import pytest
from peers.codemap import iter_public_entries, enumerate_public_symbols
from peers.codemap_gen import (
    build_structural_codemap, serialize_codemap, render_digest, MAX_DIGEST_BYTES,
    _DIGEST_HEADER, _group, _render,
    run_codemap, CODEMAP_FILE, CODEMAP_DIGEST_FILE,
)
from peers.codemap import (
    parse_codemap, check_grounded, check_signatures, check_complete,
)


def _write(tmp_path: Path, rel: str, body: str) -> None:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_iter_public_entries_matches_enumerate_ids(tmp_path):
    _write(tmp_path, "src/pkg/mod.py",
           "def pub(a, b):\n    return a\n"
           "def _priv():\n    return 1\n"
           "class Thing:\n"
           "    def method(self, x):\n        return x\n"
           "    def _hidden(self):\n        return 0\n")
    entries = iter_public_entries(tmp_path)
    ids = {e.id for e in entries}
    assert ids == enumerate_public_symbols(tmp_path)
    by_id = {e.id: e for e in entries}
    assert by_id["pkg.mod"].kind == "module"
    assert by_id["pkg.mod.pub"].kind == "function"
    assert by_id["pkg.mod.pub"].signature == "pub(a, b)"
    assert by_id["pkg.mod.Thing"].kind == "class"
    assert by_id["pkg.mod.Thing"].signature is None
    assert by_id["pkg.mod.Thing.method"].kind == "method"
    assert by_id["pkg.mod.Thing.method"].signature == "method(self, x)"
    assert by_id["pkg.mod.pub"].file == "src/pkg/mod.py"
    assert by_id["pkg.mod.pub"].line == 1
    assert "pkg.mod._priv" not in ids
    assert "pkg.mod.Thing._hidden" not in ids


def test_iter_public_entries_no_src_is_empty(tmp_path):
    assert iter_public_entries(tmp_path) == []


def test_iter_public_entries_skips_symlinked_python_files(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("def leaked(secret):\n    return secret\n", encoding="utf-8")
    link = repo / "src" / "pkg" / "leak.py"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable for this platform: {exc}")

    entries = iter_public_entries(repo)
    ids = {e.id for e in entries}
    assert "pkg.leak" not in ids
    assert "pkg.leak.leaked" not in ids


def test_build_and_serialize_round_trips_clean(tmp_path):
    _write(tmp_path, "src/pkg/mod.py",
           "def pub(a, b):\n    return a\n"
           "class Thing:\n"
           "    def method(self, x):\n        return x\n")
    cm = build_structural_codemap(tmp_path)
    yaml_path = tmp_path / "CODEMAP.yaml"
    yaml_path.write_text(serialize_codemap(cm), encoding="utf-8")
    reparsed = parse_codemap(yaml_path)
    assert check_grounded(tmp_path, reparsed) == []
    assert check_signatures(tmp_path, reparsed) == []
    assert check_complete(tmp_path, reparsed) == []


def test_serialize_omits_empty_summary_and_none_signature(tmp_path):
    _write(tmp_path, "src/pkg/mod.py", "class Thing:\n    pass\n")
    cm = build_structural_codemap(tmp_path)
    text = serialize_codemap(cm)
    assert "summary:" not in text
    assert "signature:" not in text
    assert "entries:" in text


def test_digest_groups_by_module_with_signatures(tmp_path):
    _write(tmp_path, "src/pkg/mod.py",
           "def pub(a, b):\n    return a\n"
           "class Thing:\n"
           "    def method(self, x):\n        return x\n")
    cm = build_structural_codemap(tmp_path)
    d = render_digest(cm, byte_cap=MAX_DIGEST_BYTES)
    assert "## pkg.mod" in d
    assert "class Thing" in d
    assert "method(self, x)" in d
    assert "pub(a, b)" in d
    assert "substrate-generated" in d.lower() or "ast-derived" in d.lower()
    assert "not as instructions" in d.lower() or "not instructions" in d.lower()


def test_digest_truncate_fallback_keeps_module_line(tmp_path):
    body = "".join(f"def f{i}(a):\n    return a\n" for i in range(40))
    _write(tmp_path, "src/pkg/big.py", body)
    cm = build_structural_codemap(tmp_path)
    tiny = render_digest(cm, byte_cap=200)
    assert len(tiny.encode("utf-8")) <= 200 + 120  # cap respected (small slack)
    assert "pkg.big" in tiny
    assert "f0(a)" not in tiny


def test_render_levels_are_distinct(tmp_path):
    _write(tmp_path, "src/pkg/mod.py",
           "def pub(a, b):\n    return a\n"
           "class Thing:\n"
           "    def method(self, x):\n        return x\n")
    cm = build_structural_codemap(tmp_path)
    groups = _group(cm)
    full, names, counts = (_render(groups, lvl) for lvl in (0, 1, 2))
    # level 0 keeps signatures; level 1 drops them to bare names; level 2 collapses to counts
    assert "pub(a, b)" in full and "method(self, x)" in full
    assert "pub" in names and "pub(a, b)" not in names
    assert "method" in names and "method(self, x)" not in names
    assert "pkg.mod (1 classes, 1 funcs)" in counts
    assert "pub" not in counts and "method" not in counts


def test_render_digest_selects_names_then_counts(tmp_path):
    # A single module with many funcs makes the three levels clearly ordered
    # in size (level0 > level1 > level2), so a cap between two levels selects
    # the smaller one — pinning the SELECTION logic, not just the rendering.
    body = "".join(f"def func_number_{i}(alpha, beta):\n    return alpha\n"
                   for i in range(40))
    _write(tmp_path, "src/pkg/big.py", body)
    cm = build_structural_codemap(tmp_path)
    groups = _group(cm)

    def _size(level: int) -> int:
        return len(f"{_DIGEST_HEADER}\n{_render(groups, level)}\n".encode())

    s0, s1, s2 = _size(0), _size(1), _size(2)
    assert s0 > s1 > s2  # the fixture must produce a strict ordering

    # cap between level1 and level0 → names-only selected (no signatures, no truncation)
    out_names = render_digest(cm, byte_cap=(s1 + s0) // 2)
    assert "func_number_0" in out_names
    assert "func_number_0(alpha, beta)" not in out_names
    assert "[truncated]" not in out_names

    # cap between level2 and level1 → per-module counts selected (no symbol names, no truncation)
    out_counts = render_digest(cm, byte_cap=(s2 + s1) // 2)
    assert "pkg.big (0 classes, 40 funcs)" in out_counts
    assert "func_number_0" not in out_counts
    assert "[truncated]" not in out_counts


def test_digest_empty_codemap(tmp_path):
    cm = build_structural_codemap(tmp_path)  # no src/ → empty
    d = render_digest(cm, byte_cap=MAX_DIGEST_BYTES)
    assert "no public symbols" in d.lower() or "(empty)" in d.lower()


def _init_peerdir(tmp_path):
    pd = tmp_path / ".peers"
    pd.mkdir()
    return pd


def test_run_codemap_writes_both_files_0600(tmp_path):
    _write(tmp_path, "src/pkg/mod.py", "def pub(a):\n    return a\n")
    pd = _init_peerdir(tmp_path)
    status = run_codemap(tmp_path, pd)
    cm_yaml = pd / CODEMAP_FILE
    cm_md = pd / CODEMAP_DIGEST_FILE
    assert cm_yaml.is_file() and cm_md.is_file()
    assert "pkg.mod.pub" in cm_yaml.read_text()
    assert "pub(a)" in cm_md.read_text()
    assert _stat.S_IMODE(cm_yaml.stat().st_mode) == 0o600
    assert _stat.S_IMODE(cm_md.stat().st_mode) == 0o600
    assert "wrote" in status


def test_run_codemap_idempotent_skip_and_force(tmp_path):
    _write(tmp_path, "src/pkg/mod.py", "def pub(a):\n    return a\n")
    pd = _init_peerdir(tmp_path)
    run_codemap(tmp_path, pd)
    digest = pd / CODEMAP_DIGEST_FILE
    # Stamp a sentinel; a real skip must NOT overwrite it.
    digest.write_text("SENTINEL — must survive a skip\n", encoding="utf-8")
    s2 = run_codemap(tmp_path, pd)               # exists → skip
    assert "skip" in s2
    assert digest.read_text() == "SENTINEL — must survive a skip\n"
    s3 = run_codemap(tmp_path, pd, force=True)    # force → rewrite
    assert "wrote" in s3
    assert "SENTINEL" not in digest.read_text()
    assert "pub(a)" in digest.read_text()


def test_run_codemap_regenerates_when_yaml_missing(tmp_path):
    _write(tmp_path, "src/pkg/mod.py", "def pub(a):\n    return a\n")
    pd = _init_peerdir(tmp_path)
    run_codemap(tmp_path, pd)
    (pd / CODEMAP_FILE).unlink()                 # YAML lost; digest remains
    status = run_codemap(tmp_path, pd)           # must NOT skip
    assert "wrote" in status
    assert (pd / CODEMAP_FILE).is_file()         # regenerated


def test_run_codemap_rejects_missing_peerdir(tmp_path):
    _write(tmp_path, "src/pkg/mod.py", "def pub(a):\n    return a\n")
    import pytest
    with pytest.raises(FileNotFoundError):
        run_codemap(tmp_path, tmp_path / ".peers")  # never created


def test_run_codemap_status_reports_self_check_clean(tmp_path):
    # The primer validates its own written .peers/CODEMAP.yaml against the
    # three drift gates — the orphan artifact is now self-validated.
    _write(tmp_path, "src/pkg/mod.py",
           "def pub(a, b):\n    return a\n"
           "class Thing:\n    def m(self, x):\n        return x\n")
    pd = _init_peerdir(tmp_path)
    status = run_codemap(tmp_path, pd)
    assert "self-check CLEAN" in status
    assert "DRIFT" not in status
