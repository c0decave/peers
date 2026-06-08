from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from peers.safe_io import (
    _ensure_private_dir,
    _write_text_in_private_nested_dir_no_symlink,
    append_text_in_dir_no_symlink,
    append_text_no_symlink,
    open_text_in_dir_no_symlink,
    read_text_no_symlink,
    read_text_under_root_no_follow,
    write_text_no_symlink,
)


def test_write_refuses_hard_link_before_truncating(tmp_path: Path):
    bait = tmp_path / "bait.txt"
    bait.write_text("keep me")
    link = tmp_path / "linked.txt"
    try:
        os.link(bait, link)
    except OSError as e:
        pytest.skip(f"hard links unavailable: {e}")

    with pytest.raises(OSError, match="hard-linked"):
        write_text_no_symlink(link, "clobber")

    assert bait.read_text() == "keep me"


def test_append_refuses_fifo_without_blocking(tmp_path: Path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo unavailable")
    fifo = tmp_path / "runs.jsonl"
    os.mkfifo(fifo)

    with pytest.raises(OSError, match="non-regular"):
        append_text_no_symlink(fifo, "{}\n")


def test_append_in_dir_refuses_symlinked_parent(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = tmp_path / "log"
    parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        append_text_in_dir_no_symlink(parent, "runs.jsonl", "{}\n")

    assert not (outside / "runs.jsonl").exists()


def test_append_in_dir_appends_regular_leaf(tmp_path: Path):
    parent = tmp_path / "log"
    parent.mkdir()

    append_text_in_dir_no_symlink(parent, "runs.jsonl", "one\n")
    append_text_in_dir_no_symlink(parent, "runs.jsonl", "two\n")

    assert (parent / "runs.jsonl").read_text() == "one\ntwo\n"


def test_append_in_dir_rejects_path_component_filename(tmp_path: Path):
    parent = tmp_path / "log"
    parent.mkdir()

    with pytest.raises(ValueError, match="single path component"):
        append_text_in_dir_no_symlink(parent, "../runs.jsonl", "{}\n")


def test_open_text_in_dir_writes_regular_leaf(tmp_path: Path):
    parent = tmp_path / "log"
    parent.mkdir()

    with open_text_in_dir_no_symlink(parent, "runs.jsonl", "a") as f:
        f.write("one\n")

    assert (parent / "runs.jsonl").read_text() == "one\n"


def test_open_text_in_dir_refuses_symlinked_parent(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = tmp_path / "log"
    parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        with open_text_in_dir_no_symlink(parent, "runs.jsonl", "a"):
            pass

    assert not (outside / "runs.jsonl").exists()


def test_open_text_in_dir_refuses_hard_link_before_truncating(tmp_path: Path):
    parent = tmp_path / "log"
    parent.mkdir()
    bait = tmp_path / "bait.txt"
    bait.write_text("keep me")
    link = parent / "linked.txt"
    try:
        os.link(bait, link)
    except OSError as e:
        pytest.skip(f"hard links unavailable: {e}")

    with pytest.raises(OSError, match="hard-linked"):
        with open_text_in_dir_no_symlink(parent, "linked.txt", "w"):
            pass

    assert bait.read_text() == "keep me"


def test_read_refuses_symlink(tmp_path: Path):
    bait = tmp_path / "secret.txt"
    bait.write_text("secret")
    link = tmp_path / "message.md"
    link.symlink_to(bait)

    with pytest.raises(OSError):
        read_text_no_symlink(link)


def test_read_refuses_hard_link(tmp_path: Path):
    bait = tmp_path / "secret.txt"
    bait.write_text("secret")
    link = tmp_path / "message.md"
    try:
        os.link(bait, link)
    except OSError as e:
        pytest.skip(f"hard links unavailable: {e}")

    with pytest.raises(OSError, match="hard-linked"):
        read_text_no_symlink(link)


def test_read_text_under_root_no_follow_reads_valid_utf8(tmp_path: Path):
    peers_dir = tmp_path / ".peers"
    peers_dir.mkdir()
    (peers_dir / "state.json").write_text(
        '{"note":"healthy cafe"}', encoding="utf-8",
    )

    text = read_text_under_root_no_follow(
        tmp_path, (".peers", "state.json"),
    )

    assert text == '{"note":"healthy cafe"}'


def test_read_text_under_root_no_follow_honors_max_bytes_boundary(
    tmp_path: Path,
):
    peers_dir = tmp_path / ".peers"
    peers_dir.mkdir()
    (peers_dir / "state.json").write_text("abcdef", encoding="utf-8")

    text = read_text_under_root_no_follow(
        tmp_path, (".peers", "state.json"), max_bytes=3,
    )

    assert text == "abc"


def test_read_text_under_root_no_follow_rejects_invalid_utf8(tmp_path: Path):
    peers_dir = tmp_path / ".peers"
    peers_dir.mkdir()
    (peers_dir / "state.json").write_bytes(b'{"state":"healthy\xff"}')

    with pytest.raises(UnicodeDecodeError):
        read_text_under_root_no_follow(tmp_path, (".peers", "state.json"))


# control-plane prompt and peer-output logs are world-readable by
# default. New files created by safe_io.* and the log dirs created by the
# orchestrator should be private (0600 / 0700) so another local user cannot
# read prompts / stderr tails / peer output.

def _perms(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_write_text_creates_private_file(tmp_path: Path):
    # Default umask on most distros is 022 which leaves 0o644 after a
    # naive open(..., 0o666). The substrate should default to 0o600 so a
    # second local user cannot read control-plane writes.
    prev = os.umask(0o022)
    try:
        target = tmp_path / "state.json"
        write_text_no_symlink(target, "{}\n")
        assert _perms(target) == 0o600
    finally:
        os.umask(prev)


def test_append_text_creates_private_file(tmp_path: Path):
    prev = os.umask(0o022)
    try:
        target = tmp_path / "runs.jsonl"
        append_text_no_symlink(target, "{}\n")
        assert _perms(target) == 0o600
    finally:
        os.umask(prev)


def test_open_text_in_dir_creates_private_file(tmp_path: Path):
    parent = tmp_path / "log"
    parent.mkdir()
    prev = os.umask(0o022)
    try:
        with open_text_in_dir_no_symlink(parent, "runs.jsonl", "a") as f:
            f.write("x\n")
        assert _perms(parent / "runs.jsonl") == 0o600
    finally:
        os.umask(prev)


def test_existing_world_readable_file_is_tightened(tmp_path: Path):
    # An older substrate version may have created peer output logs with
    # 0o644. Subsequent appends through the helper should narrow the
    # permission so the post-upgrade state is private even without an
    # explicit chmod by the operator.
    target = tmp_path / "tick.log"
    target.write_text("legacy\n")
    os.chmod(target, 0o644)
    append_text_no_symlink(target, "new\n")
    assert _perms(target) & 0o077 == 0
    # owner bits preserved
    assert _perms(target) & 0o700 == 0o600


def test_ensure_private_dir_creates_0700(tmp_path: Path):
    prev = os.umask(0o022)
    try:
        d = tmp_path / "log"
        _ensure_private_dir(d)
        assert _perms(d) == 0o700
    finally:
        os.umask(prev)


def test_ensure_private_dir_creates_parents(tmp_path: Path):
    # Only the leaf is guaranteed 0o700; parents fall to umask. Callers
    # that want each level narrowed should call _ensure_private_dir per
    # level (driver_orchestrator does this for peer_dir -> log -> peers).
    prev = os.umask(0o022)
    try:
        d = tmp_path / "log" / "peers"
        _ensure_private_dir(d)
        assert _perms(d) == 0o700
    finally:
        os.umask(prev)


def test_ensure_private_dir_tightens_existing(tmp_path: Path):
    d = tmp_path / "log"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    _ensure_private_dir(d)
    assert _perms(d) & 0o077 == 0


def test_ensure_private_dir_rejects_symlink(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "log"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        _ensure_private_dir(link)


def test_ensure_private_dir_rejects_symlink_swapped_after_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.chmod(outside, 0o755)
    target = tmp_path / "log"
    real_mkdir = Path.mkdir

    def racing_mkdir(self: Path, *args, **kwargs):
        result = real_mkdir(self, *args, **kwargs)
        if self == target:
            self.rmdir()
            self.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    with pytest.raises(OSError):
        _ensure_private_dir(target)

    assert target.is_symlink()
    assert target.resolve() == outside
    assert _perms(outside) == 0o755


def test_ensure_private_dir_idempotent_on_owner_only(tmp_path: Path):
    d = tmp_path / "log"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    _ensure_private_dir(d)
    assert _perms(d) == 0o700


def test_write_text_in_private_nested_dir_creates_private_tree(tmp_path: Path):
    prev = os.umask(0o022)
    try:
        _write_text_in_private_nested_dir_no_symlink(
            tmp_path, ("log", "peers"), "tick.log", "hello\n",
        )
    finally:
        os.umask(prev)

    assert (tmp_path / "log" / "peers" / "tick.log").read_text() == "hello\n"
    assert _perms(tmp_path / "log") == 0o700
    assert _perms(tmp_path / "log" / "peers") == 0o700
    assert _perms(tmp_path / "log" / "peers" / "tick.log") == 0o600


def test_write_text_in_private_nested_dir_tightens_existing_dirs(
    tmp_path: Path,
):
    log_dir = tmp_path / "log"
    peer_dir = log_dir / "peers"
    peer_dir.mkdir(parents=True)
    os.chmod(log_dir, 0o755)
    os.chmod(peer_dir, 0o755)

    _write_text_in_private_nested_dir_no_symlink(
        tmp_path, ("log", "peers"), "tick.log", "hello\n",
    )

    assert _perms(log_dir) & 0o077 == 0
    assert _perms(peer_dir) & 0o077 == 0


def test_write_text_in_private_nested_dir_refuses_symlinked_parent(
    tmp_path: Path,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "log").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="symlinked dir"):
        _write_text_in_private_nested_dir_no_symlink(
            tmp_path, ("log", "peers"), "tick.log", "hello\n",
        )

    assert not (outside / "peers" / "tick.log").exists()


def test_write_text_in_private_nested_dir_refuses_late_hardlink_without_truncating(
    tmp_path: Path, monkeypatch,
):
    """BUG-120 (v15 internal testing): the nested-dir writer opened the leaf with
    O_TRUNC *before* the post-open nlink check, so a hardlink raced in
    between the pre-stat and the open got truncated before being rejected —
    clobbering the linked victim. The open must not truncate until after the
    nlink/swap checks pass (the pattern open_text_no_symlink already uses)."""
    bait = tmp_path / "bait.txt"
    bait.write_text("keep me")
    target = tmp_path / "log" / "peers" / "tick.log"

    real_open = os.open
    raced = {"done": False}

    def racing_open(path, flags, *args, **kwargs):
        # Slip a hardlink (bait -> the leaf) in just before the helper's
        # real create-open, simulating a TOCTOU race the pre-stat missed.
        if (
            not raced["done"]
            and str(path).endswith("tick.log")
            and (flags & os.O_CREAT)
        ):
            raced["done"] = True
            try:
                os.link(bait, target)
            except OSError as e:
                pytest.skip(f"hard links unavailable: {e}")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)

    with pytest.raises(OSError, match="hard-linked"):
        _write_text_in_private_nested_dir_no_symlink(
            tmp_path, ("log", "peers"), "tick.log", "clobber\n",
        )

    assert raced["done"], "race hook never fired — test did not exercise the path"
    assert bait.read_text() == "keep me"


def test_existing_owner_only_file_stays_private(tmp_path: Path):
    # If the file is already private (0o600), the helper should not
    # widen it. We avoid 0o400 here because O_WRONLY needs write bits.
    target = tmp_path / "tick.log"
    target.write_text("legacy\n")
    os.chmod(target, 0o600)
    append_text_no_symlink(target, "new\n")
    assert _perms(target) == 0o600


def test_atomic_write_ignores_preplanted_predictable_tmp_BUG_181(
    tmp_path: Path,
) -> None:
    """BUG-181: atomic_write_text_in_dir_no_symlink wrote to ``<name>.tmp``
    opened with O_CREAT but no O_EXCL. A same-UID peer with write access
    to the control directory can pre-plant ``<name>.tmp`` as a symlink (or
    a hardlink) and let the writer race them between open(no-trunc) and
    os.replace(). After the fix, the helper uses a unique random temp
    name so a pre-planted predictable name is bypassed entirely — the
    atomic write succeeds AND the attacker's bait is untouched.
    """
    from peers.safe_io import atomic_write_text_in_dir_no_symlink

    target = tmp_path / "state.json"
    bait = tmp_path / "bait"
    bait.write_text("keep me", encoding="utf-8")

    # Pre-plant the OLD predictable temp name as a symlink to bait.
    (tmp_path / "state.json.tmp").symlink_to(bait)

    # On the buggy code: the writer tries to open state.json.tmp without
    # O_EXCL; on Linux the open follows the symlink → either bait is
    # rewritten or an OSError leaks before a successful rename. After
    # the fix the writer picks a fresh random name so this never matters.
    atomic_write_text_in_dir_no_symlink(target, "fresh\n")

    # Final file must contain the new content.
    assert target.read_text(encoding="utf-8") == "fresh\n"
    # And the bait MUST be untouched (no follow-through).
    assert bait.read_text(encoding="utf-8") == "keep me"


def test_atomic_write_temp_name_is_unpredictable_BUG_181(
    tmp_path: Path, monkeypatch,
) -> None:
    """BUG-181: the writer's temp filename must be unique per call, not
    ``<name>.tmp``. We snapshot the directory contents from inside the
    fdopen write (when the temp file is mid-flight) and assert no entry
    matches the legacy predictable name."""
    from peers.safe_io import atomic_write_text_in_dir_no_symlink

    target = tmp_path / "state.json"
    seen_tmp_names: list[str] = []

    real_fdopen = os.fdopen

    def spy_fdopen(fd, mode, *args, **kwargs):
        # Snapshot tmp dir entries while the temp file is still on disk.
        seen_tmp_names.extend(
            n for n in os.listdir(tmp_path)
            if n.endswith(".tmp") or ".tmp" in n
        )
        return real_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", spy_fdopen)
    atomic_write_text_in_dir_no_symlink(target, "v1\n")
    assert seen_tmp_names, "expected to observe a temp file during the write"
    # Must NOT match the old predictable name.
    assert "state.json.tmp" not in seen_tmp_names, (
        f"writer used predictable temp name: {seen_tmp_names!r}"
    )
    # Must include something with the target's stem in the name (so
    # the temp lives next to the target and rename semantics work).
    assert any("state.json" in n for n in seen_tmp_names)


def test_atomic_write_refuses_preplanted_tmp_collision_BUG_181(
    tmp_path: Path, monkeypatch,
) -> None:
    """BUG-181: even if an attacker guesses the random temp name and
    plants it before our O_EXCL open, the helper must refuse rather
    than truncating. We force the collision by stubbing token_hex to
    a fixed value, then pre-planting that exact name as a bait
    symlink. The writer must raise without touching the bait."""
    import secrets
    from peers.safe_io import atomic_write_text_in_dir_no_symlink

    target = tmp_path / "state.json"
    bait = tmp_path / "bait"
    bait.write_text("keep me", encoding="utf-8")

    monkeypatch.setattr(secrets, "token_hex", lambda n=8: "deadbeefcafebabe")
    (tmp_path / "state.json.deadbeefcafebabe.tmp").symlink_to(bait)

    with pytest.raises(OSError):
        atomic_write_text_in_dir_no_symlink(target, "fresh\n")
    assert bait.read_text(encoding="utf-8") == "keep me"
