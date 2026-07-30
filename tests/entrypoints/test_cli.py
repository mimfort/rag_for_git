"""Unit-тесты CLI через CliRunner с моком build_components.

Проверяем:
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
def runner(monkeypatch) -> CliRunner:
    monkeypatch.setattr("reviewer.install.staleness_warnings", lambda: [])
    response = MagicMock(status_code=200)
    response.json.return_value = {"login": "testuser"}
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.httpx.get", MagicMock(return_value=response)
    )
    return CliRunner()


def _assert_no_socket_warnings(recwarn) -> None:
    assert not [
        warning
        for warning in recwarn
        if "socket.getaddrinfo" in str(warning.message)
    ]


@pytest.fixture
def fake_components() -> MagicMock:
    """Фейковые компоненты для мока build_components."""
    c = MagicMock()
    c.store = MagicMock()
    c.graph = MagicMock()
    c.embedder = MagicMock()
    c.retriever = MagicMock()
    c.reranker = MagicMock()
    return c


@pytest.fixture
def fake_settings() -> MagicMock:
    """Фейковые настройки со всеми необходимыми ключами."""
    s = MagicMock()
    s.voyage_api_key = "test-key-111"
    s.github_token = "test-token-222"
    s.pg_dsn = "postgresql://localhost:5432/testdb"
    s.pg_pool_min_size = 1
    s.pg_pool_max_size = 4
    s.neo4j_uri = "bolt://localhost:7687"
    s.neo4j_user = "neo4j"
    s.neo4j_password = "pass"
    s.review_max_files = 50
    s.review_history = False
    s.graph_backend = "auto"
    s.default_repo = "owner/default"
    s.task_board_default.return_value = None
    return s


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

    result = runner.invoke(cli, ["index", "/repo", "--ref", "main", "--repo", "a/x"])

    assert result.exit_code == 0, result.output
    fake_components.store.init_schema.assert_called_once()
    mock_update_base.assert_called_once()
    fake_components.graph.init_schema.assert_called_once()
    fake_components.graph.clear.assert_called_once_with("a/x", branch="main")
    fake_components.graph.upsert_nodes.assert_called_once_with("a/x", ["a#foo", "b#bar"],
                                                                branch="main")
    fake_components.graph.upsert_edges.assert_called_once_with(
        "a/x", [("a#foo", "CALLS", "b#bar")], branch="main"
    )
    fake_components.store.set_index_meta.assert_called_once_with("a/x", "base:main", "abc1234")
    fake_components.store.delete_paths_except.assert_called_once_with(
        "a/x", "base:main", ["a.py", "b.py"]
    )
    fake_components.store.close.assert_called_once()
    fake_components.graph.close.assert_called_once()


@patch("reviewer.entrypoints.cli.remote_url")
@patch("reviewer.entrypoints.cli.build_components")
@patch("reviewer.entrypoints.cli.Settings")
def test_index_no_repo_no_remote_no_default_raises(
    mock_settings_cls,
    mock_build,
    mock_remote_url,
    runner,
    fake_components,
    fake_settings,
):
    """Если нет --repo, нет git remote и нет DEFAULT_REPO — ошибка с подсказкой."""
    fake_settings.default_repo = None
    mock_settings_cls.return_value = fake_settings
    mock_build.return_value = fake_components
    mock_remote_url.return_value = None

    result = runner.invoke(cli, ["index", "/repo"])

    assert result.exit_code != 0
    assert "repo" in result.output.lower()


@patch("reviewer.entrypoints.cli.build_code_graph")
@patch("reviewer.entrypoints.cli.rev_parse")
@patch("reviewer.entrypoints.cli.file_at_ref")
@patch("reviewer.entrypoints.cli.list_python_files")
@patch("reviewer.entrypoints.cli.update_base")
@patch("reviewer.entrypoints.cli.remote_url")
@patch("reviewer.entrypoints.cli.build_components")
@patch("reviewer.entrypoints.cli.Settings")
def test_index_derives_repo_from_git_remote(
    mock_settings_cls,
    mock_build,
    mock_remote_url,
    mock_update_base,
    mock_list_files,
    mock_file_at_ref,
    mock_rev_parse,
    mock_build_graph,
    runner,
    fake_components,
    fake_settings,
):
    """Если не задан --repo, repo_id берётся из git remote."""
    fake_settings.default_repo = None
    mock_settings_cls.return_value = fake_settings
    mock_build.return_value = fake_components
    mock_remote_url.return_value = "https://github.com/owner/myrepo.git"
    mock_list_files.return_value = []
    mock_rev_parse.return_value = "aaa0000"
    mock_file_at_ref.return_value = None
    mock_build_graph.return_value = ([], [], "treesitter")

    result = runner.invoke(cli, ["index", "/repo"])

    assert result.exit_code == 0, result.output
    assert "owner/myrepo" in result.output


@patch("reviewer.entrypoints.cli.build_components")
@patch("reviewer.entrypoints.cli.Settings")
def test_search_command_with_repo_flag(
    mock_settings_cls,
    mock_build,
    runner,
    fake_components,
    fake_settings,
):
    """Команда search с --repo owner/x передаёт repo_id в hybrid_search."""
    mock_settings_cls.return_value = fake_settings
    mock_build.return_value = fake_components
    hit = MagicMock()
    hit.score = 0.9
    hit.node_id = "a#foo"
    hit.path = "a.py"
    hit.start_line = 1
    fake_components.store.hybrid_search.return_value = [hit]

    result = runner.invoke(cli, ["search", "some query", "--repo", "owner/x"])

    assert result.exit_code == 0, result.output
    fake_components.store.hybrid_search.assert_called_once()
    call_args = fake_components.store.hybrid_search.call_args
    assert call_args.args[0] == "owner/x"


@patch("reviewer.entrypoints.cli.build_components")
@patch("reviewer.entrypoints.cli.Settings")
def test_search_command_no_repo_raises(
    mock_settings_cls,
    mock_build,
    runner,
    fake_components,
    fake_settings,
):
    """Команда search без --repo и без DEFAULT_REPO завершается с ошибкой."""
    fake_settings.default_repo = None
    mock_settings_cls.return_value = fake_settings
    mock_build.return_value = fake_components

    result = runner.invoke(cli, ["search", "some query"])

    assert result.exit_code != 0
    assert "repo" in result.output.lower()


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


@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_fails_on_missing_keys(
    mock_settings_cls, mock_chunk_cls, mock_graph_cls, runner, recwarn
):
    """При отсутствии ключей check возвращает exit-код 1."""
    s = MagicMock()
    s.voyage_api_key = ""
    s.github_token = ""
    s.pg_dsn = "pg://test"
    s.neo4j_uri = "neo4j://test"
    s.neo4j_user = "u"
    s.neo4j_password = "p"
    mock_settings_cls.return_value = s
    mock_chunk_cls.return_value = MagicMock()
    mock_graph_cls.return_value = MagicMock()

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 1
    assert "VOYAGE_API_KEY" in result.output
    _assert_no_socket_warnings(recwarn)


@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_fails_on_postgres_error(
    mock_settings_cls, mock_chunk_cls, mock_graph_cls, runner, recwarn
):
    """Ошибка подключения к Postgres даёт exit-код 1."""
    s = MagicMock()
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
    _assert_no_socket_warnings(recwarn)


@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_fails_on_neo4j_error(
    mock_settings_cls, mock_chunk_cls, mock_graph_cls, runner, recwarn
):
    """Ошибка подключения к Neo4j даёт exit-код 1."""
    s = MagicMock()
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
    _assert_no_socket_warnings(recwarn)


@patch("reviewer.entrypoints.cli.httpx.get")
@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_fails_on_github_api_error(
    mock_settings_cls, mock_chunk_cls, mock_graph_cls, mock_httpx, runner
):
    """HTTP-ошибка GitHub API даёт exit-код 1."""
    s = MagicMock()
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
