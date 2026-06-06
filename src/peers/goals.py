"""Goal model and pass_when DSL evaluator.

The DSL is a deliberately tiny subset: only a fixed set of names and
function calls are reachable. Arbitrary Python is rejected.
"""
from __future__ import annotations

import ast
import json
import re
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from peers.safe_io import (
    read_bytes_no_symlink,
    read_text_under_root_no_follow,
)


VALID_REVIEWER_MODES = ("other", "both", "alternating", "quorum")
_GOALS_YAML_MAX_BYTES = 2 * 1024 * 1024


@dataclass
class Goal:
    id: str
    type: str
    pass_when: str | None = None
    cmd: str | None = None
    description: str | None = None
    prompt: str | None = None
    reviewer: str | None = None
    consensus_needed: int = 2
    review_interval: int = 5
    # Quorum parameters (only meaningful when reviewer == "quorum").
    # `quorum: "2/3"` in YAML becomes (2, 3) at load time.
    quorum_num: int | None = None
    quorum_den: int | None = None
    # Per-goal subprocess deadline that overrides the engine-wide
    # default. `None` means inherit `GoalEngine.timeout_s`. Documented
    # in templates as `Goal.timeout_s`; before BUG-146 the field was
    # missing so the override was silently dropped.
    timeout_s: int | None = None


def _parse_quorum(raw: Any, gid: str) -> tuple[int | None, int | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        raise ValueError(
            f"goal {gid}: quorum must be a string like '2/3'"
        )
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", raw)
    if not m:
        raise ValueError(
            f"goal {gid}: quorum must be 'N/M' integers, got {raw!r}"
        )
    num = int(m.group(1))
    den = int(m.group(2))
    if den <= 0 or num <= 0 or num > den:
        raise ValueError(
            f"goal {gid}: quorum N/M must satisfy 0 < N <= M, got {num}/{den}"
        )
    return num, den


def _positive_int(raw: Any, field: str, gid: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(
            f"goal {gid}: {field} must be a positive integer, "
            f"got {type(raw).__name__} ({raw!r})"
        )
    if raw <= 0:
        raise ValueError(
            f"goal {gid}: {field} must be a positive integer, got {raw}"
        )
    return raw


def load_goals(path: Path) -> list[Goal]:
    try:
        data = read_bytes_no_symlink(
            Path(path), max_bytes=_GOALS_YAML_MAX_BYTES + 1
        )
        if len(data) > _GOALS_YAML_MAX_BYTES:
            raise ValueError(
                f"goals.yaml: file too large (max {_GOALS_YAML_MAX_BYTES} bytes)"
            )
        raw = yaml.safe_load(data.decode("utf-8", errors="replace"))
    except yaml.YAMLError as e:
        raise ValueError(f"goals.yaml: invalid YAML: {e}") from e
    if raw is None:
        raw = {"goals": []}
    if not isinstance(raw, dict):
        raise ValueError(
            f"goals.yaml: top-level value must be a mapping, got "
            f"{type(raw).__name__}"
        )
    raw_goals = raw.get("goals", [])
    if raw_goals is None:
        raw_goals = []
    if not isinstance(raw_goals, list):
        raise ValueError(
            f"goals.yaml: `goals` must be a list, got "
            f"{type(raw_goals).__name__}"
        )
    out: list[Goal] = []
    seen_ids: set[str] = set()
    for idx, entry in enumerate(raw_goals):
        if not isinstance(entry, dict):
            raise ValueError(
                f"goals.yaml: goals[{idx}] must be a mapping, got "
                f"{type(entry).__name__}"
            )
        # M2: reject duplicate goal IDs at load time; downstream
        # consumers (goals_status, stuck_counter) would silently
        # overwrite otherwise.
        eid = entry.get("id")
        if not isinstance(eid, str) or not eid:
            raise ValueError(
                f"goals.yaml: goals[{idx}].id must be a non-empty string"
            )
        if eid in seen_ids:
            raise ValueError(
                f"goals.yaml: duplicate goal id {eid!r} — IDs must "
                "be unique within a single goals.yaml"
            )
        seen_ids.add(eid)
        gid = eid
        gtype = entry.get("type")
        if not isinstance(gtype, str) or not gtype:
            raise ValueError(
                f"goal {gid}: type must be 'hard' or 'soft', got {gtype!r}"
            )
        consensus_needed = _positive_int(
            entry.get("consensus_needed", 2), "consensus_needed", gid
        )
        review_interval = _positive_int(
            entry.get("review_interval", 5), "review_interval", gid
        )
        quorum_num, quorum_den = _parse_quorum(entry.get("quorum"), gid)
        timeout_s_raw = entry.get("timeout_s")
        if timeout_s_raw is None:
            timeout_s: int | None = None
        else:
            timeout_s = _positive_int(timeout_s_raw, "timeout_s", gid)
        reviewer = entry.get("reviewer")
        if reviewer is not None and reviewer not in VALID_REVIEWER_MODES:
            raise ValueError(
                f"goal {gid}: reviewer must be one of "
                f"{VALID_REVIEWER_MODES}, got {reviewer!r}"
            )
        if reviewer == "quorum" and (quorum_num is None or quorum_den is None):
            raise ValueError(
                f"goal {gid}: reviewer=quorum requires `quorum: 'N/M'`"
            )
        g = Goal(
            id=gid,
            type=gtype,
            pass_when=entry.get("pass_when"),
            cmd=entry.get("cmd"),
            description=entry.get("description"),
            prompt=entry.get("prompt"),
            reviewer=reviewer,
            consensus_needed=consensus_needed,
            review_interval=review_interval,
            quorum_num=quorum_num,
            quorum_den=quorum_den,
            timeout_s=timeout_s,
        )
        if g.type == "hard":
            if not isinstance(g.cmd, str) or not g.cmd.strip():
                raise ValueError(
                    f"goal {g.id}: hard goals require non-empty string `cmd`"
                )
            if not isinstance(g.pass_when, str) or not g.pass_when.strip():
                raise ValueError(
                    f"goal {g.id}: hard goals require non-empty string "
                    "`pass_when`"
                )
            # (post-shakedown): catch DSL syntax errors at
            # load time, not at the first tick. Before this, a typo
            # like `splitlines()[-1]` only surfaced after a peer had
            # already run and burned $$ — the goal would then fail
            # with "pass_when error: node type not allowed: Subscript".
            try:
                _validate_pass_when_at_load(g.pass_when)
            except ValueError as e:
                raise ValueError(
                    f"goal {g.id}: pass_when DSL invalid: {e}"
                ) from e
        elif g.type == "soft":
            if not isinstance(g.prompt, str) or not g.prompt.strip():
                raise ValueError(
                    f"goal {g.id}: soft goals require non-empty string "
                    "`prompt`"
                )
            if not isinstance(g.reviewer, str) or not g.reviewer:
                raise ValueError(
                    f"goal {g.id}: soft goals require `reviewer`"
                )
        else:
            raise ValueError(
                f"goal {g.id}: type must be 'hard' or 'soft', got {g.type!r}"
            )
        out.append(g)
    return out


# --- DSL ---------------------------------------------------------------

_ALLOWED_NAMES = {
    "exit_code", "stdout", "stderr", "cwd",
    "regex", "json", "int", "float", "len",
    "None", "True", "False",
}

# Cap on stdout/stderr length exposed to the DSL. Prevents a malicious
# or buggy `regex(...)` with catastrophic backtracking from hanging the
# loop on multi-MB output.
_MAX_DSL_INPUT_BYTES = 1 * 1024 * 1024  # 1 MiB
_MAX_DSL_JSON_BYTES = 2 * 1024 * 1024
_MAX_DSL_LITERAL_BYTES = 8192
_MAX_REGEX_PATTERN_BYTES = 1024
_DSL_REGEX_TIMEOUT_S = 0.25

# Methods we accept being called on whitelisted names (e.g. stdout.strip()).
_ALLOWED_METHODS = {
    "strip", "rstrip", "lstrip", "lower", "upper",
    "split", "splitlines", "startswith", "endswith",
}
_METHOD_ARITY = {
    "strip": (0, 1),
    "rstrip": (0, 1),
    "lstrip": (0, 1),
    "lower": (0, 0),
    "upper": (0, 0),
    "split": (0, 1),
    "splitlines": (0, 0),
    "startswith": (1, 1),
    "endswith": (1, 1),
}


class _JsonView:
    """Wraps a dict so `view.key.sub.path` works in the DSL."""

    def __init__(self, data: Any) -> None:
        self._data = data

    def __getattr__(self, name: str) -> Any:
        if isinstance(self._data, dict) and name in self._data:
            val = self._data[name]
            return _JsonView(val) if isinstance(val, (dict, list)) else val
        raise AttributeError(name)

    def __ge__(self, other: Any) -> bool:
        return self._data >= other

    def __gt__(self, other: Any) -> bool:
        return self._data > other

    def __le__(self, other: Any) -> bool:
        return self._data <= other

    def __lt__(self, other: Any) -> bool:
        return self._data < other

    def __eq__(self, other: Any) -> bool:
        return self._data == other

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self):
        return iter(self._data)

    __hash__ = None  # type: ignore[assignment]  # explicit: not hashable


class _RegexTimeout(Exception):
    def __init__(self) -> None:
        super().__init__("regex evaluation timed out")


def _safe_regex_search(pattern: str, text: str) -> re.Match | None:
    if len(pattern.encode("utf-8", errors="replace")) > _MAX_REGEX_PATTERN_BYTES:
        raise ValueError(
            f"regex() pattern too large "
            f"(>{_MAX_REGEX_PATTERN_BYTES} bytes)"
        )
    if not hasattr(signal, "setitimer"):
        return re.search(pattern, text)

    def _raise_timeout(_signum, _frame):
        raise _RegexTimeout()

    previous_handler = None
    handler_captured = False
    previous_timer = None
    try:
        previous_handler = signal.getsignal(signal.SIGALRM)
        handler_captured = True
        signal.signal(signal.SIGALRM, _raise_timeout)
        previous_timer = signal.setitimer(
            signal.ITIMER_REAL, _DSL_REGEX_TIMEOUT_S
        )
    except (ValueError, AttributeError):
        # Signals only work in the main thread. The goal engine is
        # single-threaded, but keep this fallback for embedders.
        # if signal.signal succeeded before setitimer raised,
        # the local _raise_timeout closure would otherwise leak into
        # the next SIGALRM. Roll it back before falling through.
        if handler_captured:
            try:
                signal.signal(signal.SIGALRM, previous_handler)
            except (ValueError, AttributeError):
                pass
        return re.search(pattern, text)
    try:
        return re.search(pattern, text)
    except _RegexTimeout as e:
        raise ValueError(
            f"regex() timed out after {_DSL_REGEX_TIMEOUT_S:.2f}s"
        ) from e
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if handler_captured:
            signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer is not None and previous_timer[0] > 0:
            signal.setitimer(
                signal.ITIMER_REAL, previous_timer[0], previous_timer[1]
            )


def _make_env(ctx: dict[str, Any]) -> dict[str, Any]:
    cwd: Path = ctx["cwd"]
    cwd_resolved = cwd.resolve()

    def regex_fn(pattern: str, text: str) -> re.Match | None:
        return _safe_regex_search(pattern, text)

    def json_fn(rel_path: str) -> _JsonView:
        # Security: clamp json() to files inside the target repo. An
        # attacker-influenced pass_when otherwise reads /etc/passwd or
        # any other JSON-shaped file on the host.
        rel = Path(rel_path)
        if rel.is_absolute():
            raise ValueError(
                f"json() requires a relative path, got {rel_path!r}"
            )
        # read_text_no_symlink only protects the leaf — the
        # kernel still resolves every parent. Reject ``..``/empty parts
        # and then walk each ancestor with O_NOFOLLOW so a same-user
        # symlink swap on any intermediate dir cannot redirect the read
        # outside ``cwd_resolved``.
        parts = tuple(rel.parts)
        if not parts or any(p in ("", ".", "..") for p in parts):
            raise ValueError(
                f"json() rejects path with empty or parent components: "
                f"{rel_path!r}"
            )
        try:
            raw = read_text_under_root_no_follow(
                cwd_resolved, parts, max_bytes=_MAX_DSL_JSON_BYTES + 1,
            )
        except OSError:
            raise
        if len(raw.encode("utf-8", errors="replace")) > _MAX_DSL_JSON_BYTES:
            raise ValueError(
                f"json() file too large (>{_MAX_DSL_JSON_BYTES} bytes): "
                f"{rel_path!r}"
            )
        return _JsonView(json.loads(raw))

    return {
        "exit_code": ctx["exit_code"],
        "stdout": ctx["stdout"][:_MAX_DSL_INPUT_BYTES],
        "stderr": ctx["stderr"][:_MAX_DSL_INPUT_BYTES],
        "cwd": cwd,
        "regex": regex_fn,
        "json": json_fn,
        "int": int,
        "float": float,
        "len": len,
        "None": None,
        "True": True,
        "False": False,
    }


_FORBIDDEN_NODE_TYPES = (
    ast.Import, ast.ImportFrom,
    ast.Subscript,
    ast.List, ast.Tuple, ast.Set, ast.Dict,
    ast.Lambda,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
    ast.Starred,
    ast.IfExp,
    ast.Yield, ast.YieldFrom, ast.Await,
    ast.NamedExpr,
    ast.FormattedValue, ast.JoinedStr,
)


def _validate_call_arity(name: str, n_args: int, n_kwargs: int) -> None:
    if n_kwargs:
        raise ValueError(f"{name}() does not accept keyword arguments")
    arity = {
        "regex": (2, 2),
        "json": (1, 1),
        "int": (1, 1),
        "float": (1, 1),
        "len": (1, 1),
    }.get(name)
    if arity is None:
        arity = _METHOD_ARITY.get(name)
    if arity is None:
        return
    lo, hi = arity
    if not lo <= n_args <= hi:
        if lo == hi:
            raise ValueError(f"{name}() requires {lo} argument(s)")
        raise ValueError(f"{name}() requires between {lo} and {hi} arguments")


def _validate_ast(node: ast.AST) -> None:
    for child in ast.walk(node):
        if isinstance(child, _FORBIDDEN_NODE_TYPES):
            raise ValueError(f"node type not allowed: {type(child).__name__}")
        if isinstance(child, ast.BinOp) and not isinstance(child.op, ast.Add):
            raise ValueError(
                f"operator not allowed: {type(child.op).__name__}"
            )
        if isinstance(child, ast.UnaryOp) and not isinstance(child.op, ast.Not):
            raise ValueError(
                f"unary operator not allowed: {type(child.op).__name__}"
            )
        if isinstance(child, ast.Constant):
            val = child.value
            if isinstance(val, str):
                literal_size = len(val.encode("utf-8", errors="replace"))
                if literal_size > _MAX_DSL_LITERAL_BYTES:
                    raise ValueError(
                        f"string literal too large "
                        f"(>{_MAX_DSL_LITERAL_BYTES} bytes)"
                    )
            elif isinstance(val, int) and not isinstance(val, bool):
                if len(str(abs(val))) > 18:
                    raise ValueError("integer literal too large")
            elif not isinstance(val, (bool, float, type(None))):
                raise ValueError(
                    f"literal type not allowed: {type(val).__name__}"
                )
        if isinstance(child, ast.Attribute):
            # 1. No dunder access — blocks __class__, __subclasses__, etc.
            if child.attr.startswith("_"):
                raise ValueError(f"private attribute access: .{child.attr}")
            # 2. The chain must root at either a whitelisted Name
            #    (e.g. stdout.strip()) or a whitelisted Call
            #    (e.g. json('x.json').totals — the Call's own func is
            #    re-validated by the Call branch below).
            root = child
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                if root.id not in _ALLOWED_NAMES:
                    raise ValueError(f"disallowed name: {root.id}")
            elif isinstance(root, ast.Call):
                func = root.func
                if not (isinstance(func, ast.Name)
                        and func.id in _ALLOWED_NAMES):
                    raise ValueError(
                        "attribute chain rooted at non-whitelisted call"
                    )
            else:
                raise ValueError(
                    "attribute access not allowed on "
                    f"{type(root).__name__}"
                )
        elif isinstance(child, ast.Name):
            if child.id not in _ALLOWED_NAMES:
                raise ValueError(f"disallowed name: {child.id}")
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                if func.id not in _ALLOWED_NAMES:
                    raise ValueError(f"disallowed call: {func.id}")
                _validate_call_arity(
                    func.id, len(child.args), len(child.keywords)
                )
            elif isinstance(func, ast.Attribute):
                # 3. Method calls on whitelisted names: the method name
                #    must be in _ALLOWED_METHODS (Attribute body already
                #    enforces the root-name whitelist above).
                if func.attr not in _ALLOWED_METHODS:
                    raise ValueError(f"disallowed method: .{func.attr}()")
                _validate_call_arity(
                    func.attr, len(child.args), len(child.keywords)
                )
            else:
                raise ValueError(
                    f"call on unsupported expression: {type(func).__name__}"
                )


def _validate_pass_when_at_load(expr: str) -> None:
    """Parse + AST-validate a `pass_when` expression without evaluating
    it. Raises ValueError on any DSL violation (forbidden node type,
    disallowed name/call, private attribute, etc.). Lets `load_goals`
    fail loud at `peers init` / `peers info` / `peers run` start
    instead of during the first tick after the user has paid for it.
    """
    cleaned = re.sub(r"\s*\n\s*", " ", expr).strip()
    try:
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"syntax error: {e.msg}") from e
    _validate_ast(tree)


def evaluate_pass_when(expr: str, ctx: dict[str, Any]) -> bool:
    # Tolerate YAML literal-block formatting: a `pass_when: |` block
    # with continuation lines indented by YAML produces a string like
    # "regex(...) == None\n  and regex(...) != None\n", which trips
    # Python's ast.parse with "unexpected indent". Collapse line
    # continuations into single spaces so the expression is one line.
    # (Our DSL has no multi-line string constants — `Constant` is
    # always a single token — so this is safe.)
    cleaned = re.sub(r"\s*\n\s*", " ", expr).strip()
    try:
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"syntax error: {e.msg}") from e
    _validate_ast(tree)
    env = _make_env(ctx)
    # The expression has already passed the DSL AST whitelist above and
    # receives an empty builtins table; eval is the tiny evaluator here.
    result = eval(  # nosec B307
        compile(tree, "<pass_when>", "eval"),
        {"__builtins__": {}},
        env,
    )
    # Reject non-(bool|int|float|None) — catches the bare-method-attribute
    # foot-gun (`pass_when: stdout.strip` evaluates to a bound method
    # which is truthy and would always "pass").
    if not isinstance(result, (bool, int, float)) and result is not None:
        raise ValueError(
            f"pass_when must return bool/numeric/None, got "
            f"{type(result).__name__}: did you forget a comparison?"
        )
    return bool(result)
