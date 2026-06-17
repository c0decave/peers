"""bug-hunt protocol.

Each peer files findings as commits carrying a `Bug-Report:` trailer and
resolves them via `Bug-Resolves:` trailers. Severity (`crit|high|med|
low|info`) drives the hard gate: a project is `bug-hunt-clean` iff
**no open bug at severity >= med** is unresolved.

Schema — bug-filing commit:

    Bug-001: <short title>

    ## Bug-Report
    {
      "id": "BUG-001",
      "severity": "high",                 // crit|high|med|low|info
      "fix_by": "codex",                  // optional — peer expected to fix
      "location": "src/x.py:42",          // optional
      "description": "..."
    }

    Bug-Report: BUG-001
    Peer: claude

Schema — resolution commit:

    Resolve BUG-001: <short note>

    ## Bug-Resolution
    {
      "resolves": "BUG-001",
      "status": "fixed",                  // fixed|wontfix|duplicate|invalid
      "note": "..."
    }

    Bug-Resolves: BUG-001
    Peer: codex

Parsing tolerates:
- multiple JSON blocks per commit (only the FIRST is used);
- the `## Bug-Report` / `## Bug-Resolution` heading is optional (we fall
  back to extracting the first balanced `{...}` block in the body when
  a heading is missing);
- duplicate Bug-Report IDs across commits (most-recent wins, with a
  warning surfaced via `summary().warnings`);
- duplicate Bug-Resolves IDs (most-recent resolution status wins).
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path


SEVERITY_ORDER = ("crit", "high", "med", "low", "info")
BLOCKING_SEVERITIES = frozenset({"crit", "high", "med"})
_SEVERITY_RANK = {sev: i for i, sev in enumerate(SEVERITY_ORDER)}
_TRAILER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]{1,}):\s*(.*?)\s*$")
# `git cherry-pick -x` appends `(cherry picked from commit <sha>)`
# AFTER the trailer block. git's own `interpret-trailers --parse` treats it
# as a skippable git-generated footer, not a stop marker; we match that so
# a cherry-picked Resolve commit's trailers are still parsed.
# SHA-1 oids are 40 hex chars; SHA-256 oids are 64. Cap at 64 so
# repos created with `git init --object-format=sha256` are handled too.
_CHERRY_PICK_FOOTER_RE = re.compile(
    r"^\(cherry picked from commit [0-9a-f]{4,64}\)$"
)
_TDD_SUBJECT_RE = re.compile(
    r"^TDD:\s*reproducer\s+for\s+(BUG-\d+)\b",
    re.IGNORECASE,
)
_URL_SCHEME_KEYS = {"http", "https", "ftp", "ftps", "ssh", "file", "ws", "wss"}
_TDD_WAIVER_REASON_MIN = 40


@dataclass
class BugReport:
    id: str
    severity: str
    title: str = ""
    description: str = ""
    fix_by: str | None = None
    location: str | None = None
    cwe: str | None = None
    file: str | None = None
    function: str | None = None
    sha: str = ""               # commit that filed the bug
    found_by: str | None = None  # `Peer:` trailer of that commit


@dataclass
class BugResolution:
    id: str
    status: str                 # fixed | wontfix | duplicate | invalid | deferred
    note: str = ""
    sha: str = ""
    resolved_by: str | None = None


@dataclass
class BugReproduction:
    """A commit that carries `Bug-Reproduce: BUG-NNN`, or the older
    `TDD: reproducer for BUG-NNN` subject form — a failing test added
    BEFORE the fix, for the TDD-reproduce-first workflow."""
    id: str
    sha: str = ""
    reproduced_by: str | None = None


# `fixed` and `deferred` both close a bug for blocking-gate purposes.
# `deferred` exists so peers have an honest "this is too big for this
# session, here's the rationale + next-step note" path instead of
# half-fixing under time pressure. wontfix / duplicate / invalid stay
# strictly open (intentional from the original semantics — a human
# must explicitly accept those rationales by re-classifying or fixing).
RESOLVED_STATUSES = frozenset({"fixed", "deferred", "wontfix",
                               "duplicate", "invalid"})
CLOSING_STATUSES = frozenset({"fixed", "deferred"})


@dataclass
class BugSummary:
    reports: dict[str, BugReport] = field(default_factory=dict)
    resolutions: dict[str, BugResolution] = field(default_factory=dict)
    # Bug-Reproduce trailers grouped by bug id. A list because the same
    # bug might have multiple reproduction commits (e.g. the first test
    # only covered the happy variant, a follow-up added edge + sad).
    reproductions: dict[str, list[BugReproduction]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def open_by_severity(self) -> dict[str, list[BugReport]]:
        out: dict[str, list[BugReport]] = {s: [] for s in SEVERITY_ORDER}
        for bid, rep in self.reports.items():
            res = self.resolutions.get(bid)
            if res is not None and res.status in CLOSING_STATUSES:
                continue
            sev = rep.severity if rep.severity in SEVERITY_ORDER else "info"
            out[sev].append(rep)
        return out

    @property
    def open_blocking_count(self) -> int:
        out = self.open_by_severity
        return sum(len(out[s]) for s in BLOCKING_SEVERITIES)

    @property
    def deferred_count(self) -> int:
        return sum(1 for r in self.resolutions.values()
                   if r.status == "deferred")

    @property
    def reproduced_count(self) -> int:
        return len(self.reproductions)

    def is_clean(self) -> bool:
        return self.open_blocking_count == 0


def parse_commit_trailers(message: str) -> dict[str, list[str]]:
    """Return all trailer KEY → [values]. Last paragraph only, per
    git-trailer convention; permissive about whitespace."""
    lines = [line.rstrip("\r") for line in message.rstrip().splitlines()]
    out: dict[str, list[str]] = {}
    for line in reversed(lines):
        if line.strip() == "":
            break
        if _CHERRY_PICK_FOOTER_RE.match(line):
            # git-generated footer (see BUG-758): not a stop marker.
            continue
        m = _TRAILER_RE.match(line)
        if not m:
            break
        key, value = m.group(1), m.group(2).strip()
        if key.lower() in _URL_SCHEME_KEYS or value.startswith("//"):
            break
        out.setdefault(key, []).insert(0, value)
    return out


def _historical_tdd_subject_reproduce_ids(message: str) -> list[str]:
    """Return BUG ids from the pre-trailer TDD subject convention.

    Early audit turns used commits titled `TDD: reproducer for BUG-NNN`
    before the stricter `Bug-Reproduce:` trailer was documented. Keep the
    compatibility window narrow so ordinary "test: reproduce BUG-NNN"
    subjects cannot satisfy the gate by accident.
    """
    subject = message.splitlines()[0].strip() if message else ""
    match = _TDD_SUBJECT_RE.match(subject)
    return [match.group(1).upper()] if match else []


def _iter_json_blocks(body: str) -> Iterator[dict]:
    """Yield every balanced top-level `{...}` block in `body` that decodes
    as a JSON object, in document order.

    Security-sensitive callers (`_bug_report_block`, `_resolution_block`)
    scan ALL blocks rather than trusting the first one, so a peer cannot
    shadow the real Bug-Report/Resolution JSON by prepending a decoy object
    (BUG-712 gate-integrity hardening)."""
    in_string = False
    escape = False
    depth = 0
    start = -1
    for i, ch in enumerate(body):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                snippet = body[start:i + 1]
                start = -1
                try:
                    val = json.loads(snippet)
                except json.JSONDecodeError:
                    continue
                if isinstance(val, dict):
                    yield val


def _first_json_block(body: str) -> dict | None:
    """First balanced `{...}` JSON object in `body`, or None.

    Retained for non-security callers; Bug-Report severity and
    Bug-Resolution status are selected by id via `_bug_report_block` /
    `_resolution_block` so a decoy object cannot shadow the real one."""
    return next(_iter_json_blocks(body), None)


def _bug_report_block(body: str, bid: str) -> dict | None:
    """Return the Bug-Report JSON object for `bid`: among ALL balanced JSON
    blocks whose `id` equals `bid`, the one with the HIGHEST severity.

    Scanning every block (not just the first) and taking the max severity
    closes the gate-integrity hole BUG-712 + the residual its first
    (heading-anchored) fix missed + the BUG-713 CRLF variant: a peer can no
    longer demote a real crit/high report to `info` by prepending a decoy
    `{...}` object (in the subject, prose, or under the heading), nor
    downgrade it with an earlier lower-severity twin. No heading regex is
    involved, so CRLF/heading formatting cannot defeat it."""
    best: dict | None = None
    best_sev: str | None = None
    for blk in _iter_json_blocks(body):
        if blk.get("id") != bid:
            continue
        sev = _normalize_severity(blk.get("severity"))
        if best_sev is None or _severity_is_higher(sev, best_sev):
            best, best_sev = blk, sev
    return best


def _resolution_block(body: str, bid: str, *keys: str) -> dict | None:
    """First balanced JSON object in `body` for which any of `keys` equals
    `bid` — anchors Bug-Resolution / Bug-Defer JSON to the right id so a
    decoy object cannot shadow the real status/note (BUG-712 hardening)."""
    for blk in _iter_json_blocks(body):
        if any(blk.get(k) == bid for k in keys):
            return blk
    return None


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo),
        check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout


def _normalize_severity(raw: object) -> str:
    sev = str(raw or "info").lower()
    return sev if sev in SEVERITY_ORDER else "info"


def _severity_is_higher(left: str, right: str) -> bool:
    return _SEVERITY_RANK[left] < _SEVERITY_RANK[right]


def count_new_blocking_or_flag_bug_reports(repo: Path, since_sha: str) -> int:
    """Count Bug-Report trailers landed in `since_sha..HEAD` that should
    reset the convergence counter for the `thorough` mode.

    A Bug-Report counts iff:

    - The trailer value starts with a flag prefix — `weak-fix:` or
      `shallow-fix:` — regardless of any JSON severity. Flag-bugs are
      filed by the security mode (devil's-advocate / defense-in-depth)
      and are inherently blocking by construction.
    - OR the commit body carries a JSON object whose `severity` (after
      normalization) is one of {crit, high, med}, matched to the bug id.

    Bug-Reports at severity info/low — or with no parseable JSON block —
    do NOT count. This prevents the thorough-mode loop from spinning
    forever on "info: missing docstring" findings.

    Notes:
    - Empty range (`since_sha..HEAD` produces no commits) → 0.
    - Multiple `Bug-Report:` trailers in the same commit each count
      independently — they are independent findings even when filed
      together. The JSON block (if any) is matched by id; trailers
      whose id does not match the JSON `id` field fall back to the
      info-default and only count if they carry a flag prefix.
    - Any git failure on the range is treated as "no new bugs" (0)
      rather than blowing up the loop.
    """
    if not since_sha:
        return 0
    try:
        log = _git(repo, "log", "-z", "--pretty=format:%H%x00%B",
                   f"{since_sha}..HEAD")
    except subprocess.CalledProcessError:
        return 0
    parts = log.split("\x00")
    if parts and parts[-1] == "":
        parts.pop()
    count = 0
    i = 0
    while i + 1 < len(parts):
        sha = parts[i].lstrip("\n").strip()
        body = parts[i + 1]
        i += 2
        if not sha:
            continue
        trailers = parse_commit_trailers(body)
        report_ids = trailers.get("Bug-Report", [])
        if not report_ids:
            continue
        for bid in report_ids:
            # Flag-bug: the trailer value itself carries the prefix.
            # Prefix detection only is case-insensitive (the BUG-NNN id
            # tail is left untouched — only the discriminator prefix
            # needs normalization). Risk is low (security-mode prompts
            # emit lowercase), but `.lower()` is cheap insurance against
            # a peer that writes `WEAK-FIX:BUG-099`.
            if bid.lower().startswith(("weak-fix:", "shallow-fix:")):
                count += 1
                continue
            # Standard form: severity comes from the JSON block whose
            # `id` matches the trailer value. Anything else → info.
            sev = "info"
            blk = _bug_report_block(body, bid)
            if blk is not None:
                sev = _normalize_severity(blk.get("severity"))
            if sev in BLOCKING_SEVERITIES:
                count += 1
    return count


def list_commits(repo: Path) -> list[tuple[str, str]]:
    """Return [(sha, full-message)] for every commit reachable from HEAD,
    newest first. Each message keeps its subject + body."""
    try:
        log = _git(repo, "log", "-z", "--pretty=format:%H%x00%B", "HEAD")
    except subprocess.CalledProcessError:
        return []
    out: list[tuple[str, str]] = []
    parts = log.split("\x00")
    if parts and parts[-1] == "":
        parts.pop()
    i = 0
    while i + 1 < len(parts):
        sha = parts[i].lstrip("\n").strip()
        body = parts[i + 1]
        i += 2
        if sha:
            out.append((sha, body))
    return out


def summarize(repo: Path) -> BugSummary:
    """Walk git history, build a BugSummary. Newest-first iteration with
    "first-wins" semantics for duplicate IDs (so the most-recent
    resolution is authoritative even if an older commit also resolved
    the same ID)."""
    s = BugSummary()
    for sha, body in list_commits(repo):
        trailers = parse_commit_trailers(body)
        report_ids = trailers.get("Bug-Report", [])
        resolve_ids = trailers.get("Bug-Resolves", [])
        # Bug-Defer is parsed as a Bug-Resolves with status="deferred"
        # (no JSON block required; the trailer value is the bug id,
        # optional reason picked up from `## Bug-Defer` JSON if any).
        defer_ids = trailers.get("Bug-Defer", [])
        # Bug-Reproduce is a parallel signal: "this commit adds the
        # failing test for the bug, BEFORE the fix lands". Used by
        # TDD-style audits to enforce reproduce-then-fix ordering.
        #
        # Compatibility: early internal testing turns used `TDD: reproducer
        # for BUG-NNN` subjects before adding the explicit trailer.
        reproduce_ids = trailers.get("Bug-Reproduce", [])
        if not reproduce_ids:
            reproduce_ids = _historical_tdd_subject_reproduce_ids(body)
        _peers = trailers.get("Peer") or []
        peer_trailer = _peers[0] if _peers else None

        for bid in report_ids:
            report_json = _bug_report_block(body, bid)
            if bid in s.reports:
                # newest-first iteration → first wins. Surface duplicates
                # so operators can spot severity-downgrade-style gaming:
                # a later Bug-Report:<id> with weaker severity silently
                # overrides the earlier filing. Legitimate re-triage
                # (the ar BUG-003 case) still works — this is purely a
                # warning, not a refusal.
                #
                # To inspect what changed, look for the older commit
                # carrying the same Bug-Report:<id> trailer
                # (`git log --grep="Bug-Report: BUG-NNN"`).
                s.warnings.append(
                    f"{sha[:8]}: Bug-Report:{bid} duplicate filing — "
                    "newest commit wins (re-triage permitted; check "
                    "older commits for the original severity)"
                )
                if report_json is not None:
                    older_sev = _normalize_severity(
                        report_json.get("severity", "info")
                    )
                    current = s.reports[bid]
                    if _severity_is_higher(older_sev, current.severity):
                        s.warnings.append(
                            f"{sha[:8]}: Bug-Report:{bid} older duplicate "
                            f"has higher severity {older_sev!r}; keeping "
                            "highest severity for gate purposes"
                        )
                        current.severity = older_sev
                continue
            sev = "info"
            title = ""
            desc = ""
            fix_by = None
            location = None
            cwe = None
            file_name = None
            function = None
            if report_json is not None:
                sev = str(report_json.get("severity", "info")).lower()
                title = str(report_json.get("title", ""))
                desc = str(report_json.get("description", ""))
                fix_by = report_json.get("fix_by")
                location = report_json.get("location")
                cwe = report_json.get("cwe") or report_json.get("CWE")
                file_name = report_json.get("file")
                function = report_json.get("function")
            else:
                # Heading-less fallback: pull title from the subject line.
                subj = body.splitlines()[0] if body else ""
                title = subj.strip()
                s.warnings.append(
                    f"{sha[:8]}: Bug-Report:{bid} without parseable JSON "
                    "body; severity defaulted to 'info'"
                )
            if sev not in SEVERITY_ORDER:
                s.warnings.append(
                    f"{sha[:8]}: Bug-Report:{bid} severity={sev!r} "
                    "unknown; demoted to 'info'"
                )
                sev = "info"
            s.reports[bid] = BugReport(
                id=bid, severity=sev, title=title, description=desc,
                fix_by=str(fix_by) if fix_by else None,
                location=str(location) if location else None,
                cwe=str(cwe) if cwe else None,
                file=str(file_name) if file_name else (
                    str(location).split(":", 1)[0] if location else None
                ),
                function=str(function) if function else None,
                sha=sha, found_by=peer_trailer,
            )

        for bid in resolve_ids:
            if bid in s.resolutions:
                # docstring promised a
                # warning here but the code silently dropped duplicate
                # resolutions. Surface the conflict so operators can
                # see when a peer changes their mind about a fix.
                s.warnings.append(
                    f"{sha[:8]}: Bug-Resolves:{bid} duplicate — "
                    "newest commit wins; older resolution status kept "
                    "for forensics in git log"
                )
                continue
            if bid in s.reports and s.reports[bid].sha != sha:
                s.warnings.append(
                    f"{sha[:8]}: Bug-Resolves:{bid} is older than the "
                    "newest Bug-Report for the same id; treating the bug "
                    "as reopened"
                )
                continue
            status = "fixed"
            note = ""
            resolve_json = _resolution_block(body, bid, "resolves", "id")
            if resolve_json is not None:
                status = str(resolve_json.get("status", "fixed")).lower()
                note = str(resolve_json.get("note", ""))
            if status not in RESOLVED_STATUSES:
                s.warnings.append(
                    f"{sha[:8]}: Bug-Resolves:{bid} status={status!r} "
                    "unknown; treating as 'invalid' (still counts as open)"
                )
                status = "invalid"
            s.resolutions[bid] = BugResolution(
                id=bid, status=status, note=note,
                sha=sha, resolved_by=peer_trailer,
            )

        for bid in defer_ids:
            if bid in s.resolutions:
                s.warnings.append(
                    f"{sha[:8]}: Bug-Defer:{bid} but bug already has a "
                    f"{s.resolutions[bid].status!r} resolution — "
                    "newest commit wins (defer ignored)"
                )
                continue
            if bid in s.reports and s.reports[bid].sha != sha:
                s.warnings.append(
                    f"{sha[:8]}: Bug-Defer:{bid} is older than the newest "
                    "Bug-Report for the same id; treating the bug as reopened"
                )
                continue
            note = ""
            defer_json = _resolution_block(body, bid, "defers", "id")
            if defer_json is not None:
                note = str(defer_json.get("reason")
                           or defer_json.get("note", ""))
            if not note:
                # Defer without rationale is gaming — surface it but
                # still honor the defer so the gate can complete (the
                # warning is the audit trail).
                s.warnings.append(
                    f"{sha[:8]}: Bug-Defer:{bid} without a `reason`/`note` "
                    "in the JSON block — please justify defers explicitly"
                )
            s.resolutions[bid] = BugResolution(
                id=bid, status="deferred", note=note,
                sha=sha, resolved_by=peer_trailer,
            )

        for bid in reproduce_ids:
            # Multiple reproduce commits per bug are fine and even
            # encouraged (happy + edge + sad as three commits).
            s.reproductions.setdefault(bid, []).append(BugReproduction(
                id=bid, sha=sha, reproduced_by=peer_trailer,
            ))

    return s


def format_summary(s: BugSummary) -> str:
    """Human-readable rollup for `peers bug-hunt status` and the
    hard-gate diagnostic. Compact: one line per blocking-severity open
    bug, plus a 1-line tally."""
    lines: list[str] = []
    open_by = s.open_by_severity
    for sev in SEVERITY_ORDER:
        if sev not in BLOCKING_SEVERITIES:
            continue
        for rep in open_by[sev]:
            who = rep.fix_by or rep.found_by or "?"
            loc = rep.location or "?"
            lines.append(f"  [{sev}] {rep.id} ({rep.sha[:8]}, fix_by={who}, "
                         f"@ {loc}): {rep.title or rep.description[:60]}")
    tally = " ".join(
        f"{sev}={len(open_by[sev])}" for sev in SEVERITY_ORDER
    )
    head = (
        f"bug-hunt: {s.open_blocking_count} blocking open ({tally}); "
        f"{len(s.reports)} total reported, "
        f"{sum(1 for r in s.resolutions.values() if r.status == 'fixed')} fixed"
        f", {s.deferred_count} deferred"
        f", {s.reproduced_count} have reproduce-tests"
    )
    if not lines:
        return head
    return head + "\n" + "\n".join(lines)


def summary_dict(repo: Path) -> dict:
    """JSON-serializable bug-hunt summary for downstream tooling."""
    s = summarize(repo)
    by_severity = {sev: len(items) for sev, items in s.open_by_severity.items()}
    by_cwe: dict[str, int] = {}
    reports = []
    for rep in sorted(s.reports.values(), key=lambda r: r.id):
        if rep.cwe:
            by_cwe[rep.cwe] = by_cwe.get(rep.cwe, 0) + 1
        res = s.resolutions.get(rep.id)
        reports.append({
            "id": rep.id,
            "severity": rep.severity,
            "title": rep.title,
            "description": rep.description,
            "fix_by": rep.fix_by,
            "location": rep.location,
            "file": rep.file,
            "function": rep.function,
            "cwe": rep.cwe,
            "sha": rep.sha,
            "found_by": rep.found_by,
            "status": res.status if res else "open",
            "resolution_sha": res.sha if res else None,
            "resolved_by": res.resolved_by if res else None,
            "reproduced": rep.id in s.reproductions,
        })
    return {
        "total": len(s.reports),
        "open_blocking": s.open_blocking_count,
        "deferred": s.deferred_count,
        "reproduced": s.reproduced_count,
        "by_severity": by_severity,
        "by_cwe": by_cwe,
        "reports": reports,
        "warnings": list(s.warnings),
    }


def summary_json(repo: Path) -> str:
    return json.dumps(summary_dict(repo), indent=2, sort_keys=True)


def gate_pass(repo: Path) -> tuple[bool, str]:
    """Helper for the `bug-hunt-clean` hard gate. Returns
    (pass?, diagnostic). pass iff no blocking-severity bugs are open.
    `deferred` bugs do NOT block (see `BugResolution.status`)."""
    s = summarize(repo)
    return s.is_clean(), format_summary(s)


_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?/|test_[^/]+\.py$|[^/]+_test\.py$|"
    r"[^/]*\.test\.[a-z0-9]+$)"
)


def _commit_touches_test_path(repo: Path, sha: str) -> bool:
    """BUG-161 helper: True iff `sha` touched at least one path that
    looks like a test artifact. Used to reject trailer-only reproduce
    commits that carry no test evidence (empty diff or only doc/src
    changes)."""
    try:
        out = _git(repo, "show", "--name-only", "--format=", sha)
    except subprocess.CalledProcessError:
        return False
    for line in out.splitlines():
        line = line.strip()
        if line and _TEST_PATH_RE.search(line):
            return True
    return False


def _json_mentions_bug(value: object, bid: str) -> bool:
    if value == bid:
        return True
    if isinstance(value, list):
        return bid in {str(v) for v in value}
    return False


def _tdd_waivers(repo: Path) -> dict[str, tuple[str, str]]:
    """Return explicit historical TDD waivers by bug id.

    A waiver is intentionally not a bare trailer loophole: it must carry a
    Peer trailer and a JSON block whose id/ids/waives field names the bug,
    plus a substantive reason. Newest valid waiver wins.
    """
    out: dict[str, tuple[str, str]] = {}
    for sha, body in list_commits(repo):
        trailers = parse_commit_trailers(body)
        bids = trailers.get("Bug-Reproduce-Waive", [])
        if not bids or not trailers.get("Peer"):
            continue
        json_block = _first_json_block(body)
        if not isinstance(json_block, dict):
            continue
        reason = str(json_block.get("reason") or json_block.get("note") or "")
        if len(reason.strip()) < _TDD_WAIVER_REASON_MIN:
            continue
        for bid in bids:
            if not (
                _json_mentions_bug(json_block.get("id"), bid)
                or _json_mentions_bug(json_block.get("ids"), bid)
                or _json_mentions_bug(json_block.get("waives"), bid)
            ):
                continue
            out.setdefault(bid, (sha, reason))
    return out


def gate_tdd_pass(repo: Path) -> tuple[bool, str]:
    """Helper for the optional `tdd-reproduces-bug` hard gate. Returns
    (pass?, diagnostic). Pass iff every Bug-Resolves with status=fixed
    at a blocking severity has at least one Bug-Reproduce commit that

      (a) landed BEFORE the resolve (the test was committed first), AND
      (b) actually touched a test path: trailer-only commits
          with no diff, or commits that only changed src/docs, do not
          count as evidence that a failing test was added.

    Bugs deferred / wontfix / duplicate / invalid are not subject to
    this check — they were never fixed, so there's no fix to reproduce.

    "Before" is checked by commit order in `git log HEAD` (newest-first
    iteration in `list_commits`), which corresponds to topological-
    enough ordering for the linear histories peers produce. Branchy
    histories where a reproduce commit lives on a side branch that was
    merged after the resolve will fail this gate — and that's the
    correct behavior: if the test wasn't on `main` at the time of fix,
    the discipline wasn't followed.

    BUG-182: existing ledgers may carry historical fixes where the
    reproduce-first evidence cannot be reordered into place without a
    destructive history rewrite. Those require an explicit
    Bug-Reproduce-Waive trailer with a substantive reason, and the waiver
    must land after the fix it documents. Future fixes cannot be
    pre-waived.
    """
    s = summarize(repo)
    missing: list[str] = []
    waived: list[str] = []
    # Build a sha→index map for "before" comparison. Newest = index 0.
    # `list_commits` is newest-first, so a commit's index is its
    # distance from HEAD. Lower index = NEWER.
    order = {sha: i for i, (sha, _body) in enumerate(list_commits(repo))}
    waivers = _tdd_waivers(repo)
    for bid, rep in s.reports.items():
        if rep.severity not in BLOCKING_SEVERITIES:
            continue
        res = s.resolutions.get(bid)
        if res is None or res.status != "fixed":
            continue
        resolve_idx = order.get(res.sha, -1)
        repros = s.reproductions.get(bid, [])
        # a Bug-Reproduce trailer is only credible when the
        # commit it lives on actually touched a test path. Otherwise a
        # peer can satisfy the gate with `git commit --allow-empty`.
        evident = [r for r in repros if _commit_touches_test_path(repo, r.sha)]
        if not evident:
            waiver = waivers.get(bid)
            if not repros and waiver is not None:
                waiver_sha, _reason = waiver
                waiver_idx = order.get(waiver_sha, -1)
                if waiver_idx >= 0 and resolve_idx > waiver_idx:
                    waived.append(bid)
                    continue
            if repros:
                missing.append(
                    f"  {bid} ({rep.severity}, fixed in {res.sha[:8]}): "
                    "Bug-Reproduce commit(s) carry no test-path evidence "
                    "(trailer-only, empty, or only touch non-test paths)"
                )
            else:
                missing.append(
                    f"  {bid} ({rep.severity}, fixed in {res.sha[:8]}): "
                    "no Bug-Reproduce commit found"
            )
            continue
        # The earliest reproduce must be older (= higher index in our
        # newest-first iteration) than the resolve.
        repro_indices = [order.get(r.sha, -1) for r in evident]
        earliest_repro = max(repro_indices) if repro_indices else -1
        if earliest_repro <= resolve_idx:
            shas = ",".join(r.sha[:8] for r in evident)
            missing.append(
                f"  {bid} ({rep.severity}, fixed in {res.sha[:8]}): "
                f"reproduce commit(s) [{shas}] are not OLDER than the "
                "resolve — TDD-order broken (test landed with/after fix)"
            )
    waived_note = (
        "; waived historical fix(es): " + ", ".join(sorted(waived))
        if waived else
        ""
    )
    head = (
        f"tdd-gate: {len(missing)} fix(es) without preceding reproduce"
        if missing else
        "tdd-gate: clean "
        f"({s.reproduced_count} reproduced, {len(waived)} waived, "
        f"{sum(1 for r in s.resolutions.values() if r.status=='fixed')} fixed)"
    )
    head += waived_note
    if missing:
        return False, head + "\n" + "\n".join(missing)
    return True, head


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint exposed as `python -m peers.bug_hunt`.

    Subcommands:
      summary  [path]   — print a markdown rollup (default: cwd).
      gate     [path]   — print rollup; exit 0 iff no blocking-severity
                          bug is open (deferred does NOT block).
      gate-tdd [path]   — exit 0 iff every blocking fix has at least one
                          preceding `Bug-Reproduce:` commit (or older
                          `TDD: reproducer for BUG-NNN` subject); for
                          TDD-style audits that require the test to be
                          committed BEFORE the fix.
    """
    import argparse
    p = argparse.ArgumentParser(prog="peers-bug-hunt")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("summary", "gate", "gate-tdd"):
        s = sub.add_parser(name)
        s.add_argument("path", nargs="?", default=".")
        if name == "summary":
            s.add_argument("--format", choices=("text", "json"),
                           default="text")
    args = p.parse_args(argv)
    repo = Path(args.path).resolve()
    if args.cmd == "summary":
        if args.format == "json":
            print(summary_json(repo))
        else:
            print(format_summary(summarize(repo)))
        return 0
    if args.cmd == "gate":
        ok, diag = gate_pass(repo)
        print(diag)
        return 0 if ok else 1
    if args.cmd == "gate-tdd":
        ok, diag = gate_tdd_pass(repo)
        print(diag)
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
