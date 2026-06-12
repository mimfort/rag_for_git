"""Unit-тесты MCPReviewService.

Мокаем VCS-провайдер, Store, Graph, LLM-компоненты.
Проверяем prepare_review, кэш сессий, валидацию и cleanup.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

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
    s.review_trace = False
    s.review_verdict_log = ""
    s.review_synthesis = False
    s.review_skip_drafts = True
    s.review_max_files = 50
    s.openrouter_api_key = "test"
    s.voyage_api_key = "test"
    s.github_token = "test"
    return s


def _components() -> MagicMock:
    c = MagicMock()
    c.store = MagicMock()
    c.embedder = MagicMock()
    c.retriever = MagicMock()
    c.graph = MagicMock()
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
    try:
        svc.search_code("o/r", 7, "query")
        assert False, "ожидали ошибку"
    except ValueError as e:
        assert "prepare_review" in str(e)


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
