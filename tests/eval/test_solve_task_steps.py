"""Офлайн-атрибуция расхода solve-task по под-шагам (PRI-248, фаза 1)."""
from eval.solve_task_metrics import steps


def _assistant(tool_names, usage):
    content = [{"type": "tool_use", "name": name, "input": {}} for name in tool_names]
    return {
        "type": "assistant",
        "message": {"model": "claude-sonnet-5", "content": content, "usage": usage},
    }


def test_classify_preflight_tools():
    line = _assistant(["Bash"], {"output_tokens": 1})
    line["message"]["content"][0]["input"] = {"command": "uvx --from rag-reviewer reviewer status . --json"}
    assert steps.classify_turn(line) == "preflight"


def test_classify_gather_tools():
    assert steps.classify_turn(_assistant(["mcp__reviewer__search_codebase"], {})) == "gather"
    assert steps.classify_turn(_assistant(["mcp__reviewer__get_subsystem_summaries"], {})) == "gather"


def test_classify_brief_write():
    line = _assistant(["Write"], {})
    line["message"]["content"][0]["input"] = {
        "file_path": "/repo/docs/superpowers/briefs/2026-08-15-PRI-248-x.md"
    }
    assert steps.classify_turn(line) == "brief"


def test_classify_unknown_is_other():
    assert steps.classify_turn(_assistant(["Glob"], {})) == "other"


def test_text_only_turn_is_other():
    line = {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}], "usage": {}}}
    assert steps.classify_turn(line) == "other"


def test_attribute_window_splits_buckets_by_step():
    lines = [
        {"type": "user", "message": {"content": "Base directory for this skill: skills/solve-task PRI-1"}},
        _assistant(["mcp__reviewer__sync_board"], {"cache_creation_input_tokens": 100}),
        _assistant(["mcp__reviewer__search_codebase"], {"cache_creation_input_tokens": 300}),
    ]
    by_step = steps.attribute_window(lines, 0, len(lines))
    assert by_step["preflight"]["cache_write"] == 100.0
    assert by_step["gather"]["cache_write"] == 300.0
    assert by_step["brief"]["cache_write"] == 0.0


def test_weighted_shares_sum_to_one():
    by_step = steps.attribute_window(
        [
            {"type": "user", "message": {"content": ""}},
            _assistant(["mcp__reviewer__sync_board"], {"output_tokens": 10}),
            _assistant(["mcp__reviewer__search_tasks"], {"output_tokens": 30}),
        ],
        0,
        3,
    )
    shares = steps.weighted_shares(by_step)
    assert abs(sum(shares.values()) - 1.0) < 1e-9
    assert abs(shares["preflight"] - 0.25) < 1e-9


def test_weighted_shares_zero_cost_is_zero_not_crash():
    empty = {s: {"fresh_in": 0.0, "output": 0.0, "cache_write": 0.0, "cache_read": 0.0}
             for s in steps.STEPS}
    assert steps.weighted_shares(empty) == {s: 0.0 for s in steps.STEPS}


def test_scan_steps_missing_root_is_empty(tmp_path):
    assert steps.scan_steps(tmp_path / "nope") == {}
