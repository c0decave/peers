"""Tests for the tests_no_unjustified_skip_or_fail audit check.

The check scans all test_*.py files in a project for `@pytest.mark.skip` /
`@pytest.mark.xfail` decorators (and module-level `pytestmark`) and fails
when any of them carries no `reason=` argument or a generic/weak reason
("TODO", "FIXME", empty string, etc.). The goal is to force peers to
explain WHY a test is skipped or expected to fail, so the audit cannot
quietly hide regressions behind a marker.
"""
from __future__ import annotations

from pathlib import Path

from peers.templates.modes.audit.checks import (
    tests_no_unjustified_skip_or_fail as check,
)


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests" / "unit").mkdir(parents=True)
    return root


def test_clean_repo_passes(tmp_path: Path, capsys) -> None:
    root = _make_repo(tmp_path)
    (root / "tests" / "unit" / "test_a.py").write_text(
        "def test_one():\n"
        "    assert 1 == 1\n"
        "\n"
        "def test_two():\n"
        "    assert 'a'.upper() == 'A'\n"
    )

    rc = check.main(str(root))

    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_skip_with_solid_reason_passes(tmp_path: Path, capsys) -> None:
    root = _make_repo(tmp_path)
    (root / "tests" / "unit" / "test_a.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.skip(reason=\"requires GPU hardware unavailable in CI sandbox; "
        "manually verified on RTX-4090 with CUDA 12.4\")\n"
        "def test_gpu_path():\n"
        "    pass\n"
    )

    rc = check.main(str(root))

    assert rc == 0


def test_skip_with_todo_reason_fails(tmp_path: Path, capsys) -> None:
    root = _make_repo(tmp_path)
    (root / "tests" / "unit" / "test_a.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.skip(reason=\"TODO\")\n"
        "def test_thing():\n"
        "    pass\n"
    )

    rc = check.main(str(root))

    assert rc == 1
    out = capsys.readouterr().out
    assert "test_thing" in out
    assert "generic" in out.lower() or "weak" in out.lower()


def test_skip_with_fixme_reason_fails(tmp_path: Path, capsys) -> None:
    root = _make_repo(tmp_path)
    (root / "tests" / "unit" / "test_a.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.skip(reason=\"FIXME\")\n"
        "def test_thing():\n"
        "    pass\n"
    )

    rc = check.main(str(root))

    assert rc == 1


def test_skip_without_reason_fails(tmp_path: Path, capsys) -> None:
    root = _make_repo(tmp_path)
    (root / "tests" / "unit" / "test_a.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.skip\n"
        "def test_thing():\n"
        "    pass\n"
    )

    rc = check.main(str(root))

    assert rc == 1
    out = capsys.readouterr().out
    assert "test_thing" in out
    assert "no reason" in out.lower() or "missing reason" in out.lower()


def test_skip_with_empty_reason_fails(tmp_path: Path, capsys) -> None:
    root = _make_repo(tmp_path)
    (root / "tests" / "unit" / "test_a.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.skip(reason=\"\")\n"
        "def test_thing():\n"
        "    pass\n"
    )

    rc = check.main(str(root))

    assert rc == 1


def test_skip_with_too_short_reason_fails(tmp_path: Path, capsys) -> None:
    """Reason under MIN_REASON_LEN chars is rejected."""
    root = _make_repo(tmp_path)
    (root / "tests" / "unit" / "test_a.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.skip(reason=\"flaky\")\n"
        "def test_thing():\n"
        "    pass\n"
    )

    rc = check.main(str(root))

    assert rc == 1
    out = capsys.readouterr().out
    assert "too short" in out.lower() or "min" in out.lower()


def test_xfail_with_solid_reason_passes(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    (root / "tests" / "unit" / "test_a.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.xfail(reason=\"upstream library bug filed as github issue #4242, "
        "expected to be fixed in v2.1; production guards against the bad path\")\n"
        "def test_upstream_bug():\n"
        "    pass\n"
    )

    rc = check.main(str(root))

    assert rc == 0


def test_xfail_with_weak_reason_fails(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    (root / "tests" / "unit" / "test_a.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.xfail(reason=\"broken\")\n"
        "def test_thing():\n"
        "    pass\n"
    )

    rc = check.main(str(root))

    assert rc == 1


def test_module_level_pytestmark_skip_detected(tmp_path: Path, capsys) -> None:
    """`pytestmark = pytest.mark.skip(...)` at module level applies to every
    test in the file and must also carry a justification."""
    root = _make_repo(tmp_path)
    (root / "tests" / "unit" / "test_a.py").write_text(
        "import pytest\n"
        "\n"
        "pytestmark = pytest.mark.skip(reason=\"TODO\")\n"
        "\n"
        "def test_thing():\n"
        "    pass\n"
    )

    rc = check.main(str(root))

    assert rc == 1
    out = capsys.readouterr().out
    assert "module-level" in out.lower() or "pytestmark" in out.lower()


def test_module_level_pytestmark_skip_with_solid_reason_passes(
    tmp_path: Path,
) -> None:
    root = _make_repo(tmp_path)
    (root / "tests" / "unit" / "test_a.py").write_text(
        "import pytest\n"
        "\n"
        "pytestmark = pytest.mark.skip(reason=\"this module covers Windows-only "
        "code paths and is intentionally not exercised in the Linux CI matrix\")\n"
        "\n"
        "def test_thing():\n"
        "    pass\n"
    )

    rc = check.main(str(root))

    assert rc == 0


def test_skipif_with_condition_passes_without_reason_validation(
    tmp_path: Path,
) -> None:
    """`@pytest.mark.skipif(cond, reason=...)` is conditional — the
    condition itself documents WHY. We still want a reason but accept
    shorter/structural ones because the condition is the primary doc."""
    root = _make_repo(tmp_path)
    (root / "tests" / "unit" / "test_a.py").write_text(
        "import pytest\n"
        "import sys\n"
        "\n"
        "@pytest.mark.skipif(sys.platform == 'win32', reason=\"POSIX only\")\n"
        "def test_thing():\n"
        "    pass\n"
    )

    rc = check.main(str(root))

    assert rc == 0


def test_multiple_unjustified_skips_all_reported(
    tmp_path: Path, capsys,
) -> None:
    root = _make_repo(tmp_path)
    (root / "tests" / "unit" / "test_a.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.skip(reason=\"TODO\")\n"
        "def test_one(): pass\n"
        "\n"
        "@pytest.mark.skip\n"
        "def test_two(): pass\n"
        "\n"
        "@pytest.mark.xfail(reason=\"broken\")\n"
        "def test_three(): pass\n"
    )

    rc = check.main(str(root))

    assert rc == 1
    out = capsys.readouterr().out
    assert "test_one" in out
    assert "test_two" in out
    assert "test_three" in out


def test_nonexistent_tests_dir_passes(tmp_path: Path) -> None:
    """No tests/ dir = nothing to validate = pass."""
    root = tmp_path / "empty"
    root.mkdir()

    rc = check.main(str(root))

    assert rc == 0


def test_skipped_non_test_function_ignored(tmp_path: Path) -> None:
    """Helpers (non test_*) with skip decorator are ignored; the check
    only enforces on test_* functions."""
    root = _make_repo(tmp_path)
    (root / "tests" / "unit" / "test_a.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.skip(reason=\"TODO\")\n"
        "def helper_thing(): pass\n"
        "\n"
        "def test_uses_helper(): pass\n"
    )

    rc = check.main(str(root))

    assert rc == 0


def test_syntax_error_test_file_does_not_crash(
    tmp_path: Path, capsys,
) -> None:
    """A test file with a syntax error is reported but doesn't crash
    the check (fail-loud, not fail-crash)."""
    root = _make_repo(tmp_path)
    (root / "tests" / "unit" / "test_a.py").write_text(
        "def test_thing(:\n"
        "    pass\n"
    )

    rc = check.main(str(root))

    # syntax error itself is not a skip-justification problem → pass
    # (but a warning could be printed)
    assert rc in (0, 1)


def test_check_is_not_public_api() -> None:
    """The check module should not introduce new public-API symbols
    that api_stable would flag on main."""
    from peers.templates.modes.audit.checks import api_stable

    symbols = set(api_stable.public_symbols("src"))
    # the check exposes only `main`; verify it's the only public symbol
    # we introduce and isn't in some unexpected location
    skip_check_symbols = {
        s for s in symbols
        if "tests_no_unjustified_skip_or_fail" in s
    }
    assert skip_check_symbols == {
        "peers.templates.modes.audit.checks."
        "tests_no_unjustified_skip_or_fail.main",
    } or skip_check_symbols == set()
