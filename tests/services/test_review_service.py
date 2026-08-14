"""Unit-тесты ReviewService.

Мокаем VCS-провайдер, Store, Graph-компоненты.
Проверяем prepare() — оркестрацию, cleanup, история.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock, call, patch

import pytest

from reviewer.config.settings import Settings
from reviewer.services.review_service import ReviewService
from reviewer.vcs.base import (
    ChangedFile,
    PullRequest,
)


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

@pytest.fixture
def settings() -> Settings:
    """Минимальные настройки для unit-тестов."""
    s = Settings()
    s.review_history = False
    s.review_skip_drafts = True
    s.review_max_files = 50
    s.voyage_api_key = "test"
    s.github_token = "test"
    # изолируем доску от локального .env целиком (настроенный TASK_BOARD_* в
    # ~/.config/rag-reviewer/.env иначе протекает в payload)
    s.task_board_type = ""
    s.task_board_mcp = ""
    s.task_board_key_pattern = ""
    s.task_board_url_template = ""
    # per-type креды: task_board_default() теперь выводит тип из кредов, а не из
    # task_board_type; зануляем, чтобы unconfigured-кейс работал верно
    s.yougile_api_key = ""
    s.task_board_api_key = ""
    s.youtrack_token = ""
    return s


@pytest.fixture
def components() -> MagicMock:
    """Фейковые компоненты (store, embedder, retriever, graph)."""
    c = MagicMock()
    c.store = MagicMock()
    c.embedder = MagicMock()
    c.retriever = MagicMock()
    c.graph = MagicMock()
    return c


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def _pr(draft: bool = False) -> PullRequest:
    return PullRequest(
        number=1,
        base_sha="base123",
        head_sha="head456",
        base_ref="main",
        title="Test PR",
        body="",
        draft=draft,
    )


def _changed(
    path: str = "a.py",
    status: str = "modified",
    patch: str = "@@ -1 +1 @@\n x",
) -> ChangedFile:
    return ChangedFile(path=path, status=status, patch=patch)


class _FakeChunk:
    """Фейковый чанк с node_id."""

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id


def _fake_chunk(path: str, source: bytes) -> list[_FakeChunk]:
    return [_FakeChunk(f"{path}#foo")]


def _vcs_with_files(files: list[ChangedFile], draft: bool = False) -> MagicMock:
    """Собрать мок VCS с заданными файлами PR."""
    vcs = MagicMock()
    vcs.get_pull_request.return_value = _pr(draft=draft)
    vcs.get_changed_files.return_value = files
    vcs.get_file_at_ref.return_value = "def foo(): pass"
    return vcs


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_routes_risk_files_without_indexing(
    build_overlay_mock: MagicMock,
    _chunk_python_mock: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Рисковые не-Python пути отдаются LLM без overlay/чанков."""
    files = [
        _changed("reviewer/app.py"),
        _changed("migrations/001.sql"),
        _changed("config/app.yaml"),
    ]
    vcs = _vcs_with_files(files)
    vcs.get_file_at_ref.side_effect = lambda path, ref: {
        ".review.yml": "",
        "reviewer/app.py": "def foo(): pass\n",
        "migrations/001.sql": "DROP TABLE old_data;\n",
        "config/app.yaml": "debug: false\n",
    }.get(path, "")

    prepared = ReviewService(settings, components).prepare(
        "owner", "repo", 1, vcs_provider=vcs
    )

    assert [u.path for u in prepared.units] == ["reviewer/app.py"]
    assert prepared.changed_paths == ["reviewer/app.py"]
    assert [item.path for item in prepared.risk_paths] == ["migrations/001.sql"]
    assert prepared.sources["migrations/001.sql"] == "DROP TABLE old_data;\n"
    assert build_overlay_mock.call_args.args[4] == ["reviewer/app.py"]


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_keeps_risk_item_when_its_head_source_cannot_be_loaded(
    build_overlay_mock: MagicMock,
    _chunk_python_mock: MagicMock,
    settings: Settings,
    components: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сбой чтения risk-source не отменяет подготовку Python-ревью."""
    risk_patch = "@@ -1 +1 @@\n-SELECT 1;\n+DROP TABLE old_data;"
    files = [
        _changed("reviewer/app.py", status="added"),
        _changed("migrations/001.sql", patch=risk_patch),
    ]
    vcs = _vcs_with_files(files)

    def read_file(path: str, ref: str) -> str:
        if path == "migrations/001.sql":
            raise RuntimeError("provider response with secret-content")
        return {
            ".review.yml": "",
            "reviewer/app.py": "def foo(): pass\n",
        }.get(path, "")

    vcs.get_file_at_ref.side_effect = read_file

    prepared = ReviewService(settings, components).prepare(
        "owner", "repo", 1, vcs_provider=vcs
    )

    assert [unit.path for unit in prepared.units] == ["reviewer/app.py"]
    assert [item.path for item in prepared.risk_paths] == ["migrations/001.sql"]
    assert prepared.patches["migrations/001.sql"] == risk_patch
    assert "migrations/001.sql" not in prepared.sources
    assert build_overlay_mock.call_args.args[4] == ["reviewer/app.py"]
    assert "migrations/001.sql" in caplog.text
    assert "secret-content" not in caplog.text


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_limits_risk_paths_and_reports_overflow(
    _build_overlay_mock: MagicMock,
    _chunk_python_mock: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Одиннадцатый risk path остаётся в детерминированном overflow."""
    files = [_changed(f".env.{number}") for number in range(11)]
    vcs = _vcs_with_files(files)

    prepared = ReviewService(settings, components).prepare(
        "owner", "repo", 1, vcs_provider=vcs
    )

    assert len(prepared.risk_paths) == 10
    assert prepared.risk_skipped_paths == [".env.9"]


def test_prepare_module_isolated_from_conflicting_xdg_config(tmp_path):
    """Prepare-тесты не должны наследовать repository-home policy разработчика."""
    home = tmp_path / "developer-config/rag-reviewer/repos/o/r.yml"
    home.parent.mkdir(parents=True)
    home.write_text("task_board: {type: yougile}\n", encoding="utf-8")
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "developer-config")}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/services/test_review_service.py::test_prepare_task_keys_none_without_task_board",
        ],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Тесты: внешний vcs_provider (eval) не трогает прод base-индекс
# ---------------------------------------------------------------------------

@patch("reviewer.services.review_service.update_base")
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_custom_vcs_provider_does_not_touch_base_index(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    mock_update_base: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """eval-прогон (передан внешний vcs_provider) не синхронизирует и не затирает
    base-индекс: снапшот с base_sha='base' не должен подменять прод-SHA/чанки."""
    components.store.get_index_meta.return_value = "realsha999"   # есть прод-индекс
    vcs = _vcs_with_files([_changed("a.py")])
    vcs.get_pull_request.return_value = PullRequest(
        number=1, base_sha="base", head_sha="head", base_ref="main",
        title="Eval", body="", draft=False,
    )

    service = ReviewService(settings, components)
    service.prepare("owner", "repo", 1, vcs_provider=vcs)

    mock_update_base.assert_not_called()
    components.store.set_index_meta.assert_not_called()


# ---------------------------------------------------------------------------
# Тест: нормализация repo
# ---------------------------------------------------------------------------

@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_sets_normalized_repo(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """prepare() вычисляет repo = normalize_repo(owner/name) и прокидывает в PreparedReview."""
    vcs = _vcs_with_files([_changed("a.py")])
    service = ReviewService(settings, components)
    prepared = service.prepare("Owner", "Repo", 1, vcs_provider=vcs)

    assert prepared.repo == "owner/repo"


# ---------------------------------------------------------------------------
# Тесты prepare()
# ---------------------------------------------------------------------------

@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_returns_units_policy_and_overlay(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """prepare() собирает юниты, policy и overlay без запуска LLM."""
    vcs = _vcs_with_files([_changed("a.py")])
    vcs.get_pull_request.return_value = PullRequest(
        number=7, base_sha="base123", head_sha="head456", base_ref="main",
        title="Test PR", body="", draft=False,
    )

    service = ReviewService(settings, components)
    prepared = service.prepare("o", "r", 7, vcs_provider=vcs)

    assert prepared.prq.number == 7
    assert prepared.overlay_ref == "pr:7"
    assert [u.path for u in prepared.units] == ["a.py"]
    assert prepared.patches["a.py"] is not None
    assert prepared.policy.max_comments > 0
    assert prepared.changed_paths == ["a.py"]
    # остальные поля PreparedReview заполнены реальными данными
    assert prepared.sources == {"a.py": "def foo(): pass"}
    assert prepared.changed_node_ids == ["a.py#foo"]
    assert prepared.skipped_paths == []
    assert prepared.changed_status == {"a.py": "modified"}
    assert prepared.vcs is vcs
    # self-healing: старый overlay удалён в начале prepare()
    components.store.delete_ref.assert_called_once_with("o/r", "pr:7")


@patch("reviewer.services.review_service.update_base")
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_runs_base_sync_for_real_review(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    mock_update_base: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """prepare() без внешнего vcs_provider (прод-ревью) синхронизирует base-индекс,
    когда SHA индекса разошёлся с base_sha PR."""
    components.store.get_index_meta.return_value = "oldsha000"   # индекс устарел
    vcs = _vcs_with_files([_changed("a.py")])
    vcs.compare_files.return_value = [_changed("b.py")]

    service = ReviewService(settings, components)
    with patch.object(service, "_create_vcs_provider", return_value=vcs):
        service.prepare("owner", "repo", 1)   # vcs_provider не передан → прод-путь

    mock_update_base.assert_called_once()
    components.store.set_index_meta.assert_called_once_with("owner/repo", "base:main", "base123")


def test_prepare_closes_internal_vcs_on_failure(
    settings: Settings, components: MagicMock,
) -> None:
    """При сбое prepare() внутренне созданный VCS-провайдер закрывается —
    иначе httpx-клиент утёк бы (критично для долгоживущего MCP-сервера)."""
    vcs = _vcs_with_files([_changed("a.py")])
    vcs.get_changed_files.side_effect = RuntimeError("api down")

    service = ReviewService(settings, components)
    with patch.object(service, "_create_vcs_provider", return_value=vcs):
        with pytest.raises(RuntimeError, match="api down"):
            service.prepare("owner", "repo", 1)   # vcs_provider не передан

    vcs.close.assert_called_once()
    # overlay вычищен и при сбое: self-healing в начале + cleanup в except
    assert components.store.delete_ref.call_args_list == [
        call("owner/repo", "pr:1"), call("owner/repo", "pr:1"),
    ]


def test_prepare_failure_keeps_original_error_when_close_fails(
    settings: Settings, components: MagicMock,
) -> None:
    """Сбой самого vcs.close() fail-soft: наружу выходит ИСХОДНОЕ исключение
    подготовки, а не ошибка закрытия провайдера."""
    vcs = _vcs_with_files([_changed("a.py")])
    vcs.get_changed_files.side_effect = RuntimeError("api down")
    vcs.close.side_effect = RuntimeError("close failed")

    service = ReviewService(settings, components)
    with patch.object(service, "_create_vcs_provider", return_value=vcs):
        with pytest.raises(RuntimeError, match="api down"):
            service.prepare("owner", "repo", 1)


def test_prepare_does_not_close_external_vcs_on_failure(
    settings: Settings, components: MagicMock,
) -> None:
    """Переданный снаружи vcs_provider при сбое prepare() НЕ закрывается —
    его жизненным циклом управляет вызывающий."""
    vcs = _vcs_with_files([_changed("a.py")])
    vcs.get_changed_files.side_effect = RuntimeError("api down")

    service = ReviewService(settings, components)
    with pytest.raises(RuntimeError, match="api down"):
        service.prepare("owner", "repo", 1, vcs_provider=vcs)

    vcs.close.assert_not_called()


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_extracts_task_keys_when_task_board_configured(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """При task_board в .review.yml prepare извлекает primary-ключ из title/branch."""
    vcs = _vcs_with_files([_changed("a.py")])
    vcs.get_pull_request.return_value = PullRequest(
        number=3, base_sha="b", head_sha="h", base_ref="main",
        title="SAI-515: add logout", body="", draft=False,
        head_ref="feature/SAI-515",
    )

    def _read(path: str, ref: str) -> str:
        if path == ".review.yml":
            return "task_board: {type: yougile, mcp: yougile}"
        return "def foo(): pass"
    vcs.get_file_at_ref.side_effect = _read

    service = ReviewService(settings, components)
    prepared = service.prepare("o", "r", 3, vcs_provider=vcs)

    assert prepared.task_board == {"type": "yougile", "mcp": "yougile"}
    assert prepared.task_keys == {"primary": "SAI-515", "others": []}


@patch("reviewer.services.review_service.update_base")
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_patches_graph_incrementally_on_drift(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    _mock_update_base: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """При дрейфе SHA base-индекса prepare() вызывает инкрементальный патч графа
    (patch_graph_incremental) для repo-aware self-heal графа кода."""
    components.store.get_index_meta.return_value = "oldsha000"   # индекс устарел

    vcs = _vcs_with_files([_changed("a.py")])
    # compare_files возвращает один изменённый .py-файл
    vcs.compare_files.return_value = [_changed("b.py", status="modified")]
    vcs.get_file_at_ref.return_value = "def baz(): pass"

    # Фейковый граф, фиксирующий вызовы
    class _FakeGraph:
        def __init__(self):
            self.symbols_calls = []
            self.upsert_nodes_calls = []
            self.delete_outgoing_calls_log = []

        def symbols_for_paths(self, repo, paths, *, branch=""):
            self.symbols_calls.append((repo, list(paths)))
            return set()

        def all_node_ids(self, repo, *, branch=""):
            return set()

        def delete_symbols(self, repo, ids, *, branch=""):
            pass

        def delete_outgoing_calls(self, repo, ids, *, branch=""):
            self.delete_outgoing_calls_log.append((repo, list(ids)))

        def delete_outgoing_implements(self, repo, ids, *, branch=""):
            pass

        def upsert_nodes(self, repo, ids, *, branch=""):
            self.upsert_nodes_calls.append((repo, list(ids)))

        def upsert_edges(self, repo, edges, *, branch=""):
            pass

    fake_graph = _FakeGraph()
    components.graph = fake_graph

    service = ReviewService(settings, components)
    with patch.object(service, "_create_vcs_provider", return_value=vcs):
        service.prepare("owner", "repo", 1)

    # Изменённый surface должен быть переустановлен: узлы переписаны, исходящие
    # CALLS сброшены (входящие сохраняются). Конкретная проверка, а не "или".
    assert fake_graph.upsert_nodes_calls, (
        "patch_graph_incremental не вызвал upsert_nodes при дрейфе SHA"
    )
    repo_seen, ids = fake_graph.upsert_nodes_calls[0]
    assert repo_seen == "owner/repo" and "b.py#baz" in ids
    assert fake_graph.delete_outgoing_calls_log, (
        "не сброшены исходящие CALLS изменённой поверхности"
    )


@patch("reviewer.services.review_service.update_base")
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_graph_patch_fail_soft_does_not_abort_prepare(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    _mock_update_base: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Сбой патча графа (fail-soft) не прерывает prepare() и не ломает vector-sync."""
    components.store.get_index_meta.return_value = "oldsha000"

    vcs = _vcs_with_files([_changed("a.py")])
    vcs.compare_files.return_value = [_changed("b.py")]
    vcs.get_file_at_ref.return_value = "def baz(): pass"

    # Граф, чей метод бросает исключение
    broken_graph = MagicMock()
    broken_graph.symbols_for_paths.side_effect = RuntimeError("neo4j down")
    components.graph = broken_graph

    service = ReviewService(settings, components)
    with patch.object(service, "_create_vcs_provider", return_value=vcs):
        # prepare должен завершиться успешно, несмотря на сбой графа
        prepared = service.prepare("owner", "repo", 1)

    assert prepared is not None
    # vector-sync всё же выполнился
    _mock_update_base.assert_called_once()
    components.store.set_index_meta.assert_called_once_with("owner/repo", "base:main", "base123")


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_task_keys_none_without_task_board(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Без task_board контекст задачи неактивен: оба поля None."""
    vcs = _vcs_with_files([_changed("a.py")])
    service = ReviewService(settings, components)
    prepared = service.prepare("o", "r", 1, vcs_provider=vcs)

    assert prepared.task_board is None
    assert prepared.task_keys is None


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_attaches_structural_summary_for_modified_file(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Для изменённого на месте файла со сменой сигнатуры prepare кладёт
    непустой structural_summary в юнит."""
    vcs = _vcs_with_files([_changed("a.py", status="modified")])
    vcs.get_pull_request.return_value = PullRequest(
        number=1, base_sha="base123", head_sha="head456", base_ref="main",
        title="t", body="", draft=False,
    )

    def _read(path: str, ref: str) -> str | None:
        if path == ".review.yml":
            return None
        return "def foo(a, b):\n    return a" if ref == "head456" else "def foo(a):\n    return a"
    vcs.get_file_at_ref.side_effect = _read

    service = ReviewService(settings, components)
    prepared = service.prepare("o", "r", 1, vcs_provider=vcs)

    summary = prepared.units[0].structural_summary
    assert "foo" in summary
    assert "сигнатура" in summary


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_no_structural_summary_for_added_file(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Для добавленного файла structural_summary пуст (для нового файла сводка — шум)."""
    vcs = _vcs_with_files([_changed("a.py", status="added")])
    vcs.get_file_at_ref.side_effect = (
        lambda p, r: None if p == ".review.yml" else "def foo(a): pass"
    )

    service = ReviewService(settings, components)
    prepared = service.prepare("o", "r", 1, vcs_provider=vcs)

    assert prepared.units[0].structural_summary == ""


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_structural_summary_fail_soft_on_base_fetch_error(
    _mock_overlay, _mock_chunk, settings, components,
):
    """Сбой получения base-исходника не валит prepare; structural_summary == ""."""
    vcs = _vcs_with_files([_changed("a.py", status="modified")])
    vcs.get_pull_request.return_value = PullRequest(
        number=1, base_sha="base123", head_sha="head456", base_ref="main",
        title="t", body="", draft=False,
    )

    def _read(path, ref):
        if path == ".review.yml":
            return None
        if ref == "base123":
            raise RuntimeError("base fetch failed")
        return "def foo(a): pass"

    vcs.get_file_at_ref.side_effect = _read
    service = ReviewService(settings, components)
    prepared = service.prepare("o", "r", 1, vcs_provider=vcs)
    assert prepared.units[0].structural_summary == ""


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_no_structural_summary_for_renamed_file(
    _mock_overlay, _mock_chunk, settings, components,
):
    """Для переименованного файла structural_summary пуст (политика modified-only)."""
    vcs = _vcs_with_files([_changed("a.py", status="renamed")])
    vcs.get_file_at_ref.side_effect = (
        lambda p, r: None if p == ".review.yml" else "def foo(a): pass"
    )
    service = ReviewService(settings, components)
    prepared = service.prepare("o", "r", 1, vcs_provider=vcs)
    assert prepared.units[0].structural_summary == ""


# ---------------------------------------------------------------------------
# Тест: инкрементальный граф-синк применяет paths.ignore (parity чанки↔граф)
# ---------------------------------------------------------------------------

@patch("reviewer.services.review_service.update_base")
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_graph_sync_respects_ignore(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    _mock_update_base: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Граф-досинк применяет paths.ignore из .review.yml: игнорируемые пути
    не попадают в changed_sources, но добавляются в removed_paths (parity с чанками).

    Воспроизводит баг: changed_py строился без фильтрации по is_ignored, из-за
    чего символы vendor/ уходили в Neo4j, хотя чанков для них в Postgres нет.
    """
    components.store.get_index_meta.return_value = "oldsha000"   # индекс устарел

    # .review.yml задаёт paths.ignore = [vendor]
    def _read(path: str, ref: str) -> str:
        if path == ".review.yml":
            return "paths:\n  ignore:\n    - vendor\n"
        return "def func(): pass"

    vcs = _vcs_with_files([_changed("reviewer/a.py")])
    vcs.compare_files.return_value = [
        _changed("vendor/x.py", status="modified"),   # должен быть проигнорирован
        _changed("reviewer/a.py", status="modified"),  # должен пройти
    ]
    vcs.get_file_at_ref.side_effect = _read

    # Фейковый граф, фиксирующий аргументы вызовов
    class _FakeGraph:
        def __init__(self):
            self.captured_changed: dict[str, str] = {}
            self.captured_removed: list[str] = []
            self.upsert_nodes_calls: list[tuple[str, list[str]]] = []

        def symbols_for_paths(self, repo, paths, *, branch=""):
            return set()

        def all_node_ids(self, repo, *, branch=""):
            return set()

        def delete_symbols(self, repo, ids, *, branch=""):
            pass

        def delete_outgoing_calls(self, repo, ids, *, branch=""):
            pass

        def delete_outgoing_implements(self, repo, ids, *, branch=""):
            pass

        def upsert_nodes(self, repo, ids, *, branch=""):
            self.upsert_nodes_calls.append((repo, list(ids)))

        def upsert_edges(self, repo, edges, *, branch=""):
            pass

    fake_graph = _FakeGraph()
    components.graph = fake_graph

    # Перехватываем вызов patch_graph_incremental, чтобы проверить аргументы
    captured: dict[str, object] = {}

    def _fake_patch(graph, repo, branch="", *, changed_sources, removed_paths):
        captured["changed_sources"] = dict(changed_sources)
        captured["removed_paths"] = list(removed_paths)
        # Делегируем реальному графу для полноты
        for path, src in changed_sources.items():
            nids = [f"{path}#func"]
            fake_graph.upsert_nodes(repo, nids, branch=branch)

    service = ReviewService(settings, components)
    with (
        patch.object(service, "_create_vcs_provider", return_value=vcs),
        # patch_graph_incremental импортируется внутри ветки — патчим по месту определения
        patch("reviewer.services.graph_sync.patch_graph_incremental", _fake_patch),
    ):
        service.prepare("owner", "repo", 1)

    # vendor/x.py не должен попасть в changed_sources (parity с чанками)
    assert "vendor/x.py" not in captured.get("changed_sources", {}), (
        "игнорируемый путь vendor/x.py попал в changed_sources графа"
    )
    # reviewer/a.py должен пройти
    assert "reviewer/a.py" in captured.get("changed_sources", {}), (
        "незаигнорированный путь reviewer/a.py пропущен"
    )
    # vendor/x.py должен быть в removed_paths (символы удаляются из графа, как из чанков)
    assert "vendor/x.py" in captured.get("removed_paths", []), (
        "игнорируемый путь vendor/x.py не добавлен в removed_paths"
    )


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_is_loud_when_committed_policy_cannot_be_read(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """PRI-234: тихая потеря политики в ревью недопустима — prepare падает."""
    from reviewer.config.layers import HomeConfigError

    vcs = _vcs_with_files([_changed("a.py")])

    def fetch(path, ref):
        if path == ".review.yml":
            raise RuntimeError("сеть недоступна")
        return "def foo(): pass"

    vcs.get_file_at_ref.side_effect = fetch
    service = ReviewService(settings, components)

    with pytest.raises(HomeConfigError):
        service.prepare("owner", "repo", 1, vcs_provider=vcs)
