"""Unit-тесты MCPReviewService.

Мокаем VCS-провайдер, Store, Graph, LLM-компоненты.
Проверяем prepare_review, кэш сессий, валидацию и cleanup.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
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

    svc.publish_review("o/r", 7, "summary", [], dry_run=False, task_key="ID-1")

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
    svc.publish_review("o/r", 7, "summary", [], dry_run=True, task_key="ID-1")
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
    svc.publish_review("o/r", 7, "summary", [], dry_run=False)
    components.task_service.link_review.assert_not_called()


def test_task_tool_delegates() -> None:
    """index_task/search_tasks/get_task_context делегируют в task_service."""
    svc = _make_mcp_service()
    svc.components.task_service.index_task.return_value = {"key": "ID-1"}
    svc.components.task_service.search_tasks.return_value = "list"
    svc.components.task_service.get_task_context.return_value = "ctx"
    assert svc.index_task({"key": "ID-1"}) == {"key": "ID-1"}
    assert svc.search_tasks("q", 3) == "list"
    assert svc.get_task_context("ID-1") == "ctx"
    svc.components.task_service.search_tasks.assert_called_once_with("q", 3)


def test_search_codebase_delegates_to_retriever() -> None:
    """search_codebase зовёт retriever.search_base (include_tests=False)
    и рендерит ContextPack с номерами строк."""
    svc = _make_mcp_service()
    svc.components.retriever.search_base.return_value.as_context.return_value = "auth.py#logout\nbody"
    out = svc.search_codebase("a/b", "logout", top_k=5)
    assert "auth.py#logout" in out
    svc.components.retriever.search_base.assert_called_once_with(
        "a/b", "logout", top_k=5, branch=svc.settings.primary_branch(),
        include_tests=False)
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
    """Ветка не из REVIEW_BRANCHES → понятная заметка, retriever не зовётся."""
    svc = _make_mcp_service()
    out = svc.search_codebase("a/b", "x", branch="release/v9")
    assert "REVIEW_BRANCHES" in out
    svc.components.retriever.search_base.assert_not_called()


# ---------------------------------------------------------------------------
# Тесты Task 2: session-less методы related_symbols/callers/definition
# ---------------------------------------------------------------------------

def test_related_symbols_delegates_to_graph() -> None:
    """related_symbols зовёт graph.expand(repo, [node_id], hops=2, branch=primary)."""
    svc = _make_mcp_service()
    svc.components.graph.expand.return_value = {"a.py#bar", "a.py#baz"}
    out = svc.related_symbols("a/b", "a.py#foo")
    assert "a.py#bar" in out and "a.py#baz" in out
    svc.components.graph.expand.assert_called_once_with(
        "a/b", ["a.py#foo"], hops=2, branch=svc.settings.primary_branch())


def test_related_symbols_empty_and_failsoft() -> None:
    """Пусто → '(нет связей)'; исключение графа → тоже заглушка, не падаем."""
    svc = _make_mcp_service()
    svc.components.graph.expand.return_value = set()
    assert svc.related_symbols("a/b", "a.py#foo") == "(нет связей)"
    svc.components.graph.expand.side_effect = RuntimeError("neo4j down")
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
    assert "REVIEW_BRANCHES" in out


def test_callers_delegates_to_graph() -> None:
    """callers зовёт graph.callers и форматирует множество."""
    svc = _make_mcp_service()
    svc.components.graph.callers.return_value = {"a.py#caller"}
    out = svc.callers("a/b", "a.py#foo")
    assert "a.py#caller" in out
    svc.components.graph.callers.assert_called_once_with(
        "a/b", ["a.py#foo"], branch=svc.settings.primary_branch())
    svc.components.graph.callers.return_value = set()
    assert svc.callers("a/b", "a.py#foo") == "(вызовов не найдено)"


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
    svc.components.retriever.search_base.assert_called_once_with(
        "a/b", "foo", top_k=3, branch=svc.settings.primary_branch(),
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
