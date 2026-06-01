"""Budget accounting and token/cost parsers for peer runs."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_CONFIGURABLE_BUDGET_LIMITS = (
    "max_iterations", "max_runtime_s", "max_consecutive_failures",
    "max_tokens", "max_usd", "max_usd_mode",
)

# Operator budget caps (`peers-ctl start --max-runtime ...`) are persisted
# here, in `.peers/`, so they survive `_apply_config_budget` — which
# re-overlays config.yaml's caps onto state.budget on EVERY loop start and
# would otherwise clobber the override. peers-ctl writes this sidecar;
# the orchestrator re-applies it right after the config overlay. Kept out
# of state.json on purpose so it also works before state.json exists (the
# first start of a freshly-init'd project).
OPERATOR_BUDGET_OVERRIDE_FILE = "budget-overrides.json"


def _parse_codex_tokens(text: str) -> tuple[int, float]:
    """Codex CLI emits `tokens used\n<N>` near the end."""
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
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        token_keys = (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        )
        tok = 0
        for key in token_keys:
            value = usage.get(key)
            if isinstance(value, int):
                tok += value
        cost = obj.get("total_cost_usd")
        usd = float(cost) if isinstance(cost, (int, float)) else 0.0
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


_TOKEN_PARSERS = {
    "claude": _parse_claude_tokens,
    "codex": _parse_codex_tokens,
}


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
        budget["spent_tokens"] = budget.get("spent_tokens", 0) + tokens
        budget["spent_usd"] = budget.get("spent_usd", 0.0) + usd
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
    path = Path(repo) / ".peers" / OPERATOR_BUDGET_OVERRIDE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
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
    state["budget"]["spent_tokens"] = (
        state["budget"].get("spent_tokens", 0) + tokens
    )
    state["budget"]["spent_usd"] = (
        state["budget"].get("spent_usd", 0.0) + usd
    )
    return tokens, usd


def record_tick_accounting(
    state: dict[str, Any], success: bool, tick_dt: int,
    peer: str | None = None,
) -> None:
    """Record iteration/runtime counters for one completed tick.

    On fail, also append a per-tick attribution entry to
    `budget['wasted_runtime_per_tick']` so operators can see WHICH ticks
    burned which budget, not just the running sum. Capped at last 20
    entries (older fail-ticks drop off).
    """
    state["iteration"] += 1
    budget = state["budget"]
    budget["spent_iterations"] += 1
    budget["spent_runtime_s"] += tick_dt
    if not success:
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
        0 if success else budget["consecutive_failures"] + 1
    )
