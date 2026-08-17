"""Тесты effective layered policy для context limits в session-less MCP-тулах.

_resolve_context_limits fail-soft объединяет env, home-слои и committed
`.review.yml` (зеркало `_resolve_summary_layout`). `search_codebase` передаёт
limits/hops/ceiling_override в `Retriever.search_base`.
"""
import logging
from unittest.mock import MagicMock, patch

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
from reviewer.policy.context_limits import CodebaseLimits, CodeSectionLimits, ContextLimits


def _settings() -> Settings:
    s = Settings()
    s.voyage_api_key = "test"
    s.github_token = "test"
    s.default_repo = ""
    return s


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


def test_resolve_context_limits_uses_home_repo_layer(
    isolated_xdg_config_home,
) -> None:
    """Репозиторный home-слой задаёт graph.hops без committed policy в VCS."""
    path = isolated_xdg_config_home / "rag-reviewer/repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("context_limits: {graph: {hops: 2}}\n", encoding="utf-8")
    s = _settings()
    components = MagicMock()
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = None
    svc = MCPReviewService(s, components, vcs_factory=lambda o, n: vcs)

    limits = svc._resolve_context_limits("o/r", "dev")

    assert limits.graph.hops == 2


def test_resolve_policy_reports_repo_home_source_and_keeps_injected_vcs_open(
    isolated_xdg_config_home,
) -> None:
    """Репозиторный слой побеждает VCS и сохраняет ownership injected VCS."""
    global_path = isolated_xdg_config_home / "rag-reviewer/review.yml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text("summary_topk_threshold: 3\n", encoding="utf-8")
    repo_path = isolated_xdg_config_home / "rag-reviewer/repos/o/r.yml"
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


def test_resolve_policy_logs_sanitized_home_credential_warning(
    caplog,
    isolated_xdg_config_home,
) -> None:
    """MCP warning explains skipped credential-shaped home layer without its value."""
    path = isolated_xdg_config_home / "rag-reviewer/repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("github_token: leaked-value\n", encoding="utf-8")
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = None
    svc = MCPReviewService(_settings(), MagicMock(), vcs_factory=lambda owner, name: vcs)

    with caplog.at_level(logging.WARNING, logger="reviewer.mcp.service"):
        policy, meta = svc._resolve_policy("o/r", "dev")

    assert policy.context_limits == ContextLimits()
    assert meta.warnings
    assert "Слой policy пропущен" in caplog.text
    assert "github_token" in caplog.text
    assert "leaked-value" not in caplog.text


def test_resolve_policy_skips_invalid_home_value_without_logging_literal(
    caplog,
    isolated_xdg_config_home,
) -> None:
    secret = "do-not-echo"
    path = isolated_xdg_config_home / "rag-reviewer/repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"context_limits: {{graph: {{hops: {secret}}}}}\n"
        "future_policy: enabled\n",
        encoding="utf-8",
    )
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = "context_limits: {graph: {hops: 2}}\n"
    svc = MCPReviewService(_settings(), MagicMock(), vcs_factory=lambda owner, name: vcs)

    with caplog.at_level(logging.WARNING, logger="reviewer.mcp.service"):
        policy, meta = svc._resolve_policy("o/r", "dev")

    assert policy.context_limits.graph.hops == 2
    assert meta.sources["context_limits"] == ".review.yml"
    assert "home:repos/o/r.yml" in caplog.text
    assert secret not in caplog.text


def test_context_limits_failsoft_does_not_log_invalid_committed_literal(caplog) -> None:
    secret = "do-not-echo"
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = (
        f"context_limits: {{graph: {{hops: {secret}}}}}\n"
    )
    svc = MCPReviewService(_settings(), MagicMock(), vcs_factory=lambda owner, name: vcs)

    with caplog.at_level(logging.WARNING, logger="reviewer.mcp.service"):
        limits = svc._resolve_context_limits("o/r", "dev")

    assert limits == ContextLimits()
    assert "fail-soft" in caplog.text
    assert secret not in caplog.text


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


def test_home_repo_limits_survive_unavailable_vcs(isolated_xdg_config_home) -> None:
    """PRI-234: при недоступном VCS лимиты берутся из home-слоя, а не из env-дефолтов."""
    path = isolated_xdg_config_home / "rag-reviewer/repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("context_limits: {graph: {hops: 2}}\n", encoding="utf-8")
    s = _settings()
    components = MagicMock()
    vcs = MagicMock()
    vcs.get_file_at_ref.side_effect = RuntimeError("network down")
    svc = MCPReviewService(s, components, vcs_factory=lambda o, n: vcs)

    cl = svc._resolve_context_limits("o/r", "dev")

    # Дефолт graph.hops — 1; значение 2 доказывает, что home-слой применён,
    # хотя коммиченный слой недоступен.
    assert cl.graph.hops == 2


def test_home_repo_summary_depth_survives_unavailable_vcs(isolated_xdg_config_home) -> None:
    """PRI-234: summary_cluster_depth из home-слоя выживает при недоступном VCS."""
    path = isolated_xdg_config_home / "rag-reviewer/repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("summary_cluster_depth: 4\n", encoding="utf-8")
    s = _settings()
    components = MagicMock()
    vcs = MagicMock()
    vcs.get_file_at_ref.side_effect = RuntimeError("network down")
    svc = MCPReviewService(s, components, vcs_factory=lambda o, n: vcs)

    depth, overrides, ignore, source = svc._resolve_summary_layout("o/r", "dev")

    # Дефолт SUMMARY_CLUSTER_DEPTH — 2; значение 4 доказывает, что home-слой
    # применён, хотя коммиченный слой недоступен.
    assert depth == 4
    assert source == "home:repos/o/r.yml"
    from reviewer.graph.summaries import normalize_summary_paths_ignore
    from reviewer.policy.policy import DEFAULT_SUMMARY_PATHS_IGNORE
    assert ignore == normalize_summary_paths_ignore(DEFAULT_SUMMARY_PATHS_IGNORE)


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


def test_search_codebase_multi_passes_limits_and_hops(
    isolated_xdg_config_home,
) -> None:
    """_search_codebase_multi (PRI-255) пробрасывает в search_multi лимиты,
    резолвленные из эффективной .review.yml-политики — не дефолт-константы.

    Значения в домашнем слое НЕ совпадают с дефолтами (ceiling=15, hops=1),
    иначе тест не отличил бы резолв политики от захардкоженных CodebaseLimits().
    """
    path = isolated_xdg_config_home / "rag-reviewer/repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "context_limits: {graph: {hops: 2}, search_codebase: {ceiling: 7}}\n",
        encoding="utf-8")
    s = _settings()
    s.review_branches = "dev"          # branch="dev" должна быть отслеживаемой
    components = MagicMock()
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = None    # нет committed .review.yml
    svc = MCPReviewService(s, components, vcs_factory=lambda o, n: vcs)

    with patch("reviewer.retrieval.multiquery.search_multi") as sm:
        sm.return_value.as_context.return_value = "ok"
        svc._search_codebase_multi("o/r", ["q1", "q2"], branch="dev")

    call = sm.call_args
    assert isinstance(call.kwargs["limits"], CodebaseLimits)
    assert call.kwargs["limits"].ceiling == 7
    assert call.kwargs["hops"] == 2


def test_search_codebase_multi_passes_code_section_limits(
    isolated_xdg_config_home,
) -> None:
    """_search_codebase_multi (PRI-256) пробрасывает в search_multi файловый
    бюджет секции, резолвленный из эффективной .review.yml-политики.

    Значения в домашнем слое НЕ совпадают с дефолтами (12/1/1300), иначе тест
    не отличил бы честный резолв политики от захардкоженного CodeSectionLimits().
    """
    path = isolated_xdg_config_home / "rag-reviewer/repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "context_limits: {code_section: {max_files: 5, chars_per_file: 400}}\n",
        encoding="utf-8")
    s = _settings()
    s.review_branches = "dev"
    components = MagicMock()
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = None
    svc = MCPReviewService(s, components, vcs_factory=lambda o, n: vcs)

    with patch("reviewer.retrieval.multiquery.search_multi") as sm:
        sm.return_value.as_context.return_value = "ok"
        svc._search_codebase_multi("o/r", ["q1"], branch="dev")

    call = sm.call_args
    assert isinstance(call.kwargs["section_limits"], CodeSectionLimits)
    assert call.kwargs["section_limits"].max_files == 5
    assert call.kwargs["section_limits"].chars_per_file == 400
    assert call.kwargs["section_limits"].max_chunks_per_file == 1   # дефолт сохранён


def test_search_codebase_multi_forwards_augment_kwargs() -> None:
    """_search_codebase_multi (PRI-257/258) пробрасывает augment_sources в
    search_multi без изменений — сборка входа остаётся снаружи, в _TaskContextDeps."""
    from reviewer.retrieval.augment import AugmentSource

    s = _settings()
    s.review_branches = "dev"
    components = MagicMock()
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = None
    svc = MCPReviewService(s, components, vcs_factory=lambda o, n: vcs)
    sources = [AugmentSource(name="similar-diffs", paths=["a.py"], quota=3)]

    with patch("reviewer.retrieval.multiquery.search_multi") as sm:
        sm.return_value.as_context.return_value = "ok"
        svc._search_codebase_multi("o/r", ["q1"], branch="dev",
                                   augment_sources=sources)

    call = sm.call_args
    assert call.kwargs["augment_sources"] == sources


def test_search_codebase_multi_defaults_augment_kwargs_to_none() -> None:
    """test_exemplars и прочие вызовы без augment-сигнала не передают его вовсе."""
    s = _settings()
    s.review_branches = "dev"
    components = MagicMock()
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = None
    svc = MCPReviewService(s, components, vcs_factory=lambda o, n: vcs)

    with patch("reviewer.retrieval.multiquery.search_multi") as sm:
        sm.return_value.as_context.return_value = "ok"
        svc._search_codebase_multi("o/r", ["q1"], branch="dev")

    call = sm.call_args
    assert call.kwargs["augment_sources"] is None
