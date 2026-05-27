"""Two CommLayer variants:

- GitCommLayer (default): commits + trailers ARE the message bus.
- HybridCommLayer: code travels via git (same), but free-form messages
  travel via files in .peers/comms/<from>-to-<to>/NNNN-<topic>.md.
"""
from __future__ import annotations

import datetime as _dt
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from peers.peer_spec import is_valid_peer_name
from peers.safe_io import _ensure_private_dir

# Trailer keys must be at least 2 chars to avoid matching one-letter
# words at the start of body lines. They must also not be common
# scheme prefixes that look like trailers (`http`, `https`, `ftp`, ...).
_TRAILER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]{1,}):\s*(.*?)\s*$")
_URL_SCHEME_KEYS = {"http", "https", "ftp", "ftps", "ssh", "file", "ws", "wss"}


def parse_trailers(message: str) -> dict[str, str]:
    """Parse git-style trailers from the bottom of a commit message.

    Walks lines from the end upward, collecting `Key: value` lines until
    a blank line or a non-trailer line is hit. CRLF tolerated.

    Defensive against URL-like body lines (`https://...`) and other
    non-trailer content masquerading as trailers: keys must be ≥ 2
    chars, the value must not start with `//`, and we explicitly
    reject known URL scheme keys.

    L3: when the SAME key appears multiple times (e.g. a hedging
    `Self-Review: fail` followed by a flipped `Self-Review: pass`),
    the FIRST occurrence walking from the end wins — i.e. we honor
    the LATER-AUTHORED trailer-LINE, but ignore any earlier
    contradicting one. This collapses to "later value wins"
    deterministically rather than "last in dict insertion order".
    """
    lines = [line_.rstrip("\r") for line_ in message.rstrip().splitlines()]
    trailers: dict[str, str] = {}
    for line in reversed(lines):
        if line.strip() == "":
            break
        m = _TRAILER_RE.match(line)
        if not m:
            break
        key, value = m.group(1), m.group(2)
        if key.lower() in _URL_SCHEME_KEYS:
            break
        if value.startswith("//"):
            break
        # First-seen wins (which, walking from the END, means the
        # last-written trailer for that key in the source message
        # wins deterministically).
        if key not in trailers:
            trailers[key] = value
    return trailers


@dataclass
class Commit:
    sha: str
    subject: str
    body: str
    trailers: dict[str, str]


class GitCommLayer:
    def __init__(self, repo: Path) -> None:
        self.repo = Path(repo)

    def _git(self, *args: str) -> str:
        r = subprocess.run(
            ["git", *args], cwd=self.repo, check=True,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        return r.stdout

    def head_sha(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def new_commits_by(self, peer: str, since: str | None) -> list[Commit]:
        # Bootstrap: with no cursor, return [] rather than walking all of
        # HEAD. Caller seeds the cursor at head_sha() on first read.
        if since is None:
            return []
        rev_range = f"{since}..HEAD"
        # Use `-z` to NUL-terminate records and a NUL-safe field
        # separator inside each record. This is robust against commit
        # bodies containing any byte (including the old \x1e / \x1f
        # separators we used to pick).
        fmt = "%H%x00%s%x00%B"
        try:
            out = self._git("log", "-z", rev_range,
                            f"--format={fmt}", "--reverse")
        except subprocess.CalledProcessError:
            return []
        commits: list[Commit] = []
        # `-z` makes commits NUL-terminated; each record itself contains
        # three NUL-separated fields (sha, subject, body).
        # `out` ends up structured as:  sha\0subject\0body\0  per commit.
        # We split on \0 with maxsplit, then walk in 3-field chunks.
        parts = out.split("\x00")
        # Trailing empty string after final \0.
        if parts and parts[-1] == "":
            parts.pop()
        i = 0
        while i + 2 < len(parts):
            sha = parts[i].lstrip("\n")
            subject = parts[i + 1]
            body = parts[i + 2]
            i += 3
            trailers = parse_trailers(body)
            if trailers.get("Peer") != peer:
                continue
            commits.append(Commit(
                sha=sha, subject=subject,
                body=body, trailers=trailers,
            ))
        return commits


class HybridCommLayer:
    """Code via Git (same as GitCommLayer), free-form messages via
    `.peers/comms/<from>-to-<to>/NNNN-<topic>.md`.

    Handoff and self-review trailers still live on commits — the file
    channel is purely for things that don't fit a commit: status notes,
    soft-review request bodies, longer review responses, etc.
    """

    def __init__(self, repo: Path, peer_dir: Path) -> None:
        self.git_layer = GitCommLayer(repo)
        self.peer_dir = Path(peer_dir)

    # Delegate code-side calls to the git layer.
    def head_sha(self) -> str:
        return self.git_layer.head_sha()

    def new_commits_by(self, peer: str, since: str | None) -> list[Commit]:
        return self.git_layer.new_commits_by(peer=peer, since=since)

    # File-channel API.
    def _inbox_dir(self, sender: str, receiver: str) -> Path:
        return self.peer_dir / "comms" / f"{sender}-to-{receiver}"

    def _archive_dir(self) -> Path:
        return self.peer_dir / "comms" / "archive"

    def send(self, sender: str, receiver: str, topic: str,
             body: str) -> Path:
        """Drop a message file. Returns the path written.

        Race-safe and atomic-publication: the payload is written to a
        dotfile first, then linked into its final NNNN name. Consumers only
        glob final `NNNN-*.md` files, so they never archive an empty or
        partially written message.
        """
        # Defence against path traversal: peer names must be tame
        # tokens — they end up as directory components. Reject anything
        # that would let a sender redirect the message file out of the
        # inbox.
        for who, label in ((sender, "sender"), (receiver, "receiver")):
            if not is_valid_peer_name(who):
                raise ValueError(
                    f"hybrid comm: invalid {label} name: {who!r}"
                )
        _ensure_private_dir(self.peer_dir)
        _ensure_private_dir(self.peer_dir / "comms")
        d = self._inbox_dir(sender, receiver)
        _ensure_private_dir(d)
        safe_topic = re.sub(r"[^a-zA-Z0-9_-]+", "-", topic).strip("-")[:40]
        safe_topic = safe_topic or "msg"

        # Seed the sequence at the highest existing+1 number, then keep
        # bumping until O_EXCL succeeds.
        existing = sorted(d.glob("[0-9][0-9][0-9][0-9]-*.md"))
        next_n = 1 + (int(existing[-1].name[:4]) if existing else 0)
        ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
        payload = (
            f"---\nfrom: {sender}\nto: {receiver}\nts: {ts}\n"
            f"topic: {topic}\n---\n\n{body.rstrip()}\n"
        )
        import os as _os
        for attempt in range(10_000):  # generous upper bound on collisions
            path = d / f"{next_n:04d}-{safe_topic}.md"
            tmp_path = d / (
                f".{next_n:04d}-{safe_topic}.{_os.getpid()}.{attempt}.tmp"
            )
            try:
                flags = _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL
                flags |= getattr(_os, "O_NOFOLLOW", 0)
                flags |= getattr(_os, "O_CLOEXEC", 0)
                fd = _os.open(
                    str(tmp_path),
                    flags,
                    0o600,
                )
            except FileExistsError:
                next_n += 1
                continue
            try:
                with _os.fdopen(fd, "w") as f:
                    f.write(payload)
                    f.flush()
                    _os.fsync(f.fileno())
                try:
                    _os.link(str(tmp_path), str(path))
                except FileExistsError:
                    next_n += 1
                    continue
                return path
            except OSError:
                # If write fails, clean up the empty file and retry.
                raise
            finally:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        raise RuntimeError(
            f"hybrid comm: gave up after 10000 attempts to create a "
            f"unique inbox file in {d}"
        )

    def fetch_new(self, sender: str, receiver: str) -> list[Path]:
        """Returns sorted list of unarchived messages from sender→receiver."""
        d = self._inbox_dir(sender, receiver)
        if not d.exists():
            return []
        return sorted(d.glob("[0-9][0-9][0-9][0-9]-*.md"))

    def archive(self, path: Path) -> None:
        """Move a consumed message to the archive.

        NNNN sequences are unique only
        WITHIN a sender-to-receiver inbox. Two pairs (e.g. claude→codex
        and codex→claude) both have 0001-... and a flat archive dir
        would clobber the second on rename. Namespace the archive by
        the source inbox directory name so cross-direction collisions
        are impossible.
        """
        a = self._archive_dir()
        _ensure_private_dir(self.peer_dir)
        _ensure_private_dir(self.peer_dir / "comms")
        _ensure_private_dir(a)
        # Source inbox dirname (e.g. "claude-to-codex") becomes the
        # archive subdir; keep filename intact for forensics.
        inbox_name = path.parent.name if path.parent is not None else ""
        dest_dir = (a / inbox_name) if inbox_name else a
        _ensure_private_dir(dest_dir)
        target = dest_dir / path.name
        if target.exists():
            # Belt-and-braces: even within the same inbox, NNNN reuse
            # could happen after a manual cleanup. Append a numeric
            # suffix so we never overwrite history.
            n = 1
            while True:
                candidate = dest_dir / f"{path.stem}.{n}{path.suffix}"
                if not candidate.exists():
                    target = candidate
                    break
                n += 1
        try:
            path.rename(target)
        except FileNotFoundError:
            pass
