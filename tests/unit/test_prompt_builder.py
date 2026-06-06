from peers.goals import Goal
from peers.goal_engine import GoalResult
from peers.prompt_builder import build_prompt


def test_prompt_contains_identity():
    p = build_prompt(
        peer="claude", other="codex",
        goals=[],
        results={},
        inbox=[],
        stuck=False,
    )
    assert "You are peer 'claude'" in p
    assert "claude" in p and "codex" in p


def test_prompt_lists_open_goals_with_pass_fail():
    goals = [
        Goal(id="t", type="hard", cmd="x", pass_when="exit_code == 0"),
        Goal(id="c", type="hard", cmd="y", pass_when="exit_code == 0"),
    ]
    results = {
        "t": GoalResult("t", "pass", 10),
        "c": GoalResult("c", "fail", 10, diagnostic="bla"),
    }
    p = build_prompt(peer="claude", other="codex",
                     goals=goals, results=results,
                     inbox=[], stuck=False)
    assert "t" in p and "pass" in p
    assert "c" in p and "fail" in p


def test_prompt_includes_self_review_obligation():
    p = build_prompt(peer="claude", other="codex",
                     goals=[], results={},
                     inbox=[], stuck=False)
    assert "Self-Review" in p
    assert "Peer-Status: handoff" in p


def test_stuck_hint_appears_when_stuck():
    # kind: edge
    # "stuck" is an off-nominal peer state; the prompt branch under test
    # only fires there, so it covers the edge class for prompt_builder.
    p = build_prompt(peer="claude", other="codex",
                     goals=[], results={},
                     inbox=[], stuck=True)
    assert "strategy" in p.lower()


def test_inbox_messages_rendered():
    p = build_prompt(peer="claude", other="codex",
                     goals=[], results={},
                     inbox=["codex says: please add tests for foo"],
                     stuck=False)
    assert "codex says" in p


def test_open_goals_section_only_when_failures():
    goals = [Goal(id="t", type="hard", cmd="x", pass_when="exit_code == 0")]
    results = {"t": GoalResult("t", "pass", 5)}
    p = build_prompt(peer="claude", other="codex",
                     goals=goals, results=results,
                     inbox=[], stuck=False)
    assert "OPEN GOALS" not in p


def test_prompt_does_not_include_unused_code_analyz0r_block():
    p = build_prompt(peer="claude", other="codex",
                     goals=[], results={},
                     inbox=[], stuck=False)
    assert "code-analyz0r" not in p.lower()


def test_prompt_points_at_context_files():
    p = build_prompt(peer="claude", other="codex", goals=[], results={},
                     inbox=[], stuck=False)
    assert "PROJECT CONTEXT" in p
    assert ".peers/recon.md" in p
    assert ".peers/codemap.md" in p
    # LLM01 hygiene: the untrusted-data framing must not be silently dropped.
    assert "untrusted project data" in p
