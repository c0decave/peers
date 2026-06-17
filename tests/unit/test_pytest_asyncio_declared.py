"""TUI-03 regression: the Textual pilot tests are ``@pytest.mark.asyncio``
coroutines; without ``pytest-asyncio`` declared in an installable extra, a
clean ``pip install -e .[dev,tui]`` collects them as un-awaited coroutines and
they hard-fail (62 failures observed in the 2026-06-14 audit). This guards the
declaration so the documented test install can actually run the pilots.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
TESTS_DIR = Path(__file__).resolve().parent


def _extra(name: str) -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    extras = data.get("project", {}).get("optional-dependencies", {})
    return list(extras.get(name, []))


def _declares(package: str, extra: str = "dev") -> bool:
    # match the package name at the start of a PEP 508 requirement string
    return any(
        req.split("[")[0].split("<")[0].split(">")[0].split("=")[0].split("~")[0]
        .strip()
        .lower()
        == package.lower()
        for req in _extra(extra)
    )


def _async_marker_test_files() -> list[Path]:
    return [
        p
        for p in TESTS_DIR.glob("test_*.py")
        if "@pytest.mark.asyncio" in p.read_text(encoding="utf-8")
    ]


def test_happy_pytest_asyncio_declared_in_dev_extra() -> None:
    # the canonical test extra must carry the async plugin so CI/devs running
    # `.[dev]` (and `.[dev,tui]`) get a runner for the asyncio marker.
    assert _declares("pytest-asyncio", "dev"), (
        "pytest-asyncio must be in the [dev] extra so @pytest.mark.asyncio "
        "TUI pilots actually run on the documented install"
    )


def test_sad_absent_package_is_not_falsely_declared() -> None:
    # proves the check has teeth (not vacuously true).
    assert not _declares("pytest-asyncio-does-not-exist-xyz", "dev")


def test_edge_async_marker_usage_is_backed_by_a_declared_plugin() -> None:
    # the real invariant that broke: we DO ship asyncio-marked tests, and that
    # usage must be backed by the declared plugin. If either drifts, fail.
    marked = _async_marker_test_files()
    assert marked, "expected the suite to ship @pytest.mark.asyncio tests"
    assert _declares("pytest-asyncio", "dev"), (
        f"{len(marked)} test files use @pytest.mark.asyncio but pytest-asyncio "
        "is not declared in an installable extra"
    )
