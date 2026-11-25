from __future__ import annotations

import pytest

from eval.pri324.attribution import attribute_pair, summarize_pairs
from reviewer.mcp.brief_anchors import check_brief_anchors

SOURCES = {
    "steady.py": "def steady():\n    return 1",
    "move.py": "def old():\n    return 1\n\ndef new():\n    return 2",
    "reject.py": "def reject():\n    return 3",
}

STEADY = "// steady (steady.py:1-2)\n    1 | def steady():\n    2 |     return 1"
OLD_MOVE = "// old (move.py:1-2)\n    1 | def old():\n    2 |     return 1"
NEW_MOVE = "// new (move.py:4-5)\n    4 | def new():\n    5 |     return 2"
REJECT = "// reject (reject.py:1-2)\n    1 | def reject():\n    2 |     return 3"


def make_case(base, reframe):
    case = {
        "case": {
            "key": "PRI-1",
            "input": "Проверить влияние на бриф",
            "obligatory": [{"path": path} for path in SOURCES],
        },
        "base": base,
        "reframe": reframe,
    }
    for arm in ("base", "reframe"):
        case[f"{arm}_eligible"], counts = check_brief_anchors(
            {"code": case[arm]}, lambda paths: {path: SOURCES[path] for path in paths}
        )
        assert counts["code"]["mismatched"] == counts["code"]["unavailable"] == 0
    return case


def test_distinguishes_exact_unchanged_omission_from_removed_and_rejected_anchors():
    case = make_case(f"{STEADY}\n\n{OLD_MOVE}\n\n{REJECT}", f"{STEADY}\n\n{NEW_MOVE}\n\n{REJECT}")
    baseline = {
        "brief": "- steady.py:1 — функция\n- move.py:1 — прежняя ветка\n- reject.py:2 — возврат",
        "verified_paths": list(SOURCES),
    }
    candidate = {"brief": "- reject.py:30 — ошибочная строка", "verified_paths": []}

    result = attribute_pair(case, baseline, candidate)
    by_path = {event["path"]: event for event in result["events"]}

    assert not result["identical_task_input"]
    assert result["core"] == {"base": 3, "reframe": 0, "all": 3}
    assert by_path["steady.py"]["kind"] == "omitted"
    assert by_path["steady.py"]["window_unchanged"]
    assert by_path["steady.py"]["same_anchor_lines"] == [1]
    assert by_path["move.py"]["kind"] == "anchor_unavailable"
    assert not by_path["move.py"]["window_unchanged"]
    assert by_path["move.py"]["same_anchor_lines"] == []
    assert by_path["reject.py"]["kind"] == "rejected"
    assert by_path["reject.py"]["same_anchor_lines"] == [2]
    assert by_path["reject.py"]["other_references"] == [
        {"line": 30, "reason": "ошибочная строка"},
    ]


def test_identical_task_input_can_still_gain_a_verified_path():
    case = make_case(STEADY, STEADY)
    result = attribute_pair(
        case,
        {"brief": "", "verified_paths": []},
        {"brief": "- steady.py:1 — существующая функция", "verified_paths": ["steady.py"]},
    )

    assert result["identical_task_input"]
    event, = result["events"]
    assert event["direction"] == "gain"
    assert event["window_unchanged"]
    assert event["kind"] == "omitted"


def test_rejects_archive_paths_unsupported_by_verified_anchors():
    case = make_case(STEADY, STEADY)
    with pytest.raises(ValueError, match="подтверждённых путей"):
        attribute_pair(
            case,
            {"brief": "- steady.py:20 — не показывалась", "verified_paths": ["steady.py"]},
            {"brief": "", "verified_paths": []},
        )


def test_marks_a_path_unavailable_when_it_has_no_verified_window():
    case = make_case(STEADY, REJECT)
    result = attribute_pair(
        case,
        {"brief": "- steady.py:1 — функция", "verified_paths": ["steady.py"]},
        {"brief": "", "verified_paths": []},
    )
    event, = result["events"]
    assert event["kind"] == "path_unavailable"
    assert event["visible_ranges"]["reframe"] is None


def test_difference_of_task_medians_is_distinct_from_median_paired_change():
    def row(key, base, reframe, total, *, identical=False):
        return {
            "key": key,
            "core": {"base": base, "reframe": reframe, "all": total},
            "identical_task_input": identical,
            "events": [],
        }

    rows = [
        row("PRI-170", 8, 8, 15), row("PRI-170", 6, 8, 15),
        row("PRI-202", 2, 2, 11, identical=True), row("PRI-202", 2, 3, 11, identical=True),
        row("PRI-221", 3, 2, 10), row("PRI-221", 4, 4, 10),
    ]

    summary = summarize_pairs(rows)

    assert summary["micro_core"] == {"base": 25, "reframe": 27, "all": 72}
    assert summary["task_medians"] == {"base": 0.35, "reframe": 0.30}
    assert summary["difference_of_arm_medians"] == pytest.approx(-0.05)
    assert summary["median_paired_task_delta"] == pytest.approx(1 / 22)
    assert summary["identical_input_sensitivity"] == {
        "identical_observations": 2,
        "micro_core": {"base": 25, "reframe": 26, "all": 72},
        "task_medians": {"base": 0.35, "reframe": 0.30},
    }
