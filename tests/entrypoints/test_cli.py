"""Unit-тесты CLI через CliRunner с моком build_components.

Проверяем:
- review вызывает ReviewService.run_review с правильными аргументами;
- index выполняет шаги индексации (init_schema, update_base, build_code_graph);
- check возвращает корректные exit-коды.
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from reviewer.entrypoints.cli import cli
from reviewer.services.review_service import (
    _hunk_count,
    _file_importance_key,
    _select_changed_files,
)
from reviewer.vcs.base import ChangedFile


# ---------------------------------------------------------------------------
# Существующие тесты хелперов ReviewService
# ---------------------------------------------------------------------------


def test_hunk_count_none_and_empty():
    """При отсутствии patch возвращается 0."""
    assert _hunk_count(None) == 0
    assert _hunk_count("") == 0


def test_hunk_count_counts_atat_lines():
    """Hunks определяются по строкам, начинающимся с @@."""
    patch = "@@ -1,2 +1,2 @@\n a\n b\n@@ -5,1 +5,1 @@\n c"
    assert _hunk_count(patch) == 2


def test_importance_key_prefers_more_hunks_then_longer_patch():
    """Больше hunks → важнее; при равенстве — длиннее patch."""
    small = ChangedFile("small.py", "modified", "@@ -1 +1 @@\n a")
    big = ChangedFile("big.py", "modified", "@@ -1,5 +1,5 @@\n" + "\n a" * 10)
    # same hunks (1), big has longer patch → big is more important (smaller key)
    assert _file_importance_key(big) < _file_importance_key(small)


def test_select_changed_files_filters_non_py_and_removed():
    """Оставляем только .py, не удалённые."""
    files = [
        ChangedFile("a.py", "modified", "@@ -1 +1 @@\n x"),
        ChangedFile("b.txt", "modified", "@@ -1 +1 @@\n y"),
        ChangedFile("c.py", "removed", "@@ -1 +1 @@\n z"),
    ]
    selected = _select_changed_files(files, max_files=10)
    assert [f.path for f in selected] == ["a.py"]


def test_select_changed_files_sorts_by_importance():
    """Файлы сортируются по важности: больше hunks, затем длиннее patch."""
    files = [
        ChangedFile("small.py", "modified", "@@ -1 +1 @@\n a"),
        ChangedFile(
            "big.py", "modified",
            "@@ -1,5 +1,5 @@\n" + "\n a" * 10 + "\n@@ -20 +20 @@\n b"
        ),
        ChangedFile(
            "medium.py", "modified",
            "@@ -1,2 +1,2 @@\n a\n b\n@@ -5 +5 @@\n c"
        ),
    ]
    selected = _select_changed_files(files, max_files=2)
    assert [f.path for f in selected] == ["big.py", "medium.py"]


def test_select_changed_files_respects_max_files():
    """При превышении лимита возвращается ровно max_files элементов."""
    files = [
        ChangedFile(f"f{i}.py", "modified", f"@@ -{i} +{i} @@\n x")
        for i in range(5)
    ]
    selected = _select_changed_files(files, max_files=2)
    assert len(selected) == 2


# ---------------------------------------------------------------------------
# Фикстуры для CLI-тестов
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_components() -> MagicMock:
    """Фейковые компоненты для мока build_components."""
    c = MagicMock()
    c.store = MagicMock()
    c.graph = MagicMock()
    c.embedder = MagicMock()
    c.retriever = MagicMock()
    c.reranker = MagicMock()
    c.llm_provider = MagicMock()
    return c


@pytest.fixture
def fake_settings() -> MagicMock:
    """Фейковые настройки со всеми необходимыми ключами."""
    s = MagicMock()
    s.openrouter_api_key = "test-or"
    s.voyage_api_key = "test-voyage"
    s.github_token = "test-gh"
    s.pg_dsn = "postgresql://test"
    s.pg_pool_min_size = 1
    s.pg_pool_max_size = 4
    s.neo4j_uri = "neo4j://test"
    s.neo4j_user = "neo4j"
    s.neo4j_password = "pass"
    s.review_max_files = 50
    s.review_history = False
    s.graph_backend = "auto"
    s.review_trace = False
    s.review_verdict_log = ""
    return s


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


@patch("reviewer.entrypoints.cli.ReviewService")
@patch("reviewer.entrypoints.cli.build_components")
@patch("reviewer.entrypoints.cli.Settings")
def test_review_invokes_service_with_correct_args(
    mock_settings_cls,
    mock_build,
    mock_service_cls,
    runner,
    fake_components,
    fake_settings,
):
    """Команда review вызывает ReviewService.run_review с правильными аргументами."""
    mock_settings_cls.return_value = fake_settings
    mock_build.return_value = fake_components

    mock_result = MagicMock()
    mock_result.is_draft_skip = False
    mock_result.skipped_paths = None
    mock_result.published_flag = True
    mock_result.publish_failed = []
    mock_result.state = {"summary": "OK", "inline_comments": []}
    mock_result.usage.report.return_value = ""

    mock_service = MagicMock()
    mock_service.run_review.return_value = mock_result
    mock_service_cls.return_value = mock_service

    result = runner.invoke(cli, ["review", "owner/repo", "42"])

    assert result.exit_code == 0
    mock_service_cls.assert_called_once_with(fake_settings, fake_components)
    mock_service.run_review.assert_called_once_with("owner", "repo", 42, dry_run=False)


@patch("reviewer.entrypoints.cli.ReviewService")
@patch("reviewer.entrypoints.cli.build_components")
@patch("reviewer.entrypoints.cli.Settings")
def test_review_dry_run_flag(
    mock_settings_cls,
    mock_build,
    mock_service_cls,
    runner,
    fake_components,
    fake_settings,
):
    """Флаг --dry-run передаётся в ReviewService.run_review."""
    mock_settings_cls.return_value = fake_settings
    mock_build.return_value = fake_components

    mock_result = MagicMock()
    mock_result.is_draft_skip = False
    mock_result.skipped_paths = None
    mock_result.published_flag = None
    mock_result.publish_failed = []
    mock_result.state = {"summary": "dry", "inline_comments": []}
    mock_result.usage.report.return_value = ""

    mock_service = MagicMock()
    mock_service.run_review.return_value = mock_result
    mock_service_cls.return_value = mock_service

    result = runner.invoke(cli, ["review", "--dry-run", "owner/repo", "42"])

    assert result.exit_code == 0
    mock_service.run_review.assert_called_once_with("owner", "repo", 42, dry_run=True)
    assert "Dry-run" in result.output


@patch("reviewer.entrypoints.cli.ReviewService")
@patch("reviewer.entrypoints.cli.build_components")
@patch("reviewer.entrypoints.cli.Settings")
def test_review_draft_skip_message(
    mock_settings_cls,
    mock_build,
    mock_service_cls,
    runner,
    fake_components,
    fake_settings,
):
    """При draft_skip CLI выводит соответствующее сообщение."""
    mock_settings_cls.return_value = fake_settings
    mock_build.return_value = fake_components

    mock_result = MagicMock()
    mock_result.is_draft_skip = True
    mock_result.usage.report.return_value = ""

    mock_service = MagicMock()
    mock_service.run_review.return_value = mock_result
    mock_service_cls.return_value = mock_service

    result = runner.invoke(cli, ["review", "owner/repo", "42"])

    assert result.exit_code == 0
    assert "драфт" in result.output.lower()


@patch("reviewer.entrypoints.cli.ReviewService")
@patch("reviewer.entrypoints.cli.build_components")
@patch("reviewer.entrypoints.cli.Settings")
def test_review_publish_failure_message(
    mock_settings_cls,
    mock_build,
    mock_service_cls,
    runner,
    fake_components,
    fake_settings,
):
    """При published_flag=False CLI выводит ошибки публикации."""
    mock_settings_cls.return_value = fake_settings
    mock_build.return_value = fake_components

    mock_result = MagicMock()
    mock_result.is_draft_skip = False
    mock_result.skipped_paths = None
    mock_result.published_flag = False
    mock_result.publish_failed = ["publish: NetworkError"]
    mock_result.state = {"summary": "err", "inline_comments": []}
    mock_result.usage.report.return_value = ""

    mock_service = MagicMock()
    mock_service.run_review.return_value = mock_result
    mock_service_cls.return_value = mock_service

    result = runner.invoke(cli, ["review", "owner/repo", "42"])

    assert result.exit_code == 0
    assert "Не удалось опубликовать" in result.output
    assert "NetworkError" in result.output


@patch("reviewer.entrypoints.cli.build_components")
@patch("reviewer.entrypoints.cli.Settings")
def test_review_missing_api_keys(
    mock_settings_cls,
    mock_build,
    runner,
    fake_settings,
):
    """При отсутствии ключей review падает до вызова build_components."""
    fake_settings.openrouter_api_key = ""
    fake_settings.voyage_api_key = ""
    fake_settings.github_token = ""
    mock_settings_cls.return_value = fake_settings

    result = runner.invoke(cli, ["review", "owner/repo", "42"])

    assert result.exit_code != 0
    assert "OPENROUTER_API_KEY" in result.output
    assert "VOYAGE_API_KEY" in result.output
    assert "GITHUB_TOKEN" in result.output
    mock_build.assert_not_called()


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


@patch("reviewer.entrypoints.cli.build_code_graph")
@patch("reviewer.entrypoints.cli.rev_parse")
@patch("reviewer.entrypoints.cli.file_at_ref")
@patch("reviewer.entrypoints.cli.list_python_files")
@patch("reviewer.entrypoints.cli.update_base")
@patch("reviewer.entrypoints.cli.build_components")
@patch("reviewer.entrypoints.cli.Settings")
def test_index_command_runs_indexing_steps(
    mock_settings_cls,
    mock_build,
    mock_update_base,
    mock_list_files,
    mock_file_at_ref,
    mock_rev_parse,
    mock_build_graph,
    runner,
    fake_components,
    fake_settings,
):
    """Команда index вызывает init_schema, update_base, build_code_graph и cleanup."""
    mock_settings_cls.return_value = fake_settings
    mock_build.return_value = fake_components
    mock_list_files.return_value = ["a.py", "b.py"]
    mock_rev_parse.return_value = "abc1234"
    mock_file_at_ref.return_value = "def foo(): pass"
    mock_build_graph.return_value = (
        ["a#foo", "b#bar"],
        [("a#foo", "CALLS", "b#bar")],
        "treesitter",
    )

    result = runner.invoke(cli, ["index", "/repo", "--ref", "main"])

    assert result.exit_code == 0
    fake_components.store.init_schema.assert_called_once()
    mock_update_base.assert_called_once()
    fake_components.graph.init_schema.assert_called_once()
    fake_components.graph.clear.assert_called_once()
    fake_components.graph.upsert_nodes.assert_called_once_with(["a#foo", "b#bar"])
    fake_components.graph.upsert_edges.assert_called_once_with(
        [("a#foo", "CALLS", "b#bar")]
    )
    fake_components.store.close.assert_called_once()
    fake_components.graph.close.assert_called_once()


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


@patch("reviewer.entrypoints.cli._shutil")
@patch("reviewer.entrypoints.cli.httpx.get")
@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_all_ok(
    mock_settings_cls,
    mock_chunk_cls,
    mock_graph_cls,
    mock_httpx,
    mock_shutil,
    runner,
):
    """Все проверки проходят — exit-код 0."""
    s = MagicMock()
    s.openrouter_api_key = "key"
    s.voyage_api_key = "key"
    s.github_token = "key"
    s.pg_dsn = "pg://test"
    s.pg_pool_min_size = 1
    s.pg_pool_max_size = 4
    s.neo4j_uri = "neo4j://test"
    s.neo4j_user = "u"
    s.neo4j_password = "p"
    mock_settings_cls.return_value = s

    store = MagicMock()
    conn = store._connect.return_value.__enter__.return_value
    conn.execute.return_value = None
    mock_chunk_cls.return_value = store

    graph = MagicMock()
    mock_graph_cls.return_value = graph

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"login": "testuser"}
    mock_httpx.return_value = resp

    mock_shutil.which.return_value = "/usr/bin/scip-python"

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 0
    assert "Готово к работе" in result.output
    store.close.assert_called_once()
    graph.close.assert_called_once()


@patch("reviewer.entrypoints.cli.Settings")
def test_check_fails_on_missing_keys(mock_settings_cls, runner):
    """При отсутствии ключей check возвращает exit-код 1."""
    s = MagicMock()
    s.openrouter_api_key = ""
    s.voyage_api_key = ""
    s.github_token = ""
    s.pg_dsn = "pg://test"
    s.neo4j_uri = "neo4j://test"
    s.neo4j_user = "u"
    s.neo4j_password = "p"
    mock_settings_cls.return_value = s

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 1
    assert "OPENROUTER_API_KEY" in result.output


@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_fails_on_postgres_error(
    mock_settings_cls, mock_chunk_cls, mock_graph_cls, runner
):
    """Ошибка подключения к Postgres даёт exit-код 1."""
    s = MagicMock()
    s.openrouter_api_key = "key"
    s.voyage_api_key = "key"
    s.github_token = "key"
    s.pg_dsn = "pg://test"
    s.pg_pool_min_size = 1
    s.pg_pool_max_size = 4
    s.neo4j_uri = "neo4j://test"
    s.neo4j_user = "u"
    s.neo4j_password = "p"
    mock_settings_cls.return_value = s

    mock_chunk_cls.side_effect = RuntimeError("connection refused")
    mock_graph_cls.return_value = MagicMock()

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 1
    assert "Postgres" in result.output


@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_fails_on_neo4j_error(
    mock_settings_cls, mock_chunk_cls, mock_graph_cls, runner
):
    """Ошибка подключения к Neo4j даёт exit-код 1."""
    s = MagicMock()
    s.openrouter_api_key = "key"
    s.voyage_api_key = "key"
    s.github_token = "key"
    s.pg_dsn = "pg://test"
    s.pg_pool_min_size = 1
    s.pg_pool_max_size = 4
    s.neo4j_uri = "neo4j://test"
    s.neo4j_user = "u"
    s.neo4j_password = "p"
    mock_settings_cls.return_value = s

    store = MagicMock()
    mock_chunk_cls.return_value = store

    graph = MagicMock()
    graph._driver.verify_connectivity.side_effect = RuntimeError("neo4j down")
    mock_graph_cls.return_value = graph

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 1
    assert "Neo4j" in result.output


@patch("reviewer.entrypoints.cli.httpx.get")
@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_fails_on_github_api_error(
    mock_settings_cls, mock_chunk_cls, mock_graph_cls, mock_httpx, runner
):
    """HTTP-ошибка GitHub API даёт exit-код 1."""
    s = MagicMock()
    s.openrouter_api_key = "key"
    s.voyage_api_key = "key"
    s.github_token = "key"
    s.pg_dsn = "pg://test"
    s.pg_pool_min_size = 1
    s.pg_pool_max_size = 4
    s.neo4j_uri = "neo4j://test"
    s.neo4j_user = "u"
    s.neo4j_password = "p"
    mock_settings_cls.return_value = s

    store = MagicMock()
    mock_chunk_cls.return_value = store
    mock_graph_cls.return_value = MagicMock()

    resp = MagicMock()
    resp.status_code = 401
    mock_httpx.return_value = resp

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 1
    assert "GitHub" in result.output
