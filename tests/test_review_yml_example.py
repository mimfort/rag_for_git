import pathlib
import re

import yaml


def test_example_review_yml_documents_new_keys():
    root = pathlib.Path(__file__).resolve().parents[1]
    data = yaml.safe_load((root / ".review.yml").read_text(encoding="utf-8")) or {}
    assert "ignore" in (data.get("paths") or {}), "paths.ignore должен быть в примере"
    assert "ignore" in (data.get("summary_paths") or {}), (
        "summary_paths.ignore должен быть в примере"
    )
    assert "summary_cluster_depth_overrides" in data
    assert "summary_topk_threshold" in data
    assert "summary_cluster_depth" in data
    assert "brief_token_cost" in (data.get("solve_task") or {})
    assert "context_limits" in data


def test_task_sync_filter_example_is_commented_and_non_operational():
    root = pathlib.Path(__file__).resolve().parents[1]
    text = (root / ".review.yml").read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}

    assert data["task_board"] == {
        "type": "yougile",
        "project": "PRI",
        "key_pattern": r"PRI-\d+",
        "create_target": None,
        "done_target": "Готово",
        "options": {},
    }
    assert re.search(
        r"(?m)^  options: \{\}\n"
        r"  # sync_filter:\n"
        r"  #   max_age_days: 180\n"
        r"  #   include_archived: false$",
        text,
    )
