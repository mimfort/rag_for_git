"""Guard: ядро метрики читается из .review.yml, а не из константы модуля.

Мутационная проверка (критерий 5 PRI-271): если снять чтение ключа из
политики на копии модуля вне рабочего дерева, эти тесты обязаны покраснеть.
"""
from reviewer.metrics.brief_quality.config import DEFAULT_CORE_PATHS
from reviewer.policy.policy import ReviewPolicy

FOREIGN = """
task_board:
  type: yougile
  key_pattern: 'RON-\\d+'
metrics:
  brief_quality:
    core_paths:
      - 'app/**/*.py'
      - 'frontend/src/**'
"""


def test_policy_reads_core_paths_from_review_yml():
    policy = ReviewPolicy.from_yaml(FOREIGN)
    assert policy.brief_quality.core_paths == ("app/**/*.py", "frontend/src/**")
    assert policy.brief_quality.configured is True
    assert policy.brief_quality.key_pattern == r"RON-\d+"
    assert policy.brief_quality.matches_core("app/api/routes.py") is True
    assert policy.brief_quality.matches_core("reviewer/app.py") is False


def test_policy_without_key_keeps_default_and_unconfigured():
    policy = ReviewPolicy.from_yaml("severity_threshold: low\n")
    assert policy.brief_quality.core_paths == DEFAULT_CORE_PATHS
    assert policy.brief_quality.configured is False


def test_load_data_reads_core_paths(monkeypatch):
    """load_data — второй путь чтения политики (MCP), он тоже обязан видеть ключ."""
    from reviewer.config.settings import Settings

    policy = ReviewPolicy.load_data(
        Settings(_env_file=None),
        {"metrics": {"brief_quality": {"core_paths": ["src/**/*.py"]}}},
    )
    assert policy.brief_quality.core_paths == ("src/**/*.py",)
    assert policy.brief_quality.configured is True
