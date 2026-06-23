from reviewer.policy.policy import ReviewPolicy


def test_from_yaml_reads_depth_overrides():
    text = "summary_cluster_depth: 2\nsummary_cluster_depth_overrides:\n  reviewer/index: 3\n"
    pol = ReviewPolicy.from_yaml(text)
    assert pol.summary_cluster_depth_overrides == {"reviewer/index": 3}


def test_load_overrides_only_when_present(monkeypatch):
    class S:
        def review_categories_list(self): return []
        review_severity_threshold = "low"
        review_max_comments = 25
        review_min_confidence = 0.5
        review_output_language = "ru"
        def task_board_default(self): return None
        review_grounding_max_distance = 5
        summary_cluster_depth = 2
        summary_topk_threshold = 20
    pol = ReviewPolicy.load(S(), "summary_cluster_depth_overrides:\n  vendor: 1\n")
    assert pol.summary_cluster_depth_overrides == {"vendor": 1}
    pol2 = ReviewPolicy.load(S(), "max_comments: 10\n")
    assert pol2.summary_cluster_depth_overrides == {}      # дефолт, ключа нет
