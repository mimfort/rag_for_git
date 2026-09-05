"""Unit-тесты MCPReviewService.

Мокаем VCS-провайдер, Store, Graph, LLM-компоненты.
Проверяем prepare_review, кэш сессий, валидацию и cleanup.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psycopg_pool
import pytest

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
from reviewer.policy.context_limits import CodebaseLimits
from reviewer.retrieval.augment import AugmentResult
from reviewer.tasks.store import TaskHit
from reviewer.vcs.base import (
    ChangedFile,
    PullRequest,
)


# ---------------------------------------------------------------------------
# Хелперы (по образцу tests/services/test_review_service.py)
# ---------------------------------------------------------------------------

def _settings() -> Settings:
    s = Settings()
    s.review_history = False
    s.review_skip_drafts = True
    s.review_max_files = 50
    s.voyage_api_key = "test"
    s.github_token = "test"
    s.review_session_persist = False     # unit-тесты не трогают Postgres-таблицу сессий
    s.default_repo = ""                  # изолируем от локального .env (DEFAULT_REPO)
    # изолируем доску от локального .env целиком (иначе настроенный TASK_BOARD_*
    # в ~/.config/rag-reviewer/.env протекает в payload и ломает unconfigured-кейс)
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


def _components() -> MagicMock:
    c = MagicMock()
    c.store = MagicMock()
    c.embedder = MagicMock()
    # retriever.retrieve().as_context() должен возвращать str — иначе isinstance-проверки
    # в тестах инструментов поиска провалятся (MagicMock не str)
    c.retriever = MagicMock()
    c.retriever.retrieve.return_value.as_context.return_value = "(результат поиска)"
    c.graph = MagicMock()
    c.graph.expand.return_value = set()
    c.graph.callers.return_value = set()
    c.graph.find_symbol.return_value = []
    c.graph.expand_detailed.return_value = []
    c.graph.callers_detailed.return_value = []
    c.graph.implementations_detailed.return_value = []
    c.llm_provider = MagicMock()
    return c


def _pr(number: int = 7) -> PullRequest:
    return PullRequest(
        number=number,
        base_sha="base123",
        head_sha="head456",
        base_ref="main",
        title="Test PR",
        body="",
        draft=False,
    )


def _changed(
    path: str = "a.py",
    status: str = "modified",
    patch: str = "@@ -1 +1 @@\n x",
) -> ChangedFile:
    return ChangedFile(path=path, status=status, patch=patch)


class _FakeChunk:
    def __init__(self, node_id: str) -> None:
        self.node_id = node_id


def _fake_chunk(path: str, source: bytes) -> list[_FakeChunk]:
    return [_FakeChunk(f"{path}#foo")]


def _fake_vcs(number: int = 7) -> MagicMock:
    vcs = MagicMock()
    vcs.get_pull_request.return_value = _pr(number=number)
    vcs.get_changed_files.return_value = [_changed()]
    vcs.get_file_at_ref.return_value = "def foo(): pass"
    return vcs


def _make_mcp_service(number: int = 7) -> MCPReviewService:
    """Фабрика MCPReviewService с фейковыми компонентами."""
    settings = _settings()
    components = _components()
    fake_vcs = _fake_vcs(number=number)
    return MCPReviewService(
        settings,
        components,
        vcs_factory=lambda o, r: fake_vcs,
    )


# ---------------------------------------------------------------------------
# Тесты prepare_review
# ---------------------------------------------------------------------------

@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_sets_session_started_at(
    _ov: MagicMock,
    _ch: MagicMock,
) -> None:
    svc = _make_mcp_service()
    before = datetime.now(timezone.utc)
    svc.prepare_review("o/r", 7)
    after = datetime.now(timezone.utc)
    s = svc._sessions[("o/r", 7)]
    assert s.started_at is not None
    assert isinstance(s.started_at, datetime)
    assert s.started_at.tzinfo == timezone.utc
    assert before <= s.started_at <= after


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_returns_units_and_caches_session(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """prepare_review возвращает pr/policy/units и кэширует сессию."""
    svc = _make_mcp_service()
    out = svc.prepare_review("o/r", 7)

    assert out["pr"]["number"] == 7
    assert out["policy"]["max_comments"] > 0
    assert out["policy"]["output_language"] == "ru"
    unit = out["units"][0]
    assert unit["path"] == "a.py"
    assert isinstance(unit["commentable_right"], list)
    assert ("o/r", 7) in svc._sessions


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_payload_fields(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """Проверяем ключи pr/policy/units/skipped_paths/suggestions_mode."""
    svc = _make_mcp_service()
    out = svc.prepare_review("o/r", 7)

    # pr-блок
    pr = out["pr"]
    assert pr["title"] == "Test PR"
    assert pr["base_sha"] == "base123"
    assert pr["head_sha"] == "head456"
    assert pr["base_ref"] == "main"
    assert pr["draft"] is False

    # policy-блок
    policy = out["policy"]
    assert "severity_threshold" in policy
    assert "min_confidence" in policy
    assert "categories" in policy
    assert "ignore" in policy

    # meta
    assert "skipped_paths" in out
    assert "skip_drafts" in out
    assert "suggestions_mode" in out


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_includes_commentable_risk_path_payload(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """Migration diff is exposed with its reason and commentable lines."""
    svc = _make_mcp_service()
    vcs = svc._vcs_factory("o", "r")
    vcs.get_changed_files.return_value = [
        _changed("migrations/001.sql", patch="@@ -1 +1 @@\n-old\n+new"),
    ]
    vcs.get_file_at_ref.side_effect = lambda path, ref: {
        ".review.yml": "",
        "migrations/001.sql": "SELECT 1;\n",
    }.get(path, "")

    out = svc.prepare_review("o/r", 7)

    risk = out["risk_paths"][0]
    assert risk["path"] == "migrations/001.sql"
    assert risk["status"] == "modified"
    assert risk["reasons"] == ["migration"]
    assert risk["patch"].startswith("@@")
    assert isinstance(risk["commentable_right"], list)
    assert isinstance(risk["commentable_left"], list)
    assert out["risk_skipped_paths"] == []


# ---------------------------------------------------------------------------
# Тест: поиск без prepare бросает понятную ошибку
# ---------------------------------------------------------------------------

def test_search_without_prepare_raises_clear_error() -> None:
    """search_code без prepare_review бросает ValueError с упоминанием prepare_review."""
    svc = _make_mcp_service()
    with pytest.raises(ValueError, match="prepare_review"):
        svc.search_code("o/r", 7, "query")


# ---------------------------------------------------------------------------
# Тест: повторный prepare_review не течёт httpx-клиентом
# ---------------------------------------------------------------------------

@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_repeated_does_not_close_external_vcs(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """Повторный prepare_review для того же PR при vcs_factory (внешний vcs)
    НЕ закрывает vcs — жизненным циклом управляет фабрика/вызывающий.
    Сессия обновляется."""
    settings = _settings()
    components = _components()

    vcs1 = _fake_vcs()
    vcs2 = _fake_vcs()
    calls = []

    def factory(o, r):
        if not calls:
            calls.append(vcs1)
            return vcs1
        calls.append(vcs2)
        return vcs2

    svc = MCPReviewService(settings, components, vcs_factory=factory)
    svc.prepare_review("o/r", 7)
    svc.prepare_review("o/r", 7)

    # Внешние vcs (через фабрику) НЕ закрываются сервисом
    vcs1.close.assert_not_called()
    vcs2.close.assert_not_called()
    # Сессия всё ещё существует
    assert ("o/r", 7) in svc._sessions


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_repeated_closes_old_internal_vcs(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """Повторный prepare_review для того же PR БЕЗ vcs_factory (внутренний vcs)
    fail-soft закрывает провайдер СТАРОЙ сессии — иначе утечка httpx-клиента
    в долгоживущем сервере. Новый провайдер остаётся открытым в новой сессии."""
    settings = _settings()
    components = _components()
    vcs1 = _fake_vcs()
    vcs2 = _fake_vcs()

    svc = MCPReviewService(settings, components, vcs_factory=None)
    with patch.object(
        svc._review_service, "_create_vcs_provider", side_effect=[vcs1, vcs2],
    ):
        svc.prepare_review("o/r", 7)
        svc.prepare_review("o/r", 7)

    vcs1.close.assert_called_once()
    vcs2.close.assert_not_called()
    assert ("o/r", 7) in svc._sessions


# ---------------------------------------------------------------------------
# Тест Task 5: делегаты инструментов поиска (паритет с tool-loop агента)
# ---------------------------------------------------------------------------

@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_search_tools_delegate_to_make_tools(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """Все шесть инструментов поиска возвращают str после prepare_review."""
    svc = _make_mcp_service()
    svc.prepare_review("o/r", 7)
    assert isinstance(svc.search_code("o/r", 7, "token check"), str)
    assert isinstance(svc.get_related_symbols("o/r", 7, "a.py#f"), str)
    assert isinstance(svc.read_file("o/r", 7, "a.py", 1, 10), str)
    assert isinstance(svc.read_file("o/r", 7, "a.py", 1, 10, skeleton=True), str)
    assert isinstance(svc.get_definition("o/r", 7, "f"), str)
    assert isinstance(svc.find_callers("o/r", 7, "a.py#f"), str)
    assert isinstance(svc.get_changed_file_diff("o/r", 7, "a.py"), str)


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_payload_contains_enabled_only(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """При непустом policy.enabled_only payload prepare_review содержит его в policy."""
    from reviewer.policy.policy import ReviewPolicy

    svc = _make_mcp_service()
    # Подменяем политику: задаём непустой enabled_only вайтлист
    original_from_settings = ReviewPolicy.from_settings

    def patched_from_settings(settings):
        p = original_from_settings(settings)
        p.enabled_only = ["correctness", "security"]
        return p

    with patch(
        "reviewer.services.review_service.ReviewPolicy",
        wraps=ReviewPolicy,
    ) as mock_policy_cls:
        mock_policy_cls.from_settings.side_effect = patched_from_settings
        mock_policy_cls.from_yaml.side_effect = ReviewPolicy.from_yaml
        out = svc.prepare_review("o/r", 7)

    # enabled_only должен присутствовать в payload policy
    assert "enabled_only" in out["policy"], (
        "payload prepare_review должен содержать policy.enabled_only"
    )


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_payload_enabled_only_default_empty(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """По умолчанию policy.enabled_only — пустой список (без вайтлиста)."""
    svc = _make_mcp_service()
    out = svc.prepare_review("o/r", 7)
    assert out["policy"]["enabled_only"] == [], (
        "enabled_only по умолчанию должен быть пустым списком"
    )


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_invoke_tool_logs_steps(_ov, _ch) -> None:
    svc = _make_mcp_service()
    svc.prepare_review("o/r", 7)
    svc.search_code("o/r", 7, "token check")
    s = svc._sessions[("o/r", 7)]
    assert len(s.steps) >= 1
    assert s.steps[0]["name"] == "search_code"
    assert s.steps[0]["kind"] == "tool_call"
    assert s.steps[0]["stage"] == "analyze"


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_search_code_repeated_call_returns_result_not_stub(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """Два одинаковых вызова search_code подряд: второй возвращает результат,
    а не заглушку '(повтор: результат уже показан выше)'.

    Это проверяет, что seen-дедуп сбрасывается между вызовами (_invoke_tool
    пересоздаёт make_tools каждый раз), а ctx.cache отдаёт реальный результат.
    """
    from reviewer.tools.code_tools import _DUP_STUB

    svc = _make_mcp_service()
    svc.prepare_review("o/r", 7)

    result1 = svc.search_code("o/r", 7, "token check")
    result2 = svc.search_code("o/r", 7, "token check")

    # Второй вызов НЕ должен быть заглушкой
    assert result2 != _DUP_STUB, (
        "Второй вызов search_code вернул заглушку seen-дедупа вместо результата"
    )
    # Оба вызова возвращают одинаковый результат (из cache)
    assert result1 == result2, (
        f"Ожидали одинаковые результаты, получили:\n  result1={result1!r}\n  result2={result2!r}"
    )
    # Реальный cache-hit: источник дёрнут ровно один раз (prepare_review retriever
    # не трогает) — без этого ассерта регрессия «кэш живёт сессию» не ловится
    assert svc.components.retriever.retrieve.call_count == 1


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_payload_includes_task_context(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """payload содержит task_board и извлечённые task_keys."""
    settings = _settings()
    components = _components()
    vcs = _fake_vcs(number=7)
    vcs.get_pull_request.return_value = PullRequest(
        number=7, base_sha="base123", head_sha="head456", base_ref="main",
        title="SAI-515: add logout", body="", draft=False, head_ref="feature/SAI-515",
    )

    def _read(path: str, ref: str) -> str:
        if path == ".review.yml":
            return "task_board: {type: yougile, mcp: yougile}"
        return "def foo(): pass"
    vcs.get_file_at_ref.side_effect = _read

    svc = MCPReviewService(settings, components, vcs_factory=lambda o, r: vcs)
    out = svc.prepare_review("o/r", 7)

    assert out["task_board"] == {"type": "yougile", "mcp": "yougile"}
    assert out["task_keys"] == {"primary": "SAI-515", "others": []}


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_hands_generic_task_board_values_to_mcp(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """Конфиг политики передаётся клиенту MCP только как target/options."""
    settings = _settings()
    components = _components()
    vcs = _fake_vcs(number=7)

    def _read(path: str, ref: str) -> str:
        if path == ".review.yml":
            return """
task_board:
  type: youtrack
  create_target: Open
  done_target: Done
  options: {status_field: Stage}
"""
        return "def foo(): pass"
    vcs.get_file_at_ref.side_effect = _read

    svc = MCPReviewService(settings, components, vcs_factory=lambda o, r: vcs)
    out = svc.prepare_review("o/r", 7)

    assert out["task_board"] == {
        "type": "youtrack",
        "create_target": "Open",
        "done_target": "Done",
        "options": {"status_field": "Stage"},
    }
    assert out["task_board_warnings"] == []
    assert svc._sessions[("o/r", 7)].prepared.policy.task_board_warnings == []


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_returns_legacy_task_board_warnings_once(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """Migration warnings are public metadata, separate from generic board options."""
    settings = _settings()
    components = _components()
    vcs = _fake_vcs(number=7)

    def _read(path: str, ref: str) -> str:
        if path == ".review.yml":
            return """
task_board:
  type: youtrack
  done_state: Fixed
  status_field: Stage
"""
        return "def foo(): pass"
    vcs.get_file_at_ref.side_effect = _read

    svc = MCPReviewService(settings, components, vcs_factory=lambda o, r: vcs)
    out = svc.prepare_review("o/r", 7)

    assert out["task_board"] == {
        "type": "youtrack",
        "done_target": "Fixed",
        "options": {"status_field": "Stage"},
    }
    assert out["task_board_warnings"] == [
        "task_board.done_state migrated to done_target",
        "task_board.status_field migrated to options.status_field",
    ]
    assert out["task_board_warnings"] == (
        svc._sessions[("o/r", 7)].prepared.policy.task_board_warnings
    )


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_payload_task_keys_empty_when_no_key_in_pr(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """task_board задан, но в PR нет ключа → task_keys пустой (не None)."""
    settings = _settings()
    components = _components()
    vcs = _fake_vcs(number=7)
    vcs.get_pull_request.return_value = PullRequest(
        number=7, base_sha="base123", head_sha="head456", base_ref="main",
        title="no key here", body="", draft=False, head_ref="feature/cleanup",
    )

    def _read(path: str, ref: str) -> str:
        if path == ".review.yml":
            return "task_board: {type: yougile, mcp: yougile}"
        return "def foo(): pass"
    vcs.get_file_at_ref.side_effect = _read

    svc = MCPReviewService(settings, components, vcs_factory=lambda o, r: vcs)
    out = svc.prepare_review("o/r", 7)

    assert out["task_board"] == {"type": "yougile", "mcp": "yougile"}
    assert out["task_keys"] == {"primary": None, "others": []}


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_payload_task_context_null_when_unconfigured(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """Без task_board оба поля payload — null."""
    svc = _make_mcp_service()
    out = svc.prepare_review("o/r", 7)

    assert out["task_board"] is None
    assert out["task_keys"] is None


# ---------------------------------------------------------------------------
# Тесты Task 6: index_task/search_tasks/get_task_context + авто-линковка PR
# ---------------------------------------------------------------------------

@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_review_links_task_when_task_key(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """publish_review с task_key линкует PR↔задача↔код через task_service.link_review."""
    settings = _settings()
    components = _components()
    vcs = _fake_vcs(number=7)
    vcs.list_existing_fingerprints.return_value = set()
    svc = MCPReviewService(settings, components, vcs_factory=lambda o, r: vcs)
    svc.prepare_review("o/r", 7)

    svc.publish_review("o/r", 7, "summary", dry_run=False, task_key="ID-1")

    components.task_service.link_review.assert_called_once()
    args = components.task_service.link_review.call_args.args
    assert args[0] == "ID-1"                       # canonical task key
    pr_ref = args[1]
    assert pr_ref.repo == "o/r" and pr_ref.number == 7 and pr_ref.sha == "head456"
    assert pr_ref.url == "https://github.com/o/r/pull/7"
    assert args[2] == ["a.py#foo"]                 # changed_node_ids from session


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_review_no_link_on_dry_run(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    settings = _settings()
    components = _components()
    vcs = _fake_vcs(number=7)
    vcs.list_existing_fingerprints.return_value = set()
    svc = MCPReviewService(settings, components, vcs_factory=lambda o, r: vcs)
    svc.prepare_review("o/r", 7)
    svc.publish_review("o/r", 7, "summary", dry_run=True, task_key="ID-1")
    components.task_service.link_review.assert_not_called()


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_review_no_link_without_task_key(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    settings = _settings()
    components = _components()
    vcs = _fake_vcs(number=7)
    vcs.list_existing_fingerprints.return_value = set()
    svc = MCPReviewService(settings, components, vcs_factory=lambda o, r: vcs)
    svc.prepare_review("o/r", 7)
    svc.publish_review("o/r", 7, "summary", dry_run=False)
    components.task_service.link_review.assert_not_called()


def test_get_task_delegates_to_task_service():
    """MCPReviewService.get_task делегирует в task_service.get_task."""
    svc = _make_mcp_service()
    brief = {"key": "ID-1", "aliases": ["PRI-1"], "title": "T",
             "description": "d", "criteria": [], "status": "Open", "url": "u"}
    svc.components.task_service.get_task.return_value = brief
    assert svc.get_task("PRI-1") == brief
    svc.components.task_service.get_task.assert_called_once_with("PRI-1", project=None)


def test_get_task_miss_returns_none():
    svc = _make_mcp_service()
    svc.components.task_service.get_task.return_value = None
    assert svc.get_task("ZZ-9") is None


def test_task_tool_delegates() -> None:
    """index_task/search_tasks/get_task_context делегируют в task_service."""
    svc = _make_mcp_service()
    svc.components.task_service.index_task.return_value = {"key": "ID-1"}
    svc.components.task_service.search_tasks.return_value = "list"
    svc.components.task_service.get_task_context.return_value = "ctx"
    assert svc.index_task({"key": "ID-1"}) == {"key": "ID-1"}
    assert svc.search_tasks("q", 3) == "list"
    assert svc.get_task_context("ID-1") == "ctx"
    svc.components.task_service.search_tasks.assert_called_once_with("q", 3, project=None)
    svc.components.task_service.get_task_context.assert_called_once_with("ID-1", project=None)


def test_public_get_task_context_keeps_note_on_storage_failure():
    """PRI-276: публичный контракт тула цел — нота, а не исключение."""
    from neo4j.exceptions import ServiceUnavailable

    svc = _make_mcp_service()
    svc.components.task_service.get_task_context.side_effect = ServiceUnavailable("down")
    assert svc.get_task_context("ID-1") == "(task graph unavailable)"


def test_task_context_deps_linked_lets_storage_failure_through():
    """А провайдер секции отдаёт исключение в _safe, минуя обёртку с нотой."""
    from neo4j.exceptions import ServiceUnavailable

    from reviewer.mcp.service import _TaskContextDeps

    svc = _make_mcp_service()
    svc.components.task_service.get_task_context.side_effect = ServiceUnavailable("down")
    with pytest.raises(ServiceUnavailable):
        _TaskContextDeps(svc, None).linked("ID-1", "PRI")


def test_search_codebase_delegates_to_retriever() -> None:
    """search_codebase зовёт retriever.search_base (include_tests=False)
    и рендерит ContextPack с номерами строк."""
    svc = _make_mcp_service()
    svc.components.retriever.search_base.return_value.as_context.return_value = "auth.py#logout\nbody"
    out = svc.search_codebase("a/b", "logout", top_k=5)
    assert "auth.py#logout" in out
    call = svc.components.retriever.search_base.call_args
    assert call.args == ("a/b", "logout")
    assert call.kwargs["ceiling_override"] == 5
    assert isinstance(call.kwargs["limits"], CodebaseLimits)
    assert call.kwargs["hops"] == 1
    assert call.kwargs["branch"] == svc.settings.primary_branch()
    assert call.kwargs["include_tests"] is False
    svc.components.retriever.search_base.return_value.as_context.assert_called_once_with(
        line_numbers=True)


def test_search_codebase_empty_or_error_returns_note() -> None:
    """Пустой результат или сбой → '(ничего не найдено)'."""
    svc = _make_mcp_service()
    svc.components.retriever.search_base.return_value.as_context.return_value = ""
    assert svc.search_codebase("a/b", "x") == "(ничего не найдено)"
    svc.components.retriever.search_base.side_effect = RuntimeError("pg down")
    assert svc.search_codebase("a/b", "x") == "(ничего не найдено)"


def test_search_codebase_uses_explicit_repo() -> None:
    """search_codebase передаёт явный repo в retriever.search_base."""
    svc = _make_mcp_service()
    svc.components.retriever.search_base.return_value.as_context.return_value = "result"
    svc.search_codebase("a/x", "find foo")
    call_args = svc.components.retriever.search_base.call_args
    assert call_args.args[0] == "a/x"


def test_search_codebase_falls_back_to_default_repo() -> None:
    """При пустом repo берётся settings.default_repo."""
    svc = _make_mcp_service()
    svc.settings.default_repo = "d/efault"
    svc.components.retriever.search_base.return_value.as_context.return_value = "result"
    svc.search_codebase("", "find foo")
    call_args = svc.components.retriever.search_base.call_args
    assert call_args.args[0] == "d/efault"


def test_search_codebase_rejects_untracked_branch() -> None:
    """Ветка не отслеживается репозиторием → понятная заметка, retriever не зовётся."""
    svc = _make_mcp_service()
    out = svc.search_codebase("a/b", "x", branch="release/v9")
    assert "release/v9" in out
    assert "не отслеживается" in out
    svc.components.retriever.search_base.assert_not_called()


# ---------------------------------------------------------------------------
# Тесты _search_codebase_multi (PRI-255) — мультизапросный близнец search_codebase
# ---------------------------------------------------------------------------

def test_search_codebase_multi_delegates_to_search_multi() -> None:
    """_search_codebase_multi зовёт search_multi с queries, retriever'ом, branch,
    include_tests и рендерит результат построчно.

    Резолв лимитов из эффективной .review.yml-политики (а не дефолт-константы)
    закрыт отдельно — test_search_codebase_multi_passes_limits_and_hops в
    test_context_limits_wiring.py, на недефолтном home-слое.
    """
    svc = _make_mcp_service()
    with patch("reviewer.retrieval.multiquery.search_multi") as sm:
        sm.return_value.as_context.return_value = "auth.py#logout\nbody"
        out = svc._search_codebase_multi("a/b", ["q1", "q2"], include_tests=True)
        assert "auth.py#logout" in out
        call = sm.call_args
        assert call.args[0] is svc.components.retriever
        assert call.args[1] == "a/b"
        assert call.args[2] == ["q1", "q2"]
        assert call.kwargs["branch"] == svc.settings.primary_branch()
        assert call.kwargs["include_tests"] is True
        sm.return_value.as_context.assert_called_once_with(line_numbers=True)


def test_search_codebase_multi_empty_or_error_returns_note() -> None:
    """Пустой результат или сбой search_multi → '(ничего не найдено)'."""
    svc = _make_mcp_service()
    with patch("reviewer.retrieval.multiquery.search_multi") as sm:
        sm.return_value.as_context.return_value = ""
        assert svc._search_codebase_multi("a/b", ["x"]) == "(ничего не найдено)"
        sm.side_effect = RuntimeError("pg down")
        assert svc._search_codebase_multi("a/b", ["x"]) == "(ничего не найдено)"


def test_search_codebase_multi_rejects_untracked_branch() -> None:
    """Ветка не отслеживается репозиторием → понятная заметка, search_multi не зовётся."""
    svc = _make_mcp_service()
    with patch("reviewer.retrieval.multiquery.search_multi") as sm:
        out = svc._search_codebase_multi("a/b", ["x"], branch="release/v9")
        assert "release/v9" in out
        assert "не отслеживается" in out
        sm.assert_not_called()


class _FailingRetriever:
    """Ретривер, у которого уже доступ к `.embedder` бросает `exc`.

    search_multi читает `retriever.embedder` до входа в собственные fail-soft
    блоки (_embed_pairs/_run/_graph_items оборачивают вызовы, а не сам доступ
    к атрибуту) — поэтому только так сбой долетает до _search_codebase_multi
    настоящим, не проглоченным внутри самого search_multi.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    @property
    def embedder(self):
        raise self._exc


def _service_with_failing_retriever(exc: BaseException) -> MCPReviewService:
    svc = _make_mcp_service()
    svc.components.retriever = _FailingRetriever(exc)
    return svc


def test_search_codebase_multi_swallows_embedder_failure_by_default():
    """Приватный мультизапрос по умолчанию нем — как и был."""
    from voyageai.error import APIError

    svc = _service_with_failing_retriever(APIError("HTTP code 403"))
    assert svc._search_codebase_multi("o/n", ["q"], "dev") == "(ничего не найдено)"


def test_search_codebase_multi_reraises_embedder_failure_when_strict():
    from voyageai.error import APIError

    svc = _service_with_failing_retriever(APIError("HTTP code 403"))
    with pytest.raises(APIError):
        svc._search_codebase_multi("o/n", ["q"], "dev", strict=True)


def test_search_codebase_multi_stays_soft_on_other_failures_when_strict():
    svc = _service_with_failing_retriever(RuntimeError("boom"))
    assert svc._search_codebase_multi(
        "o/n", ["q"], "dev", strict=True) == "(ничего не найдено)"


def test_task_context_deps_code_passes_include_tests_false() -> None:
    """_TaskContextDeps.code зовёт _search_codebase_multi с include_tests=False.

    Перепутанный позиционный аргумент незаметен для FakeDeps-тестов task_context —
    там подменяется весь слой deps. Тест ловит именно проводку внутри service.py.

    augment_sources (PRI-257) идёт kwarg: без предшествующего similar() _similar_hits
    пуст, поэтому источник несёт пустой augment_paths=[].
    """
    from reviewer.mcp.service import _TaskContextDeps

    fake_service = MagicMock()
    fake_service._search_codebase_multi.return_value = "code-out"
    deps = _TaskContextDeps(fake_service, None)

    result = deps.code("o/r", "dev", ["q1", "q2"])

    call = fake_service._search_codebase_multi.call_args
    assert call.args == ("o/r", ["q1", "q2"], "dev", False)
    assert call.kwargs["strict"] is True
    sources = call.kwargs["augment_sources"]
    assert len(sources) == 1
    assert sources[0].name == "similar-diffs"
    assert sources[0].paths == []
    assert sources[0].quota == (
        fake_service._resolve_context_limits.return_value.code_section.max_augmented_files)
    assert result == "code-out"


def test_task_context_deps_similar_stores_hits_and_renders_once() -> None:
    """_TaskContextDeps.similar делает один поиск (search_hits) и рендерит его же результат.

    Второй вызов означал бы второй embed_query — лишний расход квоты Voyage.
    """
    from reviewer.mcp.service import _TaskContextDeps

    fake_service = MagicMock()
    hits = [TaskHit(key="ID-311", title="T", status="open", score=0.02,
                    aliases=["PRI-257"])]
    fake_service.components.task_service.search_hits.return_value = hits
    fake_service.components.task_service.render_hits.return_value = "1. ID-311 ..."
    deps = _TaskContextDeps(fake_service, None)

    result = deps.similar("query", "PRI")

    fake_service.components.task_service.search_hits.assert_called_once_with(
        "query", project="PRI", strict=True)
    fake_service.components.task_service.render_hits.assert_called_once_with(hits)
    assert result == "1. ID-311 ..."
    assert deps._similar_hits == hits


def test_task_context_deps_code_augments_from_similar_hits_with_aliases() -> None:
    """code() подмешивает пути похожих задач, ключуя по канону И по алиасам хита."""
    from reviewer.mcp.service import _TaskContextDeps

    fake_service = MagicMock()
    fake_service._repo_clone_path.return_value = "/clone"
    fake_service.components.task_service.search_hits.return_value = [
        TaskHit(key="ID-311", title="T", status="open", score=0.02,
               aliases=["PRI-257"]),
    ]
    fake_service.components.task_service.render_hits.return_value = "1. ID-311 ..."
    fake_service._search_codebase_multi.return_value = "code-out"
    deps = _TaskContextDeps(fake_service, None)

    deps.similar("query", "PRI")
    with patch("reviewer.retrieval.augment.collect_similar_task_paths") as collect:
        collect.return_value = AugmentResult(paths=["a.py"], gaps=[])
        deps.code("o/r", "dev", ["q1"])

    call_kwargs = collect.call_args.kwargs
    assert call_kwargs["keys"] == ["ID-311"]
    assert call_kwargs["aliases_by_key"] == {"ID-311": ["PRI-257"]}


def test_task_context_deps_augment_paths_empty_without_similar_call() -> None:
    """Без предшествующего similar() подмешивание не запускается — нет хитов."""
    from reviewer.mcp.service import _TaskContextDeps

    fake_service = MagicMock()
    deps = _TaskContextDeps(fake_service, None)

    assert deps._augment_paths("o/r") == []
    assert deps.augment_gaps == []


def test_task_context_deps_history_failure_is_a_gap_not_an_exception() -> None:
    """Сбой _ensure_history не роняет сборку — пробел копится в augment_gaps."""
    from reviewer.mcp.service import _TaskContextDeps

    fake_service = MagicMock()
    fake_service._review_service._ensure_history.side_effect = RuntimeError("no pg")
    fake_service.components.task_service.search_hits.return_value = [
        TaskHit(key="ID-311", title="T", status="open", score=0.02),
    ]
    fake_service.components.task_service.render_hits.return_value = "1. ID-311 ..."
    deps = _TaskContextDeps(fake_service, None)
    deps.similar("query", "PRI")

    with patch("reviewer.retrieval.augment.collect_similar_task_paths") as collect:
        collect.return_value = AugmentResult()
        paths = deps._augment_paths("o/r")

    assert paths == []
    assert any("история прогонов недоступна" in g for g in deps.augment_gaps)
    assert collect.call_args.kwargs["history"] is None


def test_task_context_deps_test_exemplars_passes_include_tests_true() -> None:
    """_TaskContextDeps.test_exemplars зовёт _search_codebase_multi с include_tests=True."""
    from reviewer.mcp.service import _TaskContextDeps

    fake_service = MagicMock()
    fake_service._search_codebase_multi.return_value = "tests-out"
    deps = _TaskContextDeps(fake_service, None)

    result = deps.test_exemplars("o/r", "dev", ["q1", "q2"])

    fake_service._search_codebase_multi.assert_called_once_with(
        "o/r", ["q1", "q2"], "dev", True, strict=True)
    assert result == "tests-out"


# ---------------------------------------------------------------------------
# Тесты Task 2: session-less методы related_symbols/callers/definition
# ---------------------------------------------------------------------------

def test_related_symbols_delegates_to_graph() -> None:
    """related_symbols зовёт graph.expand_detailed и форматирует компактно."""
    svc = _make_mcp_service()
    svc.components.graph.expand_detailed.return_value = [
        {"id": "a.py#bar", "rels": ["CALLS"], "dist": 1},
        {"id": "a.py#baz", "rels": ["IMPLEMENTS"], "dist": 1},
    ]
    svc.components.store.fetch_nodes.return_value = [
        SimpleNamespace(node_id="a.py#bar", path="a.py", start_line=5,
                        end_line=6, text="def bar(): ..."),
        SimpleNamespace(node_id="a.py#baz", path="a.py", start_line=9,
                        end_line=10, text="def baz(): ..."),
    ]
    out = svc.related_symbols("a/b", "a.py#foo")
    assert "// a.py#bar (a.py:5) [CALLS, d1]" in out
    assert "// a.py#baz (a.py:9) [IMPLEMENTS, d1]" in out
    svc.components.graph.expand_detailed.assert_called_once_with(
        "a/b", ["a.py#foo"], hops=2, branch=svc.settings.primary_branch())


def test_related_symbols_empty_and_failsoft() -> None:
    """Пусто → '(нет связей)'; исключение графа → тоже заглушка, не падаем."""
    svc = _make_mcp_service()
    svc.components.graph.expand_detailed.return_value = []
    assert svc.related_symbols("a/b", "a.py#foo") == "(нет связей)"
    svc.components.graph.expand_detailed.side_effect = RuntimeError("neo4j down")
    assert svc.related_symbols("a/b", "a.py#foo") == "(нет связей)"


def test_related_symbols_graph_none() -> None:
    """graph=None (Neo4j выключен) → '(граф недоступен)'."""
    svc = _make_mcp_service()
    svc.components.graph = None
    assert svc.related_symbols("a/b", "a.py#foo") == "(граф недоступен)"


def test_related_symbols_invalid_branch() -> None:
    """Невалидная ветка → заметка из _resolve_repo_branch, граф не зовётся."""
    svc = _make_mcp_service()
    out = svc.related_symbols("a/b", "a.py#foo", branch="nope")
    assert "nope" in out
    assert "не отслеживается" in out


def test_callers_delegates_to_graph() -> None:
    """callers зовёт graph.callers_detailed и форматирует компактно."""
    svc = _make_mcp_service()
    svc.components.graph.callers_detailed.return_value = [
        {"id": "a.py#caller", "rel": "CALLS"}]
    svc.components.store.fetch_nodes.return_value = [
        SimpleNamespace(node_id="a.py#caller", path="a.py", start_line=3,
                        end_line=4, text="def caller(): ...")]
    out = svc.callers("a/b", "a.py#foo")
    assert "// a.py#caller (a.py:3) [CALLS]" in out
    svc.components.graph.callers_detailed.assert_called_once_with(
        "a/b", ["a.py#foo"], branch=svc.settings.primary_branch())
    svc.components.graph.callers_detailed.return_value = []
    assert svc.callers("a/b", "a.py#foo") == "(вызовов не найдено)"


def test_implementations_delegates_to_graph() -> None:
    """implementations зовёт graph.implementations_detailed и форматирует компактно."""
    svc = _make_mcp_service()
    svc.components.graph.implementations_detailed.return_value = [
        {"id": "impl.py#Sub", "rel": "IMPLEMENTS"}]
    svc.components.store.fetch_nodes.return_value = [
        SimpleNamespace(node_id="impl.py#Sub", path="impl.py", start_line=3,
                        end_line=4, text="class Sub(Base): ...")]
    out = svc.implementations("a/b", "base.py#Base")
    assert "// impl.py#Sub (impl.py:3) [IMPLEMENTS]" in out
    svc.components.graph.implementations_detailed.assert_called_once_with(
        "a/b", ["base.py#Base"], branch=svc.settings.primary_branch())
    svc.components.graph.implementations_detailed.return_value = []
    assert svc.implementations("a/b", "base.py#Base") == "(implementations не найдены)"


def test_implementations_empty_and_failsoft() -> None:
    """Пусто → заглушка; исключение графа → тоже заглушка, не падаем."""
    svc = _make_mcp_service()
    svc.components.graph.implementations_detailed.return_value = []
    assert svc.implementations("a/b", "base.py#Base") == "(implementations не найдены)"
    svc.components.graph.implementations_detailed.side_effect = RuntimeError("neo4j down")
    assert svc.implementations("a/b", "base.py#Base") == "(implementations не найдены)"


def test_implementations_graph_none() -> None:
    """graph=None (Neo4j выключен) → '(граф недоступен)'."""
    svc = _make_mcp_service()
    svc.components.graph = None
    assert svc.implementations("a/b", "base.py#Base") == "(граф недоступен)"


def test_implementations_skips_family_lookup_for_method_nodes() -> None:
    """Minor 9: узел-метод (fqn с точкой) не платит за подсчёт семейства."""
    svc = _make_mcp_service()
    svc.components.graph.implementations_detailed.return_value = []
    out = svc.implementations("a/b", "base.py#Base.method")
    assert out == "(implementations не найдены)"
    svc.components.graph.class_members.assert_not_called()
    svc.components.graph.bases_of.assert_not_called()


def test_implementations_computes_family_for_class_nodes() -> None:
    """Узел-класс (fqn без точки) по-прежнему считает семейство при пустых наследниках."""
    svc = _make_mcp_service()
    svc.components.graph.implementations_detailed.return_value = []
    svc.components.graph.bases_of.return_value = {}
    svc.components.graph.class_members.return_value = {}
    out = svc.implementations("a/b", "base.py#Base")
    assert out == "(implementations не найдены)"
    svc.components.graph.class_members.assert_called_once()


def test_family_includes_siblings_via_common_base() -> None:
    """Important 1: узел-представитель без своих наследников получает сиблингов.

    Воспроизводит основной сценарий провала: ретрив находит адаптер
    (``AsanaBoard``), у которого нет собственных подклассов, — семейство
    обязано находиться через сиблингов по общей базе (``RestBoardBase``).
    """
    node = "reviewer/tasks/boards/asana.py#AsanaBoard"
    base = "reviewer/tasks/boards/restbase.py#RestBoardBase"
    sibling = "reviewer/tasks/boards/trello.py#TrelloBoard"

    def fake_bases_of(repo, ids, *, branch=None):
        if ids == [node]:
            return {node: [base]}
        return {}

    def fake_impl_detailed(repo, ids, *, branch=None):
        if ids == [node]:
            return []
        if ids == [base]:
            return [{"id": node, "rel": "IMPLEMENTS"}, {"id": sibling, "rel": "IMPLEMENTS"}]
        return []

    svc = _make_mcp_service()
    svc.components.graph.bases_of.side_effect = fake_bases_of
    svc.components.graph.implementations_detailed.side_effect = fake_impl_detailed
    svc.components.graph.class_members.return_value = {}
    svc.components.store.fetch_nodes.return_value = [
        SimpleNamespace(node_id=sibling, path="reviewer/tasks/boards/trello.py",
                        start_line=1, end_line=2, text="class TrelloBoard(RestBoardBase): ...")]

    out = svc.family("a/b", node)
    assert "// семейство из 1 членов" in out
    assert sibling in out


def test_definition_uses_graph_then_store() -> None:
    """definition: find_symbol → fetch_nodes → рендер через as_context (с номерами строк)."""
    svc = _make_mcp_service()
    svc.components.graph.find_symbol.return_value = ["a.py#foo"]
    node = SimpleNamespace(node_id="a.py#foo", path="a.py",
                           start_line=10, end_line=12, text="def foo(): ...")
    svc.components.store.fetch_nodes.return_value = [node]
    out = svc.definition("a/b", "foo")
    assert "a.py#foo" in out and "def foo()" in out and "a.py:10-12" in out
    assert "   10 | def foo()" in out  # номера строк присутствуют
    svc.components.graph.find_symbol.assert_called_once_with(
        "a/b", "foo", branch=svc.settings.primary_branch())
    svc.components.store.fetch_nodes.assert_called_once_with(
        "a/b", ["a.py#foo"], None, [], base_ref="base:main")


def test_definition_falls_back_to_search_base() -> None:
    """Граф пуст → фолбэк на retriever.search_base (include_tests=True)."""
    svc = _make_mcp_service()
    svc.components.graph.find_symbol.return_value = []
    svc.components.retriever.search_base.return_value.as_context.return_value = "semantic hit"
    out = svc.definition("a/b", "foo")
    assert "semantic hit" in out
    # PRI-202: definition не резолвит .review.yml (граф-фолбэк) — limits/hops
    # дефолтные, только ceiling_override сужает охват (cliff floor-lift до 4).
    svc.components.retriever.search_base.assert_called_once_with(
        "a/b", "foo", ceiling_override=3, branch=svc.settings.primary_branch(),
        include_tests=True)


# ---------------------------------------------------------------------------
# Тесты Task 5: session-less get_pr_diff
# ---------------------------------------------------------------------------

def test_get_pr_diff_formats_changed_files():
    vcs = MagicMock()
    vcs.get_changed_files.return_value = [
        _changed(path="a.py", status="modified", patch="@@ -1 +1 @@\n-x\n+y"),
    ]
    svc = MCPReviewService(_settings(), _components(), vcs_factory=lambda o, r: vcs)
    out = svc.get_pr_diff("o/r", 7)
    assert "a.py" in out and "+y" in out
    vcs.get_changed_files.assert_called_once_with(7)
    vcs.close.assert_not_called()  # factory-владелец не закрываем


def test_get_pr_diff_failsoft_on_error():
    vcs = MagicMock()
    vcs.get_changed_files.side_effect = RuntimeError("boom")
    svc = MCPReviewService(_settings(), _components(), vcs_factory=lambda o, r: vcs)
    assert svc.get_pr_diff("o/r", 7) == "(diff PR недоступен)"


def test_get_pr_diff_empty_repo_note():
    svc = MCPReviewService(_settings(), _components(), vcs_factory=lambda o, r: MagicMock())
    assert "repo не задан" in svc.get_pr_diff("", 7)


def test_get_pr_diff_failsoft_on_provider_error():
    def boom_factory(o, r):
        raise RuntimeError("no token")
    svc = MCPReviewService(_settings(), _components(), vcs_factory=boom_factory)
    assert svc.get_pr_diff("o/r", 7) == "(diff PR недоступен)"


def test_get_pr_diff_empty_files_note():
    vcs = MagicMock()
    vcs.get_changed_files.return_value = []
    svc = MCPReviewService(_settings(), _components(), vcs_factory=lambda o, r: vcs)
    assert svc.get_pr_diff("o/r", 7) == "(PR без изменённых файлов)"


# ---------------------------------------------------------------------------
# Тесты structural_summary в payload prepare_review (PRI-158)
# ---------------------------------------------------------------------------

@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_payload_includes_structural_summary(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """При смене сигнатуры payload-юнит несёт structural_summary."""
    settings = _settings()
    components = _components()
    vcs = _fake_vcs()

    def _read(path: str, ref: str) -> str | None:
        if path == ".review.yml":
            return None
        return "def foo(a, b): pass" if ref == "head456" else "def foo(a): pass"
    vcs.get_file_at_ref.side_effect = _read

    svc = MCPReviewService(settings, components, vcs_factory=lambda o, r: vcs)
    out = svc.prepare_review("o/r", 7)

    unit = out["units"][0]
    assert "structural_summary" in unit
    assert "foo" in unit["structural_summary"]


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_payload_omits_empty_structural_summary(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """Без структурных изменений (base==head) ключ structural_summary отсутствует."""
    svc = _make_mcp_service()  # _fake_vcs отдаёт одинаковый исходник для base и head
    out = svc.prepare_review("o/r", 7)

    assert "structural_summary" not in out["units"][0]


# ---------------------------------------------------------------------------
# Тесты PRI-275: preflight не платит второй таймаут пула за мёртвый Postgres
# ---------------------------------------------------------------------------

def test_task_context_deps_preflight_raises_and_stops_after_one_store_call() -> None:
    """_TaskContextDeps.preflight при недоступном сторе делает ОДИН заход в стор.

    Тип исключения обязан быть psycopg_pool.PoolTimeout, а не голый
    psycopg.OperationalError: это намеренный контракт с соседней задачей
    PRI-274 — если она выведет PoolTimeout из is_storage_unavailable, этот
    тест обязан покраснеть.
    """
    from reviewer.mcp.service import _TaskContextDeps

    calls: list[str] = []

    class _CountingStore:
        def get_repo_clone(self, repo: str) -> str | None:
            calls.append("get_repo_clone")
            raise psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec")

        def get_index_meta_row(self, repo: str, ref: str):
            calls.append("get_index_meta_row")
            return None

        def count_chunks(self, repo: str, ref: str) -> int:
            calls.append("count_chunks")
            return 0

    svc = _make_mcp_service()
    svc.components.store = _CountingStore()
    deps = _TaskContextDeps(svc, None)

    with pytest.raises(psycopg_pool.PoolTimeout):
        deps.preflight("o/r", "dev")

    assert calls == ["get_repo_clone"]


def test_task_context_deps_preflight_stays_fail_soft_on_non_storage_clone_failure() -> None:
    """Не-storage сбой чтения пути к клону остаётся fail-soft: preflight не падает.

    RuntimeError не распознаётся is_storage_unavailable, поэтому strict=True
    у _repo_clone_path его не пробрасывает — путь клона резолвится в пустую
    строку, и build_status_report вызывается как обычно.
    """
    from reviewer.mcp.service import _TaskContextDeps
    from reviewer.services.status import BranchStatus, RepoStatus

    class _BrokenCloneStore:
        def get_repo_clone(self, repo: str) -> str | None:
            raise RuntimeError("битая строка")

    svc = _make_mcp_service()
    svc.components.store = _BrokenCloneStore()

    fake_report = RepoStatus(
        repo="o/r",
        branches=[BranchStatus(branch="dev", ref="base:dev", indexed_sha="abc",
                               updated_at=None, chunks=5, graph_nodes=3, drift=0)],
        overlays=[])

    with patch("reviewer.services.status.build_status_report") as build_report:
        build_report.return_value = fake_report
        deps = _TaskContextDeps(svc, None)
        result = deps.preflight("o/r", "dev")

    assert result["indexed_sha"] == "abc"
    # clone_path — пятый позиционный аргумент build_status_report; пустая
    # строка подтверждает, что сбой чтения пути молча проглочен (fail-soft).
    assert build_report.call_args.args[4] == ""
