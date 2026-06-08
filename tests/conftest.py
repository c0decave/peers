import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _restore_basetemp_writable(basetemp: Path) -> None:
    """Walk `basetemp` and chmod every directory back to user-writable.

    Why this exists: `peers init` chmod's the scaffolded
    `.peers/checks/` tree to 0o555 to harden against a peer rewriting
    a gate script (BUG-258 family). Integration tests run `peers init`
    against a pytest tmp_path, leaving 0o555 dirs behind. pytest's
    cross-session cleanup of old numbered tmpdirs can chmod a file on
    PermissionError but does not chmod the *parent* dir, so the trees
    pile up in /tmp/pytest-of-USER/. With a 512MB tmpfs the disk fills
    after ~3-4 sessions and JUnit XML write fails — surfacing as a
    `no-prior-regression` infra error.

    The hook is intentionally best-effort: any OSError is swallowed
    because failing the pytest session over a cleanup helper is worse
    than the leak it tries to prevent. We refuse to follow symlinks
    out of the basetemp.
    """
    if not basetemp.is_dir() or basetemp.is_symlink():
        return
    try:
        for dirpath, dirnames, _ in os.walk(
            basetemp, topdown=True, followlinks=False
        ):
            dirnames[:] = [
                d for d in dirnames if not Path(dirpath, d).is_symlink()
            ]
            try:
                current = stat.S_IMODE(os.stat(dirpath).st_mode)
                os.chmod(dirpath, current | stat.S_IWUSR | stat.S_IXUSR)
            except OSError:
                continue
    except OSError:
        return


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Restore writability on the session basetemp so the NEXT session's
    cleanup of old numbered tmpdirs can rm_rf them."""
    try:
        basetemp = session.config._tmp_path_factory.getbasetemp()
    except (AttributeError, Exception):
        return
    _restore_basetemp_writable(Path(str(basetemp)))


@pytest.fixture(scope="session")
def _isolated_git_global_config(tmp_path_factory):
    """A throwaway global gitconfig carrying only an identity.

    Tests spin up real git repos and assert on hooks / staged content.
    If the operator running the suite has a global `core.hooksPath`
    (e.g. a personal commit-gate) or a global `core.excludesFile` (e.g.
    one that ignores `.env`/secrets), those leak into every test repo:
    hooks install outside the tmp repo, and `git add leak.env` is
    silently refused. Both produce spurious failures — and the hook
    install can even pollute the operator's real global hook.
    """
    cfg = tmp_path_factory.mktemp("gitconfig") / "config"
    cfg.write_text(
        "[user]\n\tname = peers-tests\n\temail = tests@peers.local\n",
        encoding="utf-8",
    )
    return cfg


@pytest.fixture(autouse=True)
def _isolate_git_env(monkeypatch, _isolated_git_global_config):
    """Point every git subprocess at the throwaway global config and
    disable system config, so the operator's global git environment
    (hooksPath, excludesFile, …) cannot influence the suite."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(_isolated_git_global_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
