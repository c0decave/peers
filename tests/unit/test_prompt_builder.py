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


def test_prompt_includes_root_cause_before_fix_rule():
    # kind: happy — the root-cause-first discipline must reach every peer.
    p = build_prompt(peer="claude", other="codex",
                     goals=[], results={},
                     inbox=[], stuck=False)
    low = p.lower()
    assert "root cause" in low
    assert "no fix" in low or "before" in low


def test_core_directive_root_cause_rule_is_unconditional():
    # kind: edge — the rule lives in the always-injected CORE_DIRECTIVE, so it
    # appears regardless of goals/stuck/mode (not gated on any branch).
    from peers.prompt_builder import CORE_DIRECTIVE
    assert "root cause" in CORE_DIRECTIVE.lower()


def test_core_directive_retains_honesty_bullets():
    # kind: sad/regression — adding the rule must not drop the existing
    # NO SLOP / NO FAKES / NO SKELETONS / BE HONEST discipline.
    from peers.prompt_builder import CORE_DIRECTIVE
    for token in ("NO SLOP", "NO FAKES", "NO SKELETONS", "BE HONEST"):
        assert token in CORE_DIRECTIVE


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


def test_prompt_omits_graphify_block_by_default():
    # graphify is opt-in; with the flag off the prompt is byte-identical to today
    # (no graph-tool guidance leaks in).
    p = build_prompt(peer="claude", other="codex", goals=[], results={},
                     inbox=[], stuck=False)
    assert "query_graph" not in p
    assert "knowledge graph" not in p.lower()


def test_prompt_includes_graphify_block_when_enabled():
    # With graphify_mcp on, agents are taught to PREFER the graph tools over grep,
    # and told to fall back to grep if the tools are unavailable (fail-open).
    p = build_prompt(peer="claude", other="codex", goals=[], results={},
                     inbox=[], stuck=False, graphify_mcp=True)
    for tool in ("query_graph", "get_neighbors", "shortest_path", "get_node",
                 "god_nodes", "graph_stats"):
        assert tool in p, f"missing graph tool '{tool}' in graphify block"
    assert "grep" in p.lower()              # mentions the grep fallback
    pl = p.lower()
    assert "fall back" in pl or "unavailable" in pl  # fail-open instruction
