"""Unit-тесты MCPReviewService.

Мокаем VCS-провайдер, Store, Graph, LLM-компоненты.
Проверяем prepare_review, кэш сессий, валидацию и cleanup.
"""
from __future__ import annotations

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
