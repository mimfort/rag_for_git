"""Unit-тесты CLI через CliRunner с моком build_components.

Проверяем:
- index выполняет шаги индексации (init_schema, update_base, build_code_graph);
- check возвращает корректные exit-коды.
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from click.testing import CliRunner
from neo4j.exceptions import AuthError as Neo4jAuthError

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
    s.review_branches = "main"
    s.review_branches_list.return_value = ["main"]
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
    monkeypatch,
    tmp_path,
):
    """Команда index вызывает init_schema, update_base, build_code_graph и cleanup."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
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
    monkeypatch,
    tmp_path,
):
    """Если не задан --repo, repo_id берётся из git remote."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
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
    s.gitlab_token = ""
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


def _check_settings():
    """Settings для check с локальными эндпоинтами (иначе совет не положен вовсе)."""
    s = MagicMock()
    s.voyage_api_key = "key"
    s.github_token = "key"
    s.pg_dsn = "postgresql://reviewer:s3cretpw@127.0.0.1:5433/reviewer"
    s.pg_pool_min_size = 1
    s.pg_pool_max_size = 4
    s.neo4j_uri = "bolt://localhost:7687"
    s.neo4j_user = "u"
    s.neo4j_password = "p"
    return s


@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_wrong_password_does_not_advise_start(
    mock_settings_cls, mock_chunk_cls, mock_graph_cls, runner, recwarn
):
    """Дефект Д-6: контейнеры живы, совет reviewer start бессмыслен."""
    mock_settings_cls.return_value = _check_settings()
    mock_chunk_cls.side_effect = psycopg.OperationalError(
        'connection failed: FATAL:  password authentication failed for user "reviewer"')
    mock_graph_cls.return_value = MagicMock()

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 1
    assert "reviewer start" not in result.output
    assert "учётные данные" in result.output
    _assert_no_socket_warnings(recwarn)


@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_missing_database_is_not_reported_as_missing_schema(
    mock_settings_cls, mock_chunk_cls, mock_graph_cls, runner, recwarn
):
    """«does not exist» несут оба случая; несуществующая БД не лечится reviewer index."""
    mock_settings_cls.return_value = _check_settings()
    mock_chunk_cls.side_effect = psycopg.OperationalError(
        'connection failed: FATAL:  database "nosuchdb" does not exist')
    mock_graph_cls.return_value = MagicMock()

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 1
    assert "reviewer start" not in result.output
    assert "схема не инициализирована" not in result.output
    assert "базы данных не существует" in result.output
    _assert_no_socket_warnings(recwarn)


@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_stopped_containers_still_advise_start(
    mock_settings_cls, mock_chunk_cls, mock_graph_cls, runner, recwarn
):
    """Обратная сторона: настоящему простою совет по-прежнему выдаётся."""
    mock_settings_cls.return_value = _check_settings()
    mock_chunk_cls.side_effect = psycopg.OperationalError(
        "connection to server at 127.0.0.1, port 5433 failed: Connection refused")
    mock_graph_cls.return_value = MagicMock()

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 1
    assert "reviewer start" in result.output
    _assert_no_socket_warnings(recwarn)


@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_neo4j_auth_error_does_not_advise_start(
    mock_settings_cls, mock_chunk_cls, mock_graph_cls, runner, recwarn
):
    """Неверные креды Neo4j запуском контейнеров не лечатся (AuthError вне предиката)."""
    mock_settings_cls.return_value = _check_settings()
    mock_chunk_cls.return_value = MagicMock()
    graph = MagicMock()
    graph._driver.verify_connectivity.side_effect = Neo4jAuthError("unauthorized")
    mock_graph_cls.return_value = graph

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 1
    assert "reviewer start" not in result.output
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


# ---------------------------------------------------------------------------
# Версия CLI и честность help-текстов (PRI-236)
# ---------------------------------------------------------------------------


def test_version_option_prints_package_version(runner):
    """`reviewer --version` печатает версию дистрибутива и выходит с кодом 0.

    Источник — метаданные пакета, те же, что читает `versioning.detect_installation`,
    поэтому `--version` не может разойтись с логикой `reviewer update`.
    """
    from importlib import metadata

    result = runner.invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert metadata.version("rag-reviewer") in result.output
    assert "reviewer" in result.output


def test_config_show_help_does_not_promise_clone_path_from_index(runner):
    """Help `--path` не обещает путь из индекса: CLI его намеренно не читает.

    Проверка негативная: закреплять формулировку целиком значило бы ломать тест на
    любой редактуре, тогда как ловить надо ровно возврат ложного обещания
    (`_resolve_clone_path` берёт `--path`, иначе cwd, и в `repo_clone` не ходит).
    """
    result = runner.invoke(cli, ["config", "show", "--help"])

    assert result.exit_code == 0
    assert "из индекса" not in result.output


@patch("reviewer.entrypoints.cli._shutil")
@patch("reviewer.entrypoints.cli.httpx.get")
@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_fails_when_vector_roundtrip_broken(
    mock_settings_cls,
    mock_chunk_cls,
    mock_graph_cls,
    mock_httpx,
    mock_shutil,
    runner,
):
    """Несовместимость типа pgvector краснит check, а не всплывает в ревью.

    Приёмка 0.4.5 (PRI-236) поймала обратный случай: check был зелёным ровно
    тогда, когда prepare_review падал на каждом PR — подключение к Postgres
    проверялось, а совместимость типа вектора нет.
    """
    s = MagicMock()
    s.voyage_api_key = "key"
    s.github_token = "key"
    s.gitlab_token = ""
    s.pg_dsn = "pg://test"
    s.pg_pool_min_size = 1
    s.pg_pool_max_size = 4
    s.neo4j_uri = "neo4j://test"
    s.neo4j_user = "u"
    s.neo4j_password = "p"
    mock_settings_cls.return_value = s

    store = MagicMock()
    store._connect.return_value.__enter__.return_value.execute.return_value = None
    store.check_vector_roundtrip.side_effect = TypeError(
        "'Vector' object is not iterable"
    )
    mock_chunk_cls.return_value = store

    mock_graph_cls.return_value = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"login": "testuser"}
    mock_httpx.return_value = resp
    mock_shutil.which.return_value = "/usr/bin/scip-python"

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 1
    assert "pgvector" in result.output
    store.close.assert_called_once()


# ---------------------------------------------------------------------------
# Происхождение repo-тега: index fail-closed, status/migrate-branches fail-open
# (issue #190: нераспознанный origin молча подставлял чужое имя)
# ---------------------------------------------------------------------------


@patch("reviewer.entrypoints.cli.build_components")
@patch("reviewer.entrypoints.cli.remote_url")
@patch("reviewer.entrypoints.cli.Settings")
def test_index_refuses_repo_substituted_from_default(
    mock_settings_cls,
    mock_remote_url,
    mock_build,
    runner,
    fake_components,
    fake_settings,
):
    """Нераспознанный origin + DEFAULT_REPO → index падает, ничего не индексируя."""
    fake_settings.default_repo = "owner/default"
    mock_settings_cls.return_value = fake_settings
    mock_build.return_value = fake_components
    mock_remote_url.return_value = "ssh://tunnel/blocked"

    result = runner.invoke(cli, ["index", "/repo", "--ref", "main"])

    assert result.exit_code != 0
    assert "DEFAULT_REPO" in result.output
    assert "--repo" in result.output
    mock_build.assert_not_called()


@patch("reviewer.entrypoints.cli.build_code_graph")
@patch("reviewer.entrypoints.cli.rev_parse")
@patch("reviewer.entrypoints.cli.file_at_ref")
@patch("reviewer.entrypoints.cli.list_python_files")
@patch("reviewer.entrypoints.cli.update_base")
@patch("reviewer.entrypoints.cli.remote_url")
@patch("reviewer.entrypoints.cli.build_components")
@patch("reviewer.entrypoints.cli.Settings")
def test_index_allows_explicit_repo_despite_unrecognized_origin(
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
    monkeypatch,
    tmp_path,
):
    """Явный --repo — законный обход: origin не разбирается, но имя задано человеком."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    fake_settings.default_repo = "owner/default"
    mock_settings_cls.return_value = fake_settings
    mock_build.return_value = fake_components
    mock_remote_url.return_value = "ssh://tunnel/blocked"
    mock_list_files.return_value = []
    mock_rev_parse.return_value = "aaa0000"
    mock_file_at_ref.return_value = None
    mock_build_graph.return_value = ([], [], "treesitter")

    result = runner.invoke(cli, ["index", "/repo", "--ref", "main", "--repo", "a/x"])

    assert result.exit_code == 0, result.output
    mock_update_base.assert_called_once()


@patch("reviewer.entrypoints.cli.remote_url")
@patch("reviewer.entrypoints.cli.Settings")
def test_status_reports_repo_source_in_json(
    mock_settings_cls, mock_remote_url, runner, fake_settings, monkeypatch,
):
    """status fail-open: подставленное имя работает, но источник виден в JSON."""
    import json as _json

    fake_settings.default_repo = "owner/default"
    mock_settings_cls.return_value = fake_settings
    mock_remote_url.return_value = "ssh://tunnel/blocked"
    captured = {}

    def fake_build(*a, **k):
        captured.update(k)
        from reviewer.services.status import RepoStatus
        return RepoStatus(repo=a[2], branches=[], overlays=[],
                          repo_source=k.get("repo_source"))

    monkeypatch.setattr("reviewer.entrypoints.cli.build_status_report", fake_build)
    monkeypatch.setattr("reviewer.entrypoints.cli.ChunkStore", MagicMock())
    monkeypatch.setattr("reviewer.entrypoints.cli.GraphStore", MagicMock())
    monkeypatch.setattr("reviewer.entrypoints.cli.SummaryStore", MagicMock())

    result = runner.invoke(cli, ["status", "/repo", "--branch", "main", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["repo_source"] == "env:DEFAULT_REPO"
    assert _json.loads(result.output)["repo_source"] == "env:DEFAULT_REPO"


@patch("reviewer.entrypoints.cli.remote_url")
@patch("reviewer.entrypoints.cli.Settings")
def test_migrate_branches_warns_on_substituted_repo(
    mock_settings_cls, mock_remote_url, runner, fake_components, fake_settings,
    monkeypatch, tmp_path,
):
    """migrate-branches остаётся fail-open, но предупреждает о подстановке."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    fake_settings.default_repo = "owner/default"
    mock_settings_cls.return_value = fake_settings
    mock_remote_url.return_value = "ssh://tunnel/blocked"
    fake_components.store.migrate_legacy_base.return_value = 0
    monkeypatch.setattr("reviewer.entrypoints.cli.build_components",
                        MagicMock(return_value=fake_components))

    result = runner.invoke(cli, ["migrate-branches"])

    assert result.exit_code == 0, result.output
    assert "DEFAULT_REPO" in result.output


@patch("reviewer.entrypoints.cli._shutil")
@patch("reviewer.entrypoints.cli.httpx.get")
@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_success_does_not_print_password(
    mock_settings_cls,
    mock_chunk_cls,
    mock_graph_cls,
    mock_httpx,
    mock_shutil,
    runner,
):
    """Успешный путь check печатал полный DSN с паролем: вывод уходит в issue и на демо экрана.

    Проверяется обе половины размена: секрет не доходит до терминала, а хост,
    порт и имя базы доходят — иначе строка перестала бы отвечать на вопрос
    «куда я подключаюсь» и её незачем было бы печатать вовсе.
    """
    s = MagicMock()
    s.voyage_api_key = "key"
    s.github_token = "key"
    s.gitlab_token = ""
    s.pg_dsn = "postgresql://reviewer:s3cretpw@127.0.0.1:5433/reviewer"
    s.pg_pool_min_size = 1
    s.pg_pool_max_size = 4
    s.neo4j_uri = "bolt://neo:b0ltpw@localhost:7687"
    s.neo4j_user = "u"
    s.neo4j_password = "p"
    mock_settings_cls.return_value = s

    store = MagicMock()
    conn = store._connect.return_value.__enter__.return_value
    conn.execute.return_value = None
    mock_chunk_cls.return_value = store
    mock_graph_cls.return_value = MagicMock()

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"login": "testuser"}
    mock_httpx.return_value = resp
    mock_shutil.which.return_value = "/usr/bin/scip-python"

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 0
    assert "s3cretpw" not in result.output
    assert "b0ltpw" not in result.output
    assert "127.0.0.1:5433/reviewer" in result.output
    assert "localhost:7687" in result.output
