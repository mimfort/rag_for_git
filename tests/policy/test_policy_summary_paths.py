from reviewer.policy.policy import DEFAULT_SUMMARY_PATHS_IGNORE, ReviewPolicy


class _Settings:
    """Минимальные настройки: from_settings читает только эти поля."""
    review_severity_threshold = "low"
    review_max_comments = 25
    review_min_confidence = 0.5
    review_output_language = "ru"
    review_grounding_max_distance = 5
    summary_cluster_depth = 2
    summary_topk_threshold = 20
    review_bug_reports = True

    def review_categories_list(self):
        return []

    def task_board_default(self):
        return None


def test_default_ignores_test_trees():
    assert ReviewPolicy.from_settings(_Settings()).summary_paths_ignore == list(
        DEFAULT_SUMMARY_PATHS_IGNORE
    )
    assert "tests" in DEFAULT_SUMMARY_PATHS_IGNORE


def test_missing_key_keeps_default():
    policy = ReviewPolicy.load_data(_Settings(), {"summary_cluster_depth": 3})
    assert policy.summary_paths_ignore == list(DEFAULT_SUMMARY_PATHS_IGNORE)


def test_explicit_value_replaces_default():
    policy = ReviewPolicy.load_data(
        _Settings(), {"summary_paths": {"ignore": ["tests", "eval"]}}
    )
    assert policy.summary_paths_ignore == ["tests", "eval"]


def test_explicit_empty_list_disables_filter():
    """Явный [] выключает фильтр, а не откатывается на дефолт."""
    policy = ReviewPolicy.load_data(_Settings(), {"summary_paths": {"ignore": []}})
    assert policy.summary_paths_ignore == []


def test_from_yaml_reads_summary_paths():
    policy = ReviewPolicy.from_yaml("summary_paths:\n  ignore:\n    - tests\n")
    assert policy.summary_paths_ignore == ["tests"]


def test_from_yaml_without_key_keeps_default():
    policy = ReviewPolicy.from_yaml("summary_cluster_depth: 2\n")
    assert policy.summary_paths_ignore == list(DEFAULT_SUMMARY_PATHS_IGNORE)


def test_summary_paths_ignore_does_not_touch_review_ignore():
    """Фильтр сводок и ignore ревью-индекса — независимые слои."""
    policy = ReviewPolicy.load_data(
        _Settings(), {"summary_paths": {"ignore": ["tests"]}}
    )
    assert policy.ignore == []


def test_public_data_exposes_summary_paths():
    from reviewer.config.layers import policy_to_public_data

    policy = ReviewPolicy.load_data(
        _Settings(), {"summary_paths": {"ignore": ["tests"]}}
    )
    public = policy_to_public_data(policy)
    assert public["summary_paths"] == {"ignore": ["tests"]}
    assert public["paths"] == {"ignore": []}
