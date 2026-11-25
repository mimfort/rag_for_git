from pathlib import Path

import pytest

from eval.pri324.replay import _prepare
from eval.pri327.windows import preserve_original_anchor
from reviewer.mcp.brief_anchors import check_brief_anchors
from reviewer.mcp.brief_evidence import _visible_blocks, _visible_lines


def _block(path, symbol, first, lines):
    return "\n".join([
        f"// {path}#{symbol} ({path}:{first}-{first + len(lines) - 1})",
        *(f"{number} | {line}" for number, line in enumerate(lines, first)),
    ])


def test_keeps_first_original_and_selected_rows_inside_both_per_file_limits():
    main = "reviewer/dispatch.py"
    base_lines = [
        "def dispatch():",
        "    return resolve_context('retain the visible declaration and original behavior')",
        "    # Keep enough code to leave room for the second heading within the same cap.",
        "    return 'original behavior'",
    ]
    selected_lines = [
        "def reconcile():",
        "    # Русский текст занимает больше байтов, чем символов.",
        "    return reconcile_tasks()",
    ]
    other = _block("helpers.py", "helper", 1, ["def helper():", "    return 1"])
    base = _block(main, "dispatch", 1, base_lines)
    selected = _block(main, "reconcile", 6, selected_lines)
    context = preserve_original_anchor(f"{base}\n\n{other}", f"{selected}\n\n{other}")

    assert context.endswith(f"\n\n{other}")
    excerpt = context.removesuffix(f"\n\n{other}")
    assert len(excerpt) <= len(base)
    assert len(excerpt.encode()) <= len(base.encode())
    assert list(_visible_lines(context)) == [main, "helpers.py"]
    assert {1, 6} <= _visible_lines(context)[main]

    sources = {
        main: "\n".join([*base_lines, "", *selected_lines]),
        "helpers.py": "def helper():\n    return 1",
    }
    checked, diagnostics = check_brief_anchors(
        {"code": context}, lambda paths: {path: sources[path] for path in paths}
    )
    assert checked["code"] == _visible_lines(context)
    assert diagnostics["code"]["mismatched"] == diagnostics["code"]["unavailable"] == 0


def test_uses_original_when_second_heading_cannot_fit():
    base = _block("a.py", "old", 1, ["def old(): pass"])
    selected = _block("a.py", "new", 2, ["def new(): pass"])
    assert preserve_original_anchor(base, selected) == base


def test_does_not_duplicate_existing_original_anchor():
    base = _block("a.py", "old", 1, ["def old():", "    return call()"])
    selected = _block("a.py", "old", 1, ["def old():", "    return 1"])
    assert preserve_original_anchor(base, selected) == selected


def test_same_coordinate_with_different_content_preserves_the_original():
    base = _block("a.py", "old", 1, ["def old():", "    return call()"])
    selected = _block("a.py", "new", 1, ["def new():", "    return 1"])
    assert preserve_original_anchor(base, selected) == base


def test_rejects_changed_paths():
    base = _block("a.py", "old", 1, ["def old(): pass"])
    selected = _block("b.py", "new", 2, ["def new(): pass"])
    with pytest.raises(ValueError, match="Пути"):
        preserve_original_anchor(base, selected)


def test_archived_displaced_anchors_remain_visible_together_with_candidate_windows():
    cases = _prepare(Path("eval/pri278"))
    composed = {}
    swaps = 0
    for key, case in cases.items():
        base = case["base"]
        selected = case["reframe"]
        composed[key] = preserve_original_anchor(base, selected)
        original_blocks = base.split("\n\n")
        changes = sum(
            old != new for old, new in zip(original_blocks, selected.split("\n\n"), strict=True)
        )
        assert len(_visible_blocks(composed[key])) == len(original_blocks) + changes
        swaps += changes
    assert swaps == 26

    for key, path, original, replacement in (
        ("PRI-215", "reviewer/tasks/boards/yougile.py", 117, 64),
        ("PRI-223", "reviewer/tasks/boards/github.py", 384, 327),
    ):
        case = cases[key]
        assert original not in _visible_lines(case["reframe"])[path]
        visible = _visible_lines(composed[key])
        assert {original, replacement} <= visible[path]
