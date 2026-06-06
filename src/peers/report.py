"""Helpers for summarising peer output formats."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class StreamSummary:
    tool_counts: Counter = field(default_factory=Counter)
    text_emissions: int = 0
    total_cost_usd: float | None = None
    num_turns: int | None = None
    is_error: bool | None = None


def summarise_stream_json_log(text: str) -> StreamSummary:
    """Summarise Claude ``--output-format stream-json --verbose`` output."""
    summary = StreamSummary()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "assistant":
            content = ev.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    summary.tool_counts[str(item.get("name", "?"))] += 1
                elif item.get("type") == "text":
                    summary.text_emissions += 1
        elif ev.get("type") == "result":
            cost = ev.get("total_cost_usd")
            # bool is an int subclass — exclude it so a malformed
            # `total_cost_usd: true` / `num_turns: false` is rejected, not
            # silently coerced to 1.0 / False.
            summary.total_cost_usd = (
                float(cost)
                if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                else None
            )
            turns = ev.get("num_turns")
            summary.num_turns = (
                turns
                if isinstance(turns, int) and not isinstance(turns, bool)
                else None
            )
            is_error = ev.get("is_error")
            summary.is_error = is_error if isinstance(is_error, bool) else None
    return summary
