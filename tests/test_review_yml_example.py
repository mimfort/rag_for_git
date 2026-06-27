import pathlib

import yaml


def test_example_review_yml_documents_new_keys():
    root = pathlib.Path(__file__).resolve().parents[1]
    data = yaml.safe_load((root / ".review.yml").read_text(encoding="utf-8")) or {}
    assert "ignore" in (data.get("paths") or {}), "paths.ignore должен быть в примере"
    assert "summary_cluster_depth_overrides" in data
    assert "summary_topk_threshold" in data
    assert "summary_cluster_depth" in data
    assert "brief_token_cost" in (data.get("solve_task") or {})
