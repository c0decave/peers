"""Tests for the optional live-tee on `_StreamCollector` (Wave-2 TUI §5.1).

The tee mirrors a peer stream's decoded text into a tail-able
``tick-<N>-<peer>.stream.jsonl`` so codex/opencode are live-watchable like
claude. It is **additive, default-OFF, and fail-CLOSED**: a tee failure must
never disturb the reader thread, the idle-timeout liveness signal, or the
scan cursor.

Coverage (every test names its category):
- happy: tee enabled → file created, grows incrementally (tail-able mid-stream),
  ordered decoded bytes.
- sad (fail-closed, load-bearing): tee write raises → reader survives,
  last_output_t still updates, no false idle-timeout, run still succeeds,
  tee_degraded set.
- edge: multibyte utf-8 split across two os.read chunks; truncation leaves the
  tee complete (pre-truncation bytes captured); grandchild-held pipe +
  request_stop still flushes/closes (no lost tail, no leak).
- default-off: flag unset → no .stream.jsonl, behaviour byte-identical.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from peers.health_guard import HealthGuard, _StreamCollector, _TeeWriter

FIX = Path(__file__).parent.parent / "fixtures"


def _make_collector(name: str, read_fd: int, tee_path: Path | None):
    """Build a real `_StreamCollector` reading the read-end of an os.pipe()."""
    stream = os.fdopen(read_fd, "rb", buffering=0)
    shared = {"last_output_t": time.monotonic()}
    shared_lock = threading.Lock()
    return _StreamCollector(
        name, stream, shared, shared_lock,
        scan_enabled=False, tee_path=tee_path,
    ), shared, shared_lock


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------

def test_tee_writes_decoded_bytes_in_order(tmp_path: Path):
    """happy: feed a stream → tee file holds the decoded bytes in order."""
    tee_path = tmp_path / "tick-00001-claude.stream.jsonl"
    r, w = os.pipe()
    col, _shared, _lock = _make_collector("out", r, tee_path)
    col.start()
    try:
        os.write(w, b"line one\n")
        os.write(w, b'{"type":"text"}\n')
        os.write(w, b"line three\n")
    finally:
        os.close(w)
    col.join(timeout=5)

    assert tee_path.exists()
    assert tee_path.read_text() == 'line one\n{"type":"text"}\nline three\n'
    # In-memory buf and the tee agree.
    assert col.text() == tee_path.read_text()
    assert col.tee is not None and col.tee.degraded is False


def test_tee_is_tailable_mid_stream(tmp_path: Path):
    """happy: the tee grows incrementally — readable BEFORE the stream ends
    (the whole point of a live tee for the TUI)."""
    tee_path = tmp_path / "tick-00002-codex.stream.jsonl"
    r, w = os.pipe()
    col, _shared, _lock = _make_collector("out", r, tee_path)
    col.start()
    try:
        os.write(w, b"first chunk\n")
        # Poll until the first chunk has landed on disk while the stream is
        # still open (no EOF yet).
        deadline = time.monotonic() + 5
        seen = ""
        while time.monotonic() < deadline:
            if tee_path.exists():
                seen = tee_path.read_text()
                if "first chunk" in seen:
                    break
            time.sleep(0.02)
        assert "first chunk" in seen, "tee not tail-able mid-stream"
        # Stream is still open; now send more and confirm growth.
        os.write(w, b"second chunk\n")
    finally:
        os.close(w)
    col.join(timeout=5)
    assert tee_path.read_text() == "first chunk\nsecond chunk\n"


def test_invoke_end_to_end_tee_enabled(tmp_path: Path):
    """happy: full invoke() with tee_dir/tee_tag wires stdout into
    <tag>.stream.jsonl and succeeds."""
    hg = HealthGuard(cwd=tmp_path)
    tee_dir = tmp_path / ".peers" / "log" / "peers"
    r = hg.invoke(
        [str(FIX / "fake_cli_ok.sh")], prompt="ignored",
        idle_timeout_s=30, absolute_max_runtime_s=5,
        tee_dir=tee_dir, tee_tag="tick-00007-claude",
    )
    assert r.classification == "success"
    stream_file = tee_dir / "tick-00007-claude.stream.jsonl"
    assert stream_file.exists()
    assert "doing work" in stream_file.read_text()


# --------------------------------------------------------------------------
# sad path (fail-closed) — LOAD-BEARING
# --------------------------------------------------------------------------

def test_tee_write_failure_does_not_break_reader_or_liveness(tmp_path: Path):
    """sad/fail-closed (load-bearing): a tee whose write() always raises must
    NOT kill the reader, NOT stop last_output_t from updating, and must mark
    itself degraded — proving no fail-OPEN idle-timeout regression."""
    tee_path = tmp_path / "tick-00003-codex.stream.jsonl"
    r, w = os.pipe()
    col, shared, lock = _make_collector("out", r, tee_path)

    # Force every tee write to raise; the collector must absorb it.
    class _Boom:
        def __init__(self) -> None:
            self.degraded = False

        def write(self, _text: str) -> None:
            raise OSError("simulated tee disk failure")

        def close(self) -> None:
            self.degraded = True

    col.tee = _Boom()

    with lock:
        baseline = shared["last_output_t"]
    col.start()
    try:
        os.write(w, b"chunk after which tee explodes\n")
        # last_output_t must advance despite the tee raising on this chunk.
        deadline = time.monotonic() + 5
        advanced = False
        while time.monotonic() < deadline:
            with lock:
                if shared["last_output_t"] > baseline:
                    advanced = True
                    break
            time.sleep(0.02)
        assert advanced, "last_output_t did not update — tee broke liveness!"
        os.write(w, b"reader is still alive\n")
    finally:
        os.close(w)
    col.join(timeout=5)

    # Reader thread completed cleanly and captured BOTH chunks in buf.
    assert not col.thread.is_alive()
    text = col.text()
    assert "chunk after which tee explodes" in text
    assert "reader is still alive" in text


def test_tee_blocking_write_does_not_freeze_liveness(tmp_path: Path):
    """sad/fail-OPEN regression (load-bearing): a tee whose write() BLOCKS
    (does NOT raise — e.g. a full disk / stalled NFS / throttled writeback;
    O_NONBLOCK is a no-op for regular files) must NOT freeze the liveness
    signal. The fail-CLOSED guard only catches RAISED exceptions, so a
    blocking tee would, if liveness were published AFTER the tee write,
    stall `last_output_t` for the whole block — and the idle-timeout / hang
    watchdog could kill a LIVE peer.

    This asserts `last_output_t` advances to ~read time WITHIN a tight bound
    while the tee write is STILL blocked (the event is not yet set), proving
    the liveness publish happens from the live read and is not gated on the
    tee fd. Then release the block and confirm clean teardown.
    """
    tee_path = tmp_path / "tick-00011-codex.stream.jsonl"
    r, w = os.pipe()
    col, shared, lock = _make_collector("out", r, tee_path)

    release = threading.Event()
    write_entered = threading.Event()

    class _Blocking:
        """A tee whose write() blocks until `release` is set (never raises)."""

        def __init__(self) -> None:
            self.degraded = False
            self.writes = 0

        def write(self, _text: str) -> None:
            self.writes += 1
            write_entered.set()
            # Block — but bounded, so a regression can't hang the suite.
            release.wait(timeout=10)

        def close(self) -> None:
            self.degraded = True

    col.tee = _Blocking()

    with lock:
        baseline = shared["last_output_t"]
    col.start()
    try:
        os.write(w, b"a chunk that makes the tee block\n")
        # The reader must enter the (blocking) tee write...
        assert write_entered.wait(timeout=5), "tee write was never reached"

        # ...and WHILE that write is still blocked (release not set),
        # last_output_t must already have advanced past the baseline,
        # within a tight bound — proving the liveness publish is NOT
        # gated on the tee fd. With the old post-append ordering, this
        # would stay frozen at `baseline` until the block clears.
        deadline = time.monotonic() + 0.2  # < 200ms tight bound
        advanced = False
        while time.monotonic() < deadline:
            assert not release.is_set()  # block still held
            with lock:
                if shared["last_output_t"] > baseline:
                    advanced = True
                    break
            time.sleep(0.005)
        assert advanced, (
            "last_output_t did not advance while the tee write was still "
            "blocked — liveness is gated on the tee (fail-OPEN)!"
        )
        # The tee really is still blocked (the reader is parked in write()).
        assert not release.is_set()
    finally:
        # Release the block, let the reader drain + tear down cleanly.
        release.set()
        os.close(w)
    col.join(timeout=5)

    assert not col.thread.is_alive()
    assert "a chunk that makes the tee block" in col.text()


def test_tee_write_failure_via_real_boom_writer(tmp_path: Path):
    """sad/fail-closed: use the REAL _TeeWriter pointed at an un-creatable
    path (parent is a file, not a dir) → degrades, reader unaffected."""
    not_a_dir = tmp_path / "iam_a_file"
    not_a_dir.write_text("x")
    tee_path = not_a_dir / "tick-00004-opencode.stream.jsonl"  # parent is a file
    r, w = os.pipe()
    col, shared, lock = _make_collector("out", r, tee_path)
    col.start()
    try:
        os.write(w, b"hello despite broken tee\n")
    finally:
        os.close(w)
    col.join(timeout=5)

    assert not col.thread.is_alive()
    assert "hello despite broken tee" in col.text()
    assert col.tee is not None and col.tee.degraded is True
    # No bogus file/dir got created at the broken location.
    assert not tee_path.exists()


def test_invoke_with_failing_tee_still_succeeds(tmp_path: Path):
    """sad/fail-closed: invoke() with a tee_dir that cannot be created (a file
    sits where the dir should be) still classifies success — observability
    failure must never fail the run."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke(
        [str(FIX / "fake_cli_ok.sh")], prompt="ignored",
        idle_timeout_s=30, absolute_max_runtime_s=5,
        tee_dir=blocker / "peers", tee_tag="tick-00009-claude",
    )
    assert r.classification == "success"
    assert "doing work" in r.stdout


# --------------------------------------------------------------------------
# edge cases
# --------------------------------------------------------------------------

def test_tee_multibyte_split_across_chunks_is_not_corrupted(tmp_path: Path):
    """edge: a 4-byte utf-8 emoji split across two os.read chunks is tee'd
    intact (the tee mirrors DECODED text, so boundaries can't corrupt it)."""
    tee_path = tmp_path / "tick-00005-claude.stream.jsonl"
    r, w = os.pipe()
    col, _shared, _lock = _make_collector("out", r, tee_path)
    col.start()
    emoji = "🛰".encode("utf-8")  # 4 bytes: f0 9f 9b b0
    assert len(emoji) == 4
    try:
        # Write the first 3 bytes, let the reader consume them (incomplete
        # char → decoder buffers it, nothing emitted yet), then the last byte.
        os.write(w, b"prefix " + emoji[:3])
        time.sleep(0.2)
        os.write(w, emoji[3:] + b" suffix\n")
    finally:
        os.close(w)
    col.join(timeout=5)
    assert tee_path.read_text() == "prefix 🛰 suffix\n"


def test_tee_captures_pre_truncation_bytes(tmp_path: Path):
    """edge: even when the in-memory scan/buf hits the soft cap and truncates,
    the tee captures the full pre-truncation byte stream (truncation only
    rewrites self.buf; the tee already mirrored everything)."""
    tee_path = tmp_path / "tick-00006-codex.stream.jsonl"
    # Build a collector with a tiny cap to force truncation of buf, directly
    # driving _append_chunk (deterministic, no thread/pipe timing).
    stream = os.fdopen(os.open(os.devnull, os.O_RDONLY), "rb", buffering=0)
    shared = {"last_output_t": time.monotonic()}
    col = _StreamCollector(
        "out", stream, shared, threading.Lock(),
        buf_cap_bytes=4096, scan_enabled=True, tee_path=tee_path,
    )
    total = b""
    for i in range(50):
        line = (f"line-{i:04d}-" + "y" * 200 + "\n")
        col._append_chunk(line, len(line.encode()))
        total += line.encode()
    col.tee.close()
    stream.close()

    # buf truncated in memory...
    assert col._truncated is True
    assert len("".join(col.buf).encode()) <= 4096 + 4096
    # ...but the tee has the COMPLETE pre-truncation stream.
    assert tee_path.read_bytes() == total


def test_tee_flushes_and_closes_with_grandchild_held_pipe(tmp_path: Path):
    """edge: a grandchild keeps the pipe write-end open; request_stop() makes
    the reader exit, and the finally-path flushes+closes the tee so the last
    bytes aren't lost and the fd doesn't leak."""
    hg = HealthGuard(cwd=tmp_path)
    tee_dir = tmp_path / ".peers" / "log" / "peers"
    threads_before = threading.active_count()
    t0 = time.monotonic()
    r = hg.invoke(
        [str(FIX / "fake_cli_grandchild_holds_pipe.sh")],
        prompt="ignored", idle_timeout_s=30, absolute_max_runtime_s=15,
        tee_dir=tee_dir, tee_tag="tick-00008-claude",
    )
    dt = time.monotonic() - t0
    assert r.classification == "success"
    assert dt < 5.0, f"invoke took {dt:.2f}s — request_stop/tee close stalled"

    stream_file = tee_dir / "tick-00008-claude.stream.jsonl"
    # The parent's line must have been flushed to the tee before close.
    assert stream_file.exists()
    assert "hello from parent" in stream_file.read_text()

    # No reader-thread leak (tee close happened in the reader's finally).
    time.sleep(0.5)
    leaked = [t for t in threading.enumerate()
              if t.name.startswith("hg-reader-")]
    assert not leaked, f"reader threads leaked: {[t.name for t in leaked]}"
    assert threading.active_count() <= threads_before + 2


def test_tee_empty_text_writes_nothing(tmp_path: Path):
    """edge: an empty-string write is a no-op (no fd opened, no file)."""
    tee_path = tmp_path / "tick-00010-claude.stream.jsonl"
    tee = _TeeWriter(tee_path)
    tee.write("")
    tee.close()
    assert not tee_path.exists()
    assert tee.degraded is False


# --------------------------------------------------------------------------
# default-off
# --------------------------------------------------------------------------

def test_default_off_no_tee_file_and_no_tee_object(tmp_path: Path):
    """default-off: tee_path=None → no _TeeWriter, no file, behaviour identical."""
    r, w = os.pipe()
    col, _shared, _lock = _make_collector("out", r, None)
    assert col.tee is None
    col.start()
    try:
        os.write(w, b"plain output\n")
    finally:
        os.close(w)
    col.join(timeout=5)
    assert col.text() == "plain output\n"
    # No stray .stream.jsonl anywhere under tmp.
    assert list(tmp_path.glob("**/*.stream.jsonl")) == []


def test_invoke_default_off_writes_no_stream_file(tmp_path: Path):
    """default-off: invoke() without tee_dir writes no .stream.jsonl."""
    hg = HealthGuard(cwd=tmp_path)
    r = hg.invoke(
        [str(FIX / "fake_cli_ok.sh")], prompt="ignored",
        idle_timeout_s=30, absolute_max_runtime_s=5,
    )
    assert r.classification == "success"
    assert list(tmp_path.glob("**/*.stream.jsonl")) == []


def test_tee_writer_open_failure_is_swallowed(tmp_path: Path):
    """sad: _TeeWriter._ensure_open failure degrades without raising; further
    writes are silent no-ops (fd never reopened)."""
    # parent is a regular file → open_text_in_dir_no_symlink can't opendir it.
    parent_file = tmp_path / "afile"
    parent_file.write_text("x")
    tee = _TeeWriter(parent_file / "child.stream.jsonl")
    tee.write("data")   # triggers open attempt → degrade
    assert tee.degraded is True
    tee.write("more")   # still a no-op, no raise
    tee.close()         # idempotent, no raise
