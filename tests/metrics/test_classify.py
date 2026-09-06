"""Unit-тесты классификатора путей и таксономии промахов."""
import pytest

from reviewer.metrics.brief_quality import classify
from reviewer.metrics.brief_quality.config import DEFAULT, BriefQualityConfig


@pytest.mark.parametrize("path", ["reviewer/app.py", "plugin/hooks/x.py", "sync_chunk.py"])
def test_core_paths_with_default_config(path):
    assert classify.is_core_production_path(path, DEFAULT) is True


def test_foreign_config_moves_the_core():
    config = BriefQualityConfig.from_review_yaml(
        {"metrics": {"brief_quality": {"core_paths": ["app/**/*.py"]}}}
    )
    assert classify.is_core_production_path("app/api/routes.py", config) is True
    assert classify.is_core_production_path("reviewer/app.py", config) is False


def test_categorize_miss_new_file_wins_over_directory():
    assert classify.categorize_miss("reviewer/new.py", existed_before=False, config=DEFAULT) == (
        classify.NEW_FILE_CATEGORY
    )


@pytest.mark.parametrize("path,expected", [
    ("tests/metrics/test_x.py", "tests/"),
    ("docs/superpowers/plans/x.md", "docs/"),
    ("reviewer/index/store.py", "reviewer/index"),   # файл ядра → верхний сегмент + модуль
    ("reviewer/app.py", "reviewer/"),                # ядро без модуля
    ("plugin/skills/x.md", "plugin/"),               # не ядро (исключение) → верхний сегмент
    (".review.yml", "корень"),
])
def test_categorize_miss_is_derived_from_core_paths(path, expected):
    assert classify.categorize_miss(path, existed_before=True, config=DEFAULT) == expected


def test_categorize_miss_follows_foreign_core():
    config = BriefQualityConfig.from_review_yaml(
        {"metrics": {"brief_quality": {"core_paths": ["app/**/*.py"]}}}
    )
    assert classify.categorize_miss("app/api/routes.py", True, config) == "app/api"
    assert classify.categorize_miss("reviewer/index/store.py", True, config) == "reviewer/"
