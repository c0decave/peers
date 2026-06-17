import subprocess

from peers.spine.gates import resolves_to_commit


def _git(p, *a):
    return subprocess.run(["git", "-C", str(p), *a],
                          capture_output=True, text=True, check=True).stdout


def _init_repo_with_commit(p):
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    _git(p, "config", "commit.gpgsign", "false")
    (p / "a").write_text("a")
    _git(p, "add", "a")
    _git(p, "commit", "-q", "-m", "a")
    return _git(p, "rev-parse", "HEAD").strip()


def test_full_sha_resolves(tmp_path):
    sha = _init_repo_with_commit(tmp_path)
    assert resolves_to_commit(tmp_path, sha) is True


def test_bogus_sha_is_false(tmp_path):
    _git(tmp_path, "init", "-q")
    assert resolves_to_commit(tmp_path, "deadbeef") is False


def test_symbolic_head_is_false(tmp_path):
    # HEAD resolves to a commit but HEAD != the full 40-hex id -> fail-closed.
    _init_repo_with_commit(tmp_path)
    assert resolves_to_commit(tmp_path, "HEAD") is False


def test_non_string_is_false(tmp_path):
    assert resolves_to_commit(tmp_path, None) is False
