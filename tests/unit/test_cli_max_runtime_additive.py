"""Test --max-runtime additive syntax (Task 7.4).

`peers-ctl start <project> --max-runtime +Xh` adds X hours to the
current `budget.max_runtime_s` instead of replacing it. The leading
`+` triggers additive semantics; without it the value is absolute.

Pure-function tests on the duration parser (parse_runtime_duration).
"""
from __future__ import annotations

from peers_ctl.cli import parse_runtime_duration


def test_absolute_hours_returns_value():
    delta, additive = parse_runtime_duration("6h")
    assert delta == 6 * 3600
    assert additive is False


def test_additive_hours_with_plus():
    delta, additive = parse_runtime_duration("+6h")
    assert delta == 6 * 3600
    assert additive is True


def test_absolute_minutes():
    delta, additive = parse_runtime_duration("30m")
    assert delta == 30 * 60
    assert additive is False


def test_additive_minutes():
    delta, additive = parse_runtime_duration("+30m")
    assert delta == 30 * 60
    assert additive is True
