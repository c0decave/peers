import subprocess
from pathlib import Path

from peers.comm_layer import GitCommLayer, parse_trailers


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        capture_output=True, text=True,
    )
    return r.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "README").write_text("x\n")
    _git(path, "add", "README")
    _git(path, "commit", "-q", "-m", "init")


def test_parse_trailers_single():
    msg = "subject\n\nbody\n\nSelf-Review: pass\nPeer-Status: handoff\n"
    t = parse_trailers(msg)
    assert t == {"Self-Review": "pass", "Peer-Status": "handoff"}


def test_parse_trailers_no_trailers():
    msg = "subject\n\nbody only\n"
    assert parse_trailers(msg) == {}


def test_finds_no_new_commits_initially(tmp_path: Path):
    repo = tmp_path / "r"
    _init_repo(repo)
    layer = GitCommLayer(repo)
    head = layer.head_sha()
    assert layer.new_commits_by(peer="claude", since=head) == []


def test_finds_commits_with_peer_trailer(tmp_path: Path):
    repo = tmp_path / "r"
    _init_repo(repo)
    base = _git(repo, "rev-parse", "HEAD").strip()
    msg = ("Add foo\n\nbody\n\nSelf-Review: pass\n"
           "Peer-Status: handoff\nPeer: claude\n")
    (repo / "foo").write_text("x")
    _git(repo, "add", "foo")
    _git(repo, "commit", "-q", "-m", msg)
    layer = GitCommLayer(repo)
    commits = layer.new_commits_by(peer="claude", since=base)
    assert len(commits) == 1
    assert commits[0].trailers["Peer-Status"] == "handoff"


def test_since_filters_history(tmp_path: Path):
    repo = tmp_path / "r"
    _init_repo(repo)
    base = _git(repo, "rev-parse", "HEAD").strip()
    msg = "Add\n\nPeer: claude\n"
    (repo / "f").write_text("x")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", msg)
    layer = GitCommLayer(repo)
    assert layer.new_commits_by(peer="claude", since=base)
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert layer.new_commits_by(peer="claude", since=head) == []


def test_head_sha(tmp_path: Path):
    repo = tmp_path / "r"
    _init_repo(repo)
    layer = GitCommLayer(repo)
    assert len(layer.head_sha()) == 40


def test_new_commits_by_none_since_returns_empty(tmp_path: Path):
    """Bootstrap behaviour: callers must pass an explicit cursor.
    Returning [] here forces _read_inbox to seed last_inbox_sha at HEAD
    instead of replaying all history."""
    repo = tmp_path / "r"
    _init_repo(repo)
    # Add a commit by claude even before any cursor was set
    (repo / "f").write_text("x")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "Add\n\nPeer: claude\n")
    layer = GitCommLayer(repo)
    assert layer.new_commits_by(peer="claude", since=None) == []


def test_parse_trailers_handles_crlf():
    msg = "subject\r\n\r\nbody\r\n\r\nSelf-Review: pass\r\nPeer: claude\r\n"
    t = parse_trailers(msg)
    assert t == {"Self-Review": "pass", "Peer": "claude"}


def test_parse_trailers_strips_value_whitespace():
    msg = "s\n\nb\n\nPeer:   claude   \n"
    t = parse_trailers(msg)
    assert t == {"Peer": "claude"}


def test_parse_trailers_rejects_url_scheme_keys():
    """C2: a body ending with a raw URL line (no real trailer block
    above) must NOT be parsed as a trailer.  `https://example.com`
    naively matches `Key: value` with Key=`https`, Value=`//example.com`,
    masking the absence of a real `Peer:` trailer."""
    msg = (
        "subject\n\n"
        "Some body text.\n"
        "https://example.com/x\n"
    )
    t = parse_trailers(msg)
    assert "https" not in t
    assert t == {}


def test_parse_trailers_accepts_url_as_value_in_real_trailer():
    """`See: https://...` IS a valid trailer (key `See` is fine).
    Defensive logic only rejects URL-scheme KEYS, not URL VALUES."""
    msg = "s\n\nb\n\nSee: https://example.com\nPeer: claude\n"
    t = parse_trailers(msg)
    assert t == {"See": "https://example.com", "Peer": "claude"}


def test_new_commits_by_handles_record_separator_in_body(tmp_path: Path):
    """C3: ASCII RS (\\x1e) or US (\\x1f) inside a commit body must
    not split the parser's record structure."""
    repo = tmp_path / "r"
    _init_repo(repo)
    base = _git(repo, "rev-parse", "HEAD").strip()
    # Body containing both separators. A peer could legitimately quote
    # binary output, or use these chars in a message.
    body = (
        "Add with weird body\n\n"
        "binary-ish: \x1emiddle\x1fend\n\n"
        "Peer: claude\n"
    )
    (repo / "f").write_text("x")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", body)
    layer = GitCommLayer(repo)
    commits = layer.new_commits_by(peer="claude", since=base)
    assert len(commits) == 1, commits
    assert commits[0].trailers.get("Peer") == "claude"


def test_parse_trailers_value_must_not_start_with_double_slash():
    msg = "s\n\nb\n\nPeer: //claude\n"
    assert parse_trailers(msg) == {}


def test_ignores_commits_by_other_peer(tmp_path: Path):
    repo = tmp_path / "r"
    _init_repo(repo)
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "f").write_text("x")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "Add\n\nPeer: codex\n")
    layer = GitCommLayer(repo)
    assert layer.new_commits_by(peer="claude", since=base) == []
