"""Тесты обвязки context_limits в session-less MCP-тулах (PRI-202, Task 7).

_resolve_context_limits — fail-soft резолв ContextLimits из .review.yml ветки
(зеркало _resolve_summary_depth). search_codebase пробрасывает limits/hops/
ceiling_override в Retriever.search_base (новая сигнатура, Task 5).
"""
from unittest.mock import MagicMock

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
    """Сбой чтения .review.yml (VCS недоступен) → дефолт-константы ContextLimits."""
    s = _settings()
    components = MagicMock()
    vcs = MagicMock()
    vcs.get_file_at_ref.side_effect = RuntimeError("network down")
    svc = MCPReviewService(s, components, vcs_factory=lambda o, n: vcs)

    cl = svc._resolve_context_limits("o/r", "dev")

    assert isinstance(cl, ContextLimits)
    assert cl.search_codebase.ceiling == 15        # дефолт-константа CodebaseLimits.ceiling


def test_resolve_context_limits_no_review_yml_returns_defaults() -> None:
    """Файла .review.yml нет (пустой текст) → тоже дефолт, без исключения."""
    s = _settings()
    components = MagicMock()
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = None
    svc = MCPReviewService(s, components, vcs_factory=lambda o, n: vcs)

    cl = svc._resolve_context_limits("o/r", "dev")

    assert isinstance(cl, ContextLimits)
    assert cl.search_codebase.ceiling == 15


def test_resolve_context_limits_uses_home_repo_layer(tmp_path) -> None:
    """Репозиторный home-слой задаёт лимит graph.hops без .review.yml в VCS."""
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
