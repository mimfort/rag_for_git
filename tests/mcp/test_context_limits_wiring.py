"""Тесты effective layered policy для context limits в session-less MCP-тулах.

_resolve_context_limits fail-soft объединяет env, home-слои и committed
`.review.yml` (зеркало `_resolve_summary_depth`). `search_codebase` передаёт
limits/hops/ceiling_override в `Retriever.search_base`.
"""
import logging
from unittest.mock import MagicMock, patch

import pytest

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
from reviewer.policy.context_limits import CodebaseLimits, ContextLimits


def _settings() -> Settings:
    s = Settings()
    s.voyage_api_key = "test"
    s.github_token = "test"
    s.default_repo = ""
    return s


@pytest.fixture(autouse=True)
def _isolated_home_config(monkeypatch, tmp_path) -> None:
    """Изолирует MCP-резолв policy от конфигурации разработчика."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_resolve_context_limits_failsoft_returns_defaults() -> None:
    """Сбой чтения committed policy через VCS → дефолтные ContextLimits."""
    s = _settings()
    components = MagicMock()
    vcs = MagicMock()
    vcs.get_file_at_ref.side_effect = RuntimeError("network down")
    svc = MCPReviewService(s, components, vcs_factory=lambda o, n: vcs)

    cl = svc._resolve_context_limits("o/r", "dev")

    assert isinstance(cl, ContextLimits)
    assert cl.search_codebase.ceiling == 15        # дефолт-константа CodebaseLimits.ceiling


def test_resolve_context_limits_no_review_yml_returns_defaults() -> None:
    """Без committed policy effective policy даёт дефолты без исключения."""
    s = _settings()
    components = MagicMock()
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = None
    svc = MCPReviewService(s, components, vcs_factory=lambda o, n: vcs)

    cl = svc._resolve_context_limits("o/r", "dev")

    assert isinstance(cl, ContextLimits)
    assert cl.search_codebase.ceiling == 15


def test_resolve_context_limits_uses_home_repo_layer(tmp_path) -> None:
    """Репозиторный home-слой задаёт graph.hops без committed policy в VCS."""
    path = tmp_path / "rag-reviewer/repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("context_limits: {graph: {hops: 2}}\n", encoding="utf-8")
    s = _settings()
    components = MagicMock()
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = None
    svc = MCPReviewService(s, components, vcs_factory=lambda o, n: vcs)

    limits = svc._resolve_context_limits("o/r", "dev")

    assert limits.graph.hops == 2


def test_resolve_policy_reports_repo_home_source_and_keeps_injected_vcs_open(tmp_path) -> None:
    """Репозиторный слой побеждает VCS и сохраняет ownership injected VCS."""
    global_path = tmp_path / "rag-reviewer/review.yml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text("summary_topk_threshold: 3\n", encoding="utf-8")
    repo_path = tmp_path / "rag-reviewer/repos/o/r.yml"
    repo_path.parent.mkdir(parents=True)
    repo_path.write_text("summary_topk_threshold: 7\n", encoding="utf-8")
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = "summary_topk_threshold: 5\n"
    svc = MCPReviewService(_settings(), MagicMock(), vcs_factory=lambda owner, name: vcs)

    policy, meta = svc._resolve_policy("o/r", "dev")

    assert policy.summary_topk_threshold == 7
    assert meta.sources["summary_topk_threshold"] == "home:repos/o/r.yml"
    vcs.close.assert_not_called()


def test_resolve_policy_closes_internally_created_vcs() -> None:
    """Созданный сервисом VCS закрывается после резолва policy."""
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = None
    svc = MCPReviewService(_settings(), MagicMock(), vcs_factory=None)

    with patch.object(svc._review_service, "_create_vcs_provider", return_value=vcs):
        svc._resolve_policy("o/r", "dev")

    vcs.close.assert_called_once_with()


def test_resolve_context_limits_failsoft_closes_internal_vcs_after_fetch_error() -> None:
    """Сбой чтения committed layer сохраняет fallback и закрывает owned VCS."""
    vcs = MagicMock()
    vcs.get_file_at_ref.side_effect = RuntimeError("network down")
    svc = MCPReviewService(_settings(), MagicMock(), vcs_factory=None)

    with patch.object(svc._review_service, "_create_vcs_provider", return_value=vcs):
        limits = svc._resolve_context_limits("o/r", "dev")

    assert limits == ContextLimits()
    vcs.close.assert_called_once_with()


def test_resolve_context_limits_ignores_internal_vcs_close_failure() -> None:
    """Ошибка close внутреннего provider не отменяет уже полученный policy."""
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = "context_limits: {graph: {hops: 2}}\n"
    vcs.close.side_effect = RuntimeError("close failed")
    svc = MCPReviewService(_settings(), MagicMock(), vcs_factory=None)

    with patch.object(svc._review_service, "_create_vcs_provider", return_value=vcs):
        limits = svc._resolve_context_limits("o/r", "dev")

    assert limits.graph.hops == 2
    vcs.close.assert_called_once_with()


def test_resolve_policy_logs_sanitized_home_credential_warning(caplog, tmp_path) -> None:
    """MCP warning explains skipped credential-shaped home layer without its value."""
    path = tmp_path / "rag-reviewer/repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("github_token: leaked-value\n", encoding="utf-8")
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = None
    svc = MCPReviewService(_settings(), MagicMock(), vcs_factory=lambda owner, name: vcs)

    with caplog.at_level(logging.WARNING, logger="reviewer.mcp.service"):
        policy, meta = svc._resolve_policy("o/r", "dev")

    assert policy.context_limits == ContextLimits()
    assert meta.warnings
    assert "Домашний слой policy пропущен" in caplog.text
    assert "github_token" in caplog.text
    assert "leaked-value" not in caplog.text


def test_resolve_context_limits_uses_falsey_injected_vcs_factory() -> None:
    """Переданная VCS-фабрика остаётся caller-owned независимо от truthiness."""
    class _FalseyFactory:
        def __init__(self, vcs) -> None:
            self.vcs = vcs

        def __bool__(self) -> bool:
            return False

        def __call__(self, owner, name):
            return self.vcs

    injected_vcs = MagicMock()
    injected_vcs.get_file_at_ref.return_value = None
    internal_vcs = MagicMock()
    internal_vcs.get_file_at_ref.return_value = None
    svc = MCPReviewService(
        _settings(), MagicMock(), vcs_factory=_FalseyFactory(injected_vcs)
    )

    with patch.object(svc._review_service, "_create_vcs_provider", return_value=internal_vcs) as create:
        svc._resolve_context_limits("o/r", "dev")

    create.assert_not_called()
    injected_vcs.close.assert_not_called()


class _FakeRetriever:
    """Фейковый Retriever — фиксирует kwargs вызова search_base (новая сигнатура)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search_base(self, repo, query, *, limits=None, hops=1, ceiling_override=None,
                    branch="", include_tests=False):
        self.calls.append({"limits": limits, "hops": hops, "ceiling_override": ceiling_override})

        class _Pack:
            def as_context(self_inner, line_numbers=False):
                return "ok"
        return _Pack()


def test_search_codebase_passes_limits_and_topk_override() -> None:
    """search_codebase резолвит ContextLimits и пробрасывает limits/hops/ceiling_override."""
    s = _settings()
    s.review_branches = "dev"          # branch="dev" должна быть отслеживаемой
    components = MagicMock()
    retr = _FakeRetriever()
    components.retriever = retr
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = None    # нет .review.yml → дефолт-лимиты
    svc = MCPReviewService(s, components, vcs_factory=lambda o, n: vcs)

    svc.search_codebase("o/r", "q", top_k=40, branch="dev")

    assert retr.calls[0]["ceiling_override"] == 40
    assert isinstance(retr.calls[0]["limits"], CodebaseLimits)
    assert retr.calls[0]["hops"] == 1
