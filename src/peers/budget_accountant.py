"""Budget accounting and token/cost parsers for peer runs."""
from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from peers.safe_io import read_bytes_under_root_no_follow


_CONFIGURABLE_BUDGET_LIMITS = (
    "max_iterations", "max_runtime_s", "max_consecutive_failures",
    "max_tokens", "max_usd", "max_usd_mode",
)
# cap the operator-override read so a same-UID swap to a huge file
# cannot exhaust memory in json.loads (defense-in-depth atop the BUG-513
# no-follow read). Operator overrides are a handful of numeric caps; 64 KiB
# is enormous headroom.
_BUDGET_OVERRIDE_MAX_BYTES = 64 * 1024

# Operator budget caps (`peers-ctl start --max-runtime ...`) are persisted
# here, in `.peers/`, so they survive `_apply_config_budget` — which
# re-overlays config.yaml's caps onto state.budget on EVERY loop start and
# would otherwise clobber the override. peers-ctl writes this sidecar;
# the orchestrator re-applies it right after the config overlay. Kept out
# of state.json on purpose so it also works before state.json exists (the
# first start of a freshly-init'd project).
OPERATOR_BUDGET_OVERRIDE_FILE = "budget-overrides.json"


def _nonnegative_int(value: object) -> int:
    """Return a trusted non-negative integer counter, or 0."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _nonnegative_float(value: object) -> float:
    """Return a trusted finite non-negative float counter, or 0.0."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        amount = float(value)
        if math.isfinite(amount) and amount >= 0.0:
            return amount
    return 0.0


def _parse_codex_json_usage(text: str) -> int | None:
    """Sum input+output tokens across `turn.completed` events of a codex
    --json (JSONL) stream. Returns None if no such event is present (so the
    caller can fall back to the legacy text scrape). Verified against
    codex-cli 0.133: `{"type":"turn.completed","usage":{"input_tokens":N,
    "output_tokens":M}}`."""
    total: int | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{") or "turn.completed" not in line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "turn.completed":
            continue
        usage = obj.get("usage")
        if not isinstance(usage, dict):
            continue
        s = 0
        for key in ("input_tokens", "output_tokens"):
            v = usage.get(key)
            s += _nonnegative_int(v)
        total = (total or 0) + s
    return total


def _parse_codex_tokens(text: str) -> tuple[int, float]:
    """Tokens for a codex run. Prefers the `turn.completed.usage` events of a
    `--json` stream (Option C); falls back to the legacy `tokens used\n<N>`
    text scrape for plain `codex exec`."""
    json_tok = _parse_codex_json_usage(text)
    if json_tok is not None:
        return json_tok, 0.0
    m = re.search(r"tokens used\s*\n\s*([\d,]+)", text)
    if not m:
        return 0, 0.0
    try:
        return int(m.group(1).replace(",", "")), 0.0
    except ValueError:
        return 0, 0.0


def _parse_claude_tokens(text: str) -> tuple[int, float]:
    """Parse Claude Code token and USD summaries.

    Default text-mode `claude -p` does not reliably emit token/cost
    accounting. JSON and stream-json result envelopes do, so prefer those
    and only fall back to strict summary banners.
    """
    tokens, usd = _parse_claude_json_envelope(text)
    if tokens or usd:
        return tokens, usd

    tokens = 0
    usd = 0.0
    for pat in (
        r"(?im)^\s*(\d[\d,]*)\s+tokens?\s+used\.?\s*$",
        r"(?im)^\s*(?:total\s+)?tokens?\s+used\s*[:=]\s*(\d[\d,]*)\s*$",
    ):
        m = re.search(pat, text)
        if m:
            try:
                tokens = int(m.group(1).replace(",", ""))
                break
            except ValueError:
                tokens = 0
    for pat in (
        r"(?im)^\s*(?:total\s+)?cost\s*[:=]\s*\$(\d+\.\d+)\s*$",
        r"(?im)^\s*\$(\d+\.\d+)\s+(?:total\s+)?cost\s*$",
    ):
        m = re.search(pat, text)
        if m:
            try:
                usd = float(m.group(1))
                break
            except ValueError:
                usd = 0.0
    return tokens, usd


def _parse_claude_json_envelope(text: str) -> tuple[int, float]:
    """Best-effort scan for a Claude json or stream-json result envelope.

    Returns (0, 0.0) when nothing usable is found. Never raises.
    """

    def _from_obj(obj: object) -> tuple[int, float] | None:
        if not isinstance(obj, dict):
            return None
        if "usage" not in obj and "total_cost_usd" not in obj:
            return None
        usage_raw = obj.get("usage")
        usage = usage_raw if isinstance(usage_raw, dict) else {}
        token_keys = (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        )
        tok = 0
        for key in token_keys:
            value = usage.get(key)
            tok += _nonnegative_int(value)
        cost = obj.get("total_cost_usd")
        usd = _nonnegative_float(cost)
        return tok, usd

    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed = _from_obj(obj)
        if parsed is not None:
            return parsed

    try:
        obj = json.loads(text.strip())
    except json.JSONDecodeError:
        obj = None
    parsed = _from_obj(obj) if obj is not None else None
    if parsed is not None:
        return parsed

    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            obj = None
        parsed = _from_obj(obj) if obj is not None else None
        if parsed is not None:
            return parsed
    return 0, 0.0


def _parse_opencode_tokens(text: str) -> tuple[int, float]:
    """Tokens + USD for an opencode `--format json` run: sum the `step-finish`
    part events' `tokens.total` and `cost` across all steps (verified against
    opencode 1.15.13: `{"type":"step_finish","part":{"type":"step-finish",
    "tokens":{"total":N,...},"cost":C}}`)."""
    total_tok = 0
    total_usd = 0.0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{") or "step-finish" not in line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(obj, dict):
            continue
        part = obj.get("part")
        if not isinstance(part, dict) or part.get("type") != "step-finish":
            continue
        tokens = part.get("tokens")
        if isinstance(tokens, dict):
            tot = tokens.get("total")
            if isinstance(tot, int):
                total_tok += _nonnegative_int(tot)
            else:
                for key in ("input", "output"):
                    v = tokens.get(key)
                    total_tok += _nonnegative_int(v)
        cost = part.get("cost")
        total_usd += _nonnegative_float(cost)
    return total_tok, total_usd


_TOKEN_PARSERS = {
    "claude": _parse_claude_tokens,
    "codex": _parse_codex_tokens,
    "opencode": _parse_opencode_tokens,
}


def _argv_emits_token_accounting(tool: str, argv: Sequence[str]) -> bool:
    """True when ``argv`` selects a structured output mode the matching parser
    can read tokens/cost from. The parsers (above) only account the JSON /
    stream-json envelopes; a plain text invocation yields (0, 0.0)."""
    def _switch_value_in(switch: str, allowed: set[str]) -> bool:
        for i, arg in enumerate(argv):
            if arg == switch and i + 1 < len(argv) and argv[i + 1] in allowed:
                return True
            if arg.startswith(switch + "=") and arg.split("=", 1)[1] in allowed:
                return True
        return False

    if tool == "claude":
        return _switch_value_in("--output-format", {"json", "stream-json"})
    if tool == "codex":
        return any(a == "--json" or a.startswith("--json=") for a in argv)
    if tool == "opencode":
        return _switch_value_in("--format", {"json"})
    # unknown tool: no parser, so we cannot judge — caller must not false-warn.
    return False


def budget_argv_warning(
    tool: str,
    argv: Sequence[str],
    *,
    max_tokens: int | None = None,
    max_usd: float | None = None,
) -> str | None:
    """BRAIN-09 preflight: warn when a budget cap is configured but the peer's
    ``argv`` cannot emit the token/cost accounting the cap is enforced from, so
    the cap would be silently inert. Returns a one-line warning, or ``None``
    when there is nothing to warn about.

    Stays silent for unknown tools (no parser to reason about) and when no cap
    is set (a 0/None cap means "unlimited" — nothing to enforce)."""
    has_cap_tokens = bool(max_tokens)  # 0/None -> unset
    has_cap_usd = bool(max_usd)
    if not (has_cap_tokens or has_cap_usd):
        return None
    if tool not in _TOKEN_PARSERS:
        return None
    if _argv_emits_token_accounting(tool, argv):
        return None
    caps = []
    if has_cap_tokens:
        caps.append("max_tokens")
    if has_cap_usd:
        caps.append("max_usd")
    hint = {
        "claude": "--output-format stream-json",
        "codex": "--json",
        "opencode": "--format json",
    }.get(tool, "a JSON output mode")
    return (
        f"budget.{'/'.join(caps)} is set but the argv emits no parseable "
        f"token/cost accounting (add {hint}); the cap will be silently "
        f"UNENFORCED with this argv."
    )


def budget_argv_warnings(
    specs: Sequence[Any],
    *,
    max_tokens: int | None = None,
    max_usd: float | None = None,
) -> list[str]:
    """Per-peer BRAIN-09 preflight warnings for a run's peer specs. Each spec
    is expected to expose ``name``, ``tool`` and ``argv``. Returns one
    ``peer '<name>': <warning>`` line per peer whose argv cannot account for
    the configured cap (empty when all are fine or no cap is set)."""
    out: list[str] = []
    for spec in specs:
        warn = budget_argv_warning(
            getattr(spec, "tool", ""),
            tuple(getattr(spec, "argv", ()) or ()),
            max_tokens=max_tokens,
            max_usd=max_usd,
        )
        if warn:
            out.append(f"peer '{getattr(spec, 'name', '<unknown>')}': {warn}")
    return out


class BudgetAccountant:
    """State-backed facade for budget accounting operations."""

    def __init__(
        self,
        state: dict[str, Any] | None = None,
        *,
        max_iterations: int = 200,
        max_runtime_s: int = 21600,
        max_consecutive_failures: int = 5,
        max_tokens: int | None = None,
        max_usd: float | None = None,
        max_usd_mode: str = "hard",
    ) -> None:
        if state is not None:
            self.state = state
            return
        self.state = {
            "iteration": 0,
            "budget": {
                "max_iterations": max_iterations,
                "max_runtime_s": max_runtime_s,
                "max_consecutive_failures": max_consecutive_failures,
                "max_tokens": max_tokens,
                "max_usd": max_usd,
                "max_usd_mode": max_usd_mode,
                "spent_iterations": 0,
                "spent_runtime_s": 0,
                "spent_tokens": 0,
                "spent_usd": 0.0,
                "consecutive_failures": 0,
            },
            "warnings": [],
        }

    def record_tick(
        self,
        *,
        tokens: int = 0,
        usd: float = 0.0,
        duration_s: int = 0,
        success: bool = True,
    ) -> None:
        budget = self.state["budget"]
        budget["spent_tokens"] = (
            _nonnegative_int(budget.get("spent_tokens", 0))
            + _nonnegative_int(tokens)
        )
        budget["spent_usd"] = (
            _nonnegative_float(budget.get("spent_usd", 0.0))
            + _nonnegative_float(usd)
        )
        record_tick_accounting(self.state, success, duration_s)

    def snapshot(self) -> dict[str, Any]:
        return dict(self.state["budget"])

    def reason(self) -> str | None:
        return BudgetCheck(self.state).reason()

    def is_exhausted(self, cap: str | None = None) -> bool:
        reason = self.reason()
        if cap is None:
            return reason is not None
        aliases = {
            "iterations": "max_iterations",
            "runtime": "max_runtime",
            "consecutive_failures": "max_consecutive_failures",
            "tokens": "max_tokens",
            "usd": "max_usd",
        }
        return reason == aliases.get(cap, cap)


def _apply_config_budget(
    state: dict[str, Any],
    cfg_budget: dict[str, Any],
    peer_tools: list[str] | None = None,
) -> None:
    """Overlay config.yaml budget limits onto state.budget."""
    from peers.billing import resolve_max_usd_mode

    for key in _CONFIGURABLE_BUDGET_LIMITS:
        if key in cfg_budget:
            state["budget"][key] = cfg_budget[key]
    declared = cfg_budget.get("max_usd_mode", "auto")
    mode, reason = resolve_max_usd_mode(declared, peer_tools or [])
    state["budget"]["max_usd_mode"] = mode
    state["budget"]["max_usd_mode_reason"] = reason


def read_operator_budget_overrides(repo: Path | str) -> dict[str, Any]:
    """Read operator cap overrides from `.peers/budget-overrides.json`.

    These are the explicit caps an operator passed to `peers-ctl start`
    (e.g. `--max-runtime 12h` -> ``{"max_runtime_s": 43200}``) and which
    MUST win over config.yaml. Returns ``{}`` when the file is absent,
    unreadable, or malformed. Only recognised numeric budget caps survive;
    unknown keys and bool values (an int subclass — a silent footgun) are
    dropped, and ``max_usd_mode`` (a string knob, not an operator cap) is
    excluded.
    """
    # read via safe_io so a same-UID symlink swap of the leaf
    # OR the .peers ancestor cannot redirect this open to attacker-staged
    # JSON. The controller-side write was hardened by BUG-238; this is the
    # symmetric peers-side hardening.
    try:
        raw = read_bytes_under_root_no_follow(
            Path(repo), (".peers", OPERATOR_BUDGET_OVERRIDE_FILE),
            max_bytes=_BUDGET_OVERRIDE_MAX_BYTES + 1,
        )
        # reject wholesale rather than json-parse a truncated prefix.
        if len(raw) > _BUDGET_OVERRIDE_MAX_BYTES:
            return {}
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if (key in _CONFIGURABLE_BUDGET_LIMITS and key != "max_usd_mode"
                and isinstance(value, (int, float))
                and not isinstance(value, bool)):
            out[key] = value
    return out


def apply_operator_budget_overrides(
    state: dict[str, Any], repo: Path | str,
) -> dict[str, Any]:
    """Re-apply operator cap overrides ON TOP of the config overlay so the
    operator's explicit `--max-runtime` (etc.) wins over config.yaml.

    Call this immediately after :func:`_apply_config_budget`. Returns the
    overrides that were applied (for logging / tests)."""
    overrides = read_operator_budget_overrides(repo)
    budget = state.setdefault("budget", {})
    for key, value in overrides.items():
        budget[key] = value
    return overrides


class BudgetCheck:
    """Tick-precondition budget gate."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.b = state["budget"]

    def reason(self) -> str | None:
        if self.b["spent_iterations"] >= self.b["max_iterations"]:
            return "max_iterations"
        if self.b["spent_runtime_s"] >= self.b["max_runtime_s"]:
            return "max_runtime"
        if self.b["consecutive_failures"] >= self.b["max_consecutive_failures"]:
            return "max_consecutive_failures"
        max_tokens = self.b.get("max_tokens")
        if max_tokens is not None and self.b.get("spent_tokens", 0) >= max_tokens:
            return "max_tokens"
        max_usd = self.b.get("max_usd")
        if max_usd is not None and self.b.get("spent_usd", 0.0) >= max_usd:
            mode = self.b.get("max_usd_mode", "hard")
            if mode == "hard":
                return "max_usd"
            if mode == "warn":
                warned_for = self.b.get("max_usd_warned_for")
                if warned_for != max_usd:
                    _warn_once(
                        self.state,
                        f"max_usd:{max_usd:.2f} reached "
                        f"(spent ${self.b['spent_usd']:.2f}) "
                        "but mode=warn - loop continues",
                    )
                    self.b["max_usd_warned_for"] = max_usd
        return None


def _warn_once(state: dict[str, Any], msg: str) -> None:
    """Append `msg` to state.warnings unless the latest warning matches."""
    warnings = state.setdefault("warnings", [])
    if warnings and warnings[-1] == msg:
        return
    warnings.append(msg)


def account_tokens_usd(
    state: dict[str, Any], tool: str, run: Any,
) -> tuple[int, float]:
    """Parse and add token/USD usage for a completed peer run."""
    parser = _TOKEN_PARSERS.get(tool)
    if parser is None:
        return 0, 0.0
    tokens, usd = parser(run.stdout + run.stderr)
    tokens = _nonnegative_int(tokens)
    usd = _nonnegative_float(usd)
    state["budget"]["spent_tokens"] = (
        _nonnegative_int(state["budget"].get("spent_tokens", 0)) + tokens
    )
    state["budget"]["spent_usd"] = (
        _nonnegative_float(state["budget"].get("spent_usd", 0.0)) + usd
    )
    return tokens, usd


def record_tick_accounting(
    state: dict[str, Any], success: bool, tick_dt: int,
    peer: str | None = None, rate_limited: bool = False,
) -> None:
    """Record iteration/runtime counters for one completed tick.

    On fail, also append a per-tick attribution entry to
    `budget['wasted_runtime_per_tick']` so operators can see WHICH ticks
    burned which budget, not just the running sum. Capped at last 20
    entries (older fail-ticks drop off).

    A `rate_limited` tick is NEUTRAL (full-depth-analysis §6): a transient
    server 429/5xx must NOT count toward `max_consecutive_failures`. The v17
    anti-degradation design (peer-health, rotation, backoff) already
    special-cases it, but the budget layer did not — so an all-peers transient
    outage would halt the run with `budget:max_consecutive_failures` after 5
    ticks (`structured_halt`: "a transient error must NOT halt the run").
    Wall-clock (`spent_iterations`/`spent_runtime_s`) still counts.
    """
    state["iteration"] += 1
    budget = state["budget"]
    budget["spent_iterations"] += 1
    budget["spent_runtime_s"] += tick_dt
    if not success and not rate_limited:
        budget["wasted_runtime_s"] = budget.get("wasted_runtime_s", 0) + tick_dt
        per_tick = budget.setdefault("wasted_runtime_per_tick", [])
        per_tick.append({
            "iteration": int(state["iteration"]),
            "peer": str(peer) if peer is not None else None,
            "duration_s": int(tick_dt),
        })
        if len(per_tick) > 20:
            del per_tick[:-20]
    budget["consecutive_failures"] = (
        0 if (success or rate_limited) else budget["consecutive_failures"] + 1
    )
