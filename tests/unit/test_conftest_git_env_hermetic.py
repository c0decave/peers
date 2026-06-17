"""Regression: the suite must be hermetic with respect to git's hook-exported
location vars (BUG-741 harvest follow-up).

The pre-push gate runs ``pytest`` from inside a git hook, where git exports
``GIT_DIR`` / ``GIT_INDEX_FILE`` / ``GIT_WORK_TREE`` / … . If those reach a
test's ``git`` subprocess they OVERRIDE its ``-C <tmp_repo>`` and it silently
operates on the REAL repo, so git-heavy tests pass standalone but fail under
``git push``. ``tests/conftest.py`` scrubs them at startup; this pins that so a
future edit cannot re-introduce the leak.

happy: with the scrub in place, none of the redirect vars are visible to tests.
edge:  the scrub is a no-op when the var is already absent (the ambient case).
sad:   a leaked redirect var (simulated) would make a tmp-repo git call escape
       to the real repo — proving the leak is the failure mechanism.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

# These are the vars git exports to hooks that REDIRECT a child ``git`` away
# from its cwd/-C target. They must not be visible to any test.
_LEAKABLE_GIT_LOC_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
)


# --- happy: the conftest scrub leaves none of them in the environment ---------

@pytest.mark.parametrize("var", _LEAKABLE_GIT_LOC_VARS)
def test_git_redirect_var_is_scrubbed_from_env(var: str) -> None:
    assert var not in os.environ, (
        f"{var} leaked into the test environment — a git subprocess would be "
        "redirected to the real repo (suite not hermetic inside a git hook)"
    )


# --- edge: scrubbing an already-absent var is a no-op (ambient, non-hook case)-

def test_scrub_is_noop_when_var_absent() -> None:
    # GIT_DIR is absent here (conftest scrubbed / never set); popping again is
    # safe and idempotent — mirrors how the conftest startup scrub behaves
    # outside a hook.
    assert os.environ.pop("GIT_DIR", None) is None


# --- sad: a leaked GIT_DIR really does hijack a tmp-repo git call -------------

def test_leaked_git_dir_would_hijack_tmp_repo_git(tmp_path: Path) -> None:
    real = tmp_path / "real"
    fixture = tmp_path / "fixture"
    for d in (real, fixture):
        d.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        subprocess.run(["git", "-C", str(d), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)
    # Put a distinguishing commit only in `real`.
    (real / "marker.txt").write_text("real-repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(real), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(real), "commit", "-qm", "real-only"], check=True)

    env = dict(os.environ)
    env["GIT_DIR"] = str(real / ".git")  # simulate the hook leak

    # A `-C fixture` call SHOULD see `fixture` (no commits); with GIT_DIR leaked
    # it is hijacked to `real` and reports the real-only commit instead.
    out = subprocess.run(
        ["git", "-C", str(fixture), "log", "--oneline"],
        env=env, capture_output=True, text=True,
    )
    assert "real-only" in out.stdout, (
        "expected the leaked GIT_DIR to hijack the -C fixture call to the real "
        "repo — this is the exact failure mechanism the conftest scrub prevents"
    )
