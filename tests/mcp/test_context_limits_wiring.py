"""Тесты effective layered policy для context limits в session-less MCP-тулах.

_resolve_context_limits fail-soft объединяет env, home-слои и committed
`.review.yml` (зеркало `_resolve_summary_layout`). `search_codebase` передаёт
limits/hops/ceiling_override в `Retriever.search_base`.
"""
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from reviewer.config.layers import build_config_report, resolve_policy_data
from reviewer.config.settings import Settings
from reviewer.index.store import Retrieved
from reviewer.mcp.schemas import BriefCitationIn
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
    assert meta.sources["context_limits.graph.hops"] == ".review.yml"
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

    Значения в домашнем слое НЕ совпадают с дефолтами (20/1/975), иначе тест
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


def test_git_file_limit_is_reported_from_effective_policy_layer(
    isolated_xdg_config_home,
) -> None:
    path = isolated_xdg_config_home / "rag-reviewer/repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("context_limits: {code_section: {max_git_files: 3}}\n", encoding="utf-8")
    data, meta = resolve_policy_data(
        "o/r", "dev", lambda ref: "context_limits: {code_section: {max_git_files: 1}}",
    )

    report = build_config_report("o/r", "dev", _settings(), data, meta)

    assert report["effective"]["context_limits"]["code_section"]["max_git_files"] == 3
    assert report["sources"]["context_limits.code_section.max_git_files"] == "home:repos/o/r.yml"


@pytest.mark.parametrize("value", ["4", "-1", "0.5", "true", "'2'"])
def test_invalid_home_git_file_limit_preserves_committed_layer(
    isolated_xdg_config_home, value: str,
) -> None:
    path = isolated_xdg_config_home / "rag-reviewer/repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text(f"context_limits: {{code_section: {{max_git_files: {value}}}}}\n",
                    encoding="utf-8")
    data, meta = resolve_policy_data(
        "o/r", "dev", lambda ref: "context_limits: {code_section: {max_git_files: 1}}",
    )

    report = build_config_report("o/r", "dev", _settings(), data, meta)

    assert report["effective"]["context_limits"]["code_section"]["max_git_files"] == 1
    assert report["sources"]["context_limits.code_section.max_git_files"] == ".review.yml"


@pytest.mark.parametrize("value", ["4", "-1", "0.5", "true", "'2'"])
def test_invalid_committed_git_file_limit_falls_back_to_default(value: str) -> None:
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = f"context_limits: {{code_section: {{max_git_files: {value}}}}}"
    service = MCPReviewService(_settings(), MagicMock(), vcs_factory=lambda *_: vcs)

    limits = service._resolve_context_limits("o/r", "dev")

    assert limits.code_section.max_git_files == 0


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def lexical_clone(tmp_path) -> Path:
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "dev")
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "remote", "add", "origin", "https://github.com/o/r.git")
    (clone / ".review.yml").write_text(
        "context_limits: {code_section: {max_files: 3, max_git_files: 2}}\n",
        encoding="utf-8",
    )
    configs = clone / "configs"
    configs.mkdir()
    (configs / "schema.yml").write_text("schema:\n  sentinelword: committed\n",
                                        encoding="utf-8")
    (configs / "ignored.yml").write_text("sentinelword: private\n", encoding="utf-8")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "Добавить конфигурацию")
    return clone


def _task_context_service(
    *, registered_clone: Path | None = None, committed_policy: str | None = None,
    task: dict | None = None,
) -> MCPReviewService:
    settings = _settings()
    settings.review_branches = "dev,missing"
    components = MagicMock()
    components.store.get_repo_clone.return_value = str(registered_clone) if registered_clone else None
    components.store.get_index_meta_row.return_value = None
    components.graph.count_nodes.return_value = 0
    components.summary_store.count_summaries.return_value = 0
    components.task_service.get_task_context.return_value = ""
    components.task_service.get_task.return_value = task
    components.task_service.search_hits.return_value = []
    components.task_service.render_hits.return_value = ""
    retriever = components.retriever
    retriever.graph = None
    retriever.embedder.embed_queries.side_effect = lambda queries: [[0.1] for _ in queries]
    retriever.store.hybrid_search.return_value = [
        Retrieved("hybrid.py#f", "hybrid.py", "f", "function", 1, 1, "x = 1", 0.0, bm25_hit=True),
        Retrieved(
            "tests/test_hybrid.py#test_f", "tests/test_hybrid.py", "test_f", "function",
            1, 1, "assert x == 1", 0.0, bm25_hit=True,
        ),
    ]
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = committed_policy
    return MCPReviewService(settings, components, vcs_factory=lambda *_: vcs)


def test_prepare_task_context_verifies_git_citation_from_selected_branch(
    lexical_clone, isolated_xdg_config_home,
) -> None:
    policy = isolated_xdg_config_home / "rag-reviewer/repos/o/r.yml"
    policy.parent.mkdir(parents=True)
    policy.write_text("paths: {ignore: [configs/ignored.yml]}\n", encoding="utf-8")
    sha = _git(lexical_clone, "rev-parse", "dev")
    _git(lexical_clone, "switch", "-c", "other")
    schema = lexical_clone / "configs/schema.yml"
    schema.write_text("schema:\n  sentinelword: other-branch\n", encoding="utf-8")
    _git(lexical_clone, "commit", "-am", "Изменить схему")
    schema.write_text("schema:\n  sentinelword: worktree\n", encoding="utf-8")
    task = {
        "key": "ID-1", "title": "Investigate",
        "description": "background\n" * 8 + "sentinelword",
    }
    service = _task_context_service(registered_clone=lexical_clone, task=task)

    payload = service.prepare_task_context(
        "o/r", "ID-1", "dev", warm_board=False, include_brief_evidence=True,
        include_code_budget=True,
    )

    assert f"git-lexical@{sha}" in payload["code"]
    assert "configs/schema.yml" in payload["code"]
    assert "sentinelword: committed" in payload["code"]
    assert "worktree" not in payload["code"]
    assert "other-branch" not in payload["code"]
    assert "configs/ignored.yml" not in payload["code"]
    assert "configs/schema.yml" not in payload["test_exemplars"]
    assert "tests/test_hybrid.py" in payload["test_exemplars"]
    assert payload["code_budget"]["git_lexical_files"] == 1
    assert payload["code_budget"]["visible_files"] == 2
    assert payload["brief_source_sha"] == sha
    assert payload["brief_line_checks"]["test_exemplars"]["unavailable"] == 1
    checked = service.verify_task_brief(
        payload["brief_evidence_id"],
        [
            BriefCitationIn(section="code", path="configs/schema.yml", line=2),
            BriefCitationIn(section="code", path="configs/schema.yml", line=3),
            BriefCitationIn(section="test_exemplars", path="configs/schema.yml", line=2),
        ],
    )
    assert [citation["status"] for citation in checked["citations"]] == [
        "verified", "outside_excerpt", "section_unavailable",
    ]


@pytest.mark.parametrize("pathspec_env", [None, "GIT_LITERAL_PATHSPECS"])
def test_prepare_task_context_git_search_uses_free_text_with_explicit_clone(
    lexical_clone, pathspec_env, monkeypatch,
) -> None:
    (lexical_clone / ".review.yml").write_text(
        "paths: {ignore: [configs/ignored.yml]}\n", encoding="utf-8",
    )
    _git(lexical_clone, "commit", "-am", "Игнорировать конфигурацию")
    service = _task_context_service(
        committed_policy="context_limits: {code_section: {max_git_files: 2}}",
    )
    if pathspec_env:
        monkeypatch.setenv(pathspec_env, "1")

    payload = service.prepare_task_context(
        "o/r", "sentinelword", "dev", str(lexical_clone), warm_board=False,
    )

    assert "git-lexical@" in payload["code"]
    assert "configs/schema.yml" in payload["code"]
    assert "configs/ignored.yml" not in payload["code"]


def test_prepare_task_context_uses_ignore_from_same_git_snapshot_despite_replace_ref(
    lexical_clone,
) -> None:
    policy = lexical_clone / ".review.yml"
    policy.write_text("paths: {ignore: [configs/ignored.yml]}\n", encoding="utf-8")
    _git(lexical_clone, "commit", "-am", "Игнорировать конфигурацию")
    sha = _git(lexical_clone, "rev-parse", "dev")
    _git(lexical_clone, "switch", "-c", "replacement")
    policy.write_text("paths: {ignore: []}\n", encoding="utf-8")
    _git(lexical_clone, "commit", "-am", "Изменить политику")
    _git(lexical_clone, "replace", sha, "replacement")
    service = _task_context_service(
        committed_policy="context_limits: {code_section: {max_git_files: 2}}",
    )

    payload = service.prepare_task_context(
        "o/r", "sentinelword", "dev", str(lexical_clone), warm_board=False,
    )

    assert f"git-lexical@{sha}" in payload["code"]
    assert "configs/schema.yml" in payload["code"]
    assert "configs/ignored.yml" not in payload["code"]


@pytest.mark.parametrize("invalid_policy", ["paths: [", "paths: {ignore: ignored.yml}"])
def test_prepare_task_context_git_search_requires_readable_committed_ignore_policy(
    lexical_clone, invalid_policy: str,
) -> None:
    (lexical_clone / ".review.yml").write_text(invalid_policy, encoding="utf-8")
    _git(lexical_clone, "commit", "-am", "Добавить невалидную политику")
    service = _task_context_service(
        committed_policy="context_limits: {code_section: {max_git_files: 2}}",
    )

    payload = service.prepare_task_context(
        "o/r", "sentinelword", "dev", str(lexical_clone), warm_board=False,
    )

    assert "hybrid.py" in payload["code"]
    assert "git-lexical@" not in payload["code"]
    assert any(gap["section"] == "code.git_lexical" for gap in payload["gaps"])


@pytest.mark.parametrize("failure", ["missing_clone", "foreign_clone", "missing_branch"])
def test_prepare_task_context_keeps_hybrid_results_when_git_fails(
    lexical_clone, failure: str,
) -> None:
    branch = "missing" if failure == "missing_branch" else "dev"
    if failure == "foreign_clone":
        _git(lexical_clone, "remote", "set-url", "origin", "https://github.com/o/other.git")
    service = _task_context_service(
        registered_clone=None if failure == "missing_clone" else lexical_clone,
        committed_policy="context_limits: {code_section: {max_git_files: 1}}",
    )

    payload = service.prepare_task_context("o/r", "sentinelword", branch, warm_board=False)

    assert "hybrid.py" in payload["code"]
    assert "git-lexical@" not in payload["code"]
    assert any(gap["section"] == "code.git_lexical" for gap in payload["gaps"])


def test_prepare_task_context_keeps_hybrid_results_without_git_matches(lexical_clone) -> None:
    service = _task_context_service(registered_clone=lexical_clone)

    payload = service.prepare_task_context("o/r", "unfindableword", "dev", warm_board=False)

    assert "hybrid.py" in payload["code"]
    assert "git-lexical@" not in payload["code"]
    assert all(gap["section"] != "code.git_lexical" for gap in payload["gaps"])


def test_prepare_task_context_default_git_quota_never_opens_explicit_clone(
    tmp_path, monkeypatch,
) -> None:
    service = _task_context_service()

    calls = []
    real_run = subprocess.run

    def record_run(args, *other, **kwargs):
        if args[0] == "git":
            calls.append(args)
        return real_run(args, *other, **kwargs)

    monkeypatch.setattr(subprocess, "run", record_run)
    payload = service.prepare_task_context(
        "o/r", "sentinelword", "dev", str(tmp_path), warm_board=False,
    )

    assert "hybrid.py" in payload["code"]
    assert calls == []


def test_prepare_task_context_keeps_explicit_paths_in_their_sections_and_applies_ignore():
    service = _task_context_service(
        committed_policy="paths: {ignore: [pkg/ignored.py]}\n",
        task={
            "key": "ID-1", "title": "Use pkg/explicit.py and tests/test_explicit.py",
            "description": (
                "Проверить pkg/ignored.py и https://example.org/pkg/linked.py.\n"
                "## Контекст\nСправка pkg/historical.py."
            ),
        },
    )
    exact = ["pkg/explicit.py", "tests/test_explicit.py"]
    retriever = service.components.retriever
    retriever.store.fetch_retrieved_at_paths.side_effect = lambda repo, paths, *, base_ref: [
        Retrieved(f"{path}#f", path, "f", "function", 1, 1, "def f(): pass", 0.0)
        for path in paths
    ]

    payload = service.prepare_task_context("o/r", "ID-1", "dev", warm_board=False)

    assert exact[0] in payload["code"] and exact[1] not in payload["code"]
    assert exact[1] in payload["test_exemplars"] and exact[0] not in payload["test_exemplars"]
    assert [c.args for c in retriever.store.fetch_retrieved_at_paths.call_args_list] == [
        ("o/r", [exact[0]]), ("o/r", [exact[1]]),
    ]
    assert all(c.kwargs == {"base_ref": "base:dev"}
               for c in retriever.store.fetch_retrieved_at_paths.call_args_list)
    retriever.store.fetch_retrieved_covering_lines.assert_not_called()


@pytest.mark.parametrize(("path", "section"), [
    ("pkg/requested.py", "code"),
    ("tests/test_requested.py", "test_exemplars"),
])
def test_prepare_task_context_verifies_requested_line_after_excerpt_limit(lexical_clone, path, section):
    policy = "context_limits: {code_section: {max_files: 1}}"
    (lexical_clone / ".review.yml").write_text(policy, encoding="utf-8")
    file = lexical_clone / path
    file.parent.mkdir()
    lines = ["def target():", *(f"    value = 'fixture value number {i}'" for i in range(80))]
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(lexical_clone, "add", ".")
    _git(lexical_clone, "commit", "-m", "Добавить запрос строки")
    sha = _git(lexical_clone, "rev-parse", "dev")
    s = _task_context_service(registered_clone=lexical_clone, committed_policy=policy)
    s.components.store.get_index_meta_row.return_value = (sha, datetime.now(UTC))
    store = s.components.retriever.store
    item = Retrieved(f"{path}#target", path, "target", "function",
                     1, len(lines), "\n".join(lines), 0.0, bm25_hit=True)
    store.hybrid_search.return_value = [item]
    store.fetch_retrieved_covering_lines.return_value = [item]

    pack = s.prepare_task_context(
        "o/r", f"Проверить поведение `{path}:70`", "dev", warm_board=False,
        include_brief_evidence=True,
    )
    checked = s.verify_task_brief(pack["brief_evidence_id"], [
        BriefCitationIn(section=section, path=path, line=70),
        BriefCitationIn(section=section, path=path, line=82),
    ])

    assert [c["status"] for c in checked["citations"]] == ["verified", "outside_excerpt"]
    assert f"   70 | {lines[69]}" in pack[section]
    assert pack["brief_source_sha"] == sha
    store.fetch_retrieved_covering_lines.assert_called_once_with("o/r", {path: 70}, base_ref="base:dev")


@pytest.mark.parametrize("failed", [False, True])
def test_prepare_task_context_no_covering_line_keeps_ranked_excerpt(failed):
    policy = "context_limits: {code_section: {max_files: 1}}"
    s = _task_context_service(committed_policy=policy)
    store = s.components.retriever.store
    path = "pkg/ranked.py"
    item = Retrieved(f"{path}#target", path, "target", "function",
                     1, 1, "def target(): pass", 0.0, bm25_hit=True)
    store.hybrid_search.return_value = [item]
    if failed:
        store.fetch_retrieved_covering_lines.side_effect = psycopg.OperationalError("connection refused")
    else:
        store.fetch_retrieved_covering_lines.return_value = []

    pack = s.prepare_task_context("o/r", f"Проверить `{path}:999`", "dev", warm_board=False)
    assert "def target(): pass" in pack["code"]
    assert "999 |" not in pack["code"]
    store.fetch_retrieved_covering_lines.assert_called_once_with("o/r", {path: 999}, base_ref="base:dev")
    gaps = [g for g in pack["gaps"] if g["section"] == "code.explicit_lines"]
    assert [g["cause"] for g in gaps] == (["storage_unavailable"] if failed else [])
