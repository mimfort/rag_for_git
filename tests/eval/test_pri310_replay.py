import pytest

from eval.pri310.replay import assess, condition_contexts, load_inputs, score_task
from reviewer.mcp.brief_evidence import _visible_lines


def block(path, first):
    return f"// {path}#f ({path}:{first}-{first + 1})\n{first:>5} | def f():\n{first + 1:>5} |     pass"


def test_position_variants_change_only_the_order_and_anchor_graph_only_paths():
    paths = ["a.py", "b.py", "c.py", "d.py", "e.py"]
    parts = [block(path, first) for first, path in enumerate(paths, 10)]
    context = "\n\n".join(parts)
    variants = condition_contexts(context, ["a.py", "b.py", "d.py", "e.py"], paths)
    assert variants["rank"] == context
    assert variants["edges"] == "\n\n".join(parts[i] for i in [0, 2, 4, 3, 1])
    assert variants["graph_fixed"] == "\n\n".join(parts[i] for i in [0, 3, 2, 4, 1])
    assert all(_visible_lines(text) == _visible_lines(context) for text in variants.values())


def test_truncated_context_is_rejected_before_reordering():
    with pytest.raises(ValueError, match="Обрезанная"):
        condition_contexts(block("a.py", 10) + "\n[...truncated]", ["a.py"], ["a.py"])


def test_replay_covers_twenty_frozen_cases_with_five_large():
    inputs, labels = load_inputs()
    assert len(inputs) == len(labels) == 20
    assert sum(len(label["core"]) >= 10 for label in labels.values()) == 5
    assert all("core" not in row and "dependencies" not in row for row in inputs)


def test_scoring_requires_a_line_visible_in_the_section():
    row = {"key": "PRI-123", "contexts": {"rank": "\n\n".join([
        block("a.py", 10), block("b.py", 20),
    ])}}
    brief = {"key": "PRI-123", "brief": "## Relevant code\n- a.py:10 — править\n"
                                       "- b.py:22 — проверить"}
    result = score_task(row, "rank", brief, {"core": {"a.py"}, "dependencies": {"b.py"}})
    assert result["coverage"] == {
        "core": {"matched": 1, "all": 1}, "dependencies": {"matched": 0, "all": 1},
    }
    assert result["rejected_references"] == 1


def test_gate_rejects_dependency_loss_in_large_tasks_even_with_pooled_gain():
    baseline = {
        "all": {"core": {"matched": 50, "all": 100}, "dependencies": {"matched": 7, "all": 10}},
        "large": {"dependencies": {"matched": 2, "all": 3}},
        "missing_briefs": 0,
    }
    candidate = {
        "all": {"core": {"matched": 56, "all": 100}, "dependencies": {"matched": 8, "all": 10}},
        "large": {"dependencies": {"matched": 1, "all": 3}},
        "missing_briefs": 0,
    }
    assert not assess(baseline, candidate)["eligible"]
    candidate["large"] = baseline["large"]
    assert assess(baseline, candidate)["eligible"]
    candidate["missing_briefs"] = 1
    assert not assess(baseline, candidate)["eligible"]
