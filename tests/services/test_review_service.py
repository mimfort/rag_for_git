"""Unit-тесты ReviewService.

Мокаем VCS-провайдер, Store, Graph, LLM-компоненты и LangGraph.
Проверяем оркестрацию, cleanup, history и fail-soft.
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from reviewer.config.settings import Settings
from reviewer.services.review_service import ReviewService
from reviewer.vcs.base import (
    ChangedFile,
    Finding,
    InlineComment,
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
    s.review_trace = False
    s.review_verdict_log = ""
    s.review_synthesis = False
    s.review_skip_drafts = True
    s.review_max_files = 50
    s.openrouter_api_key = "test"
    s.voyage_api_key = "test"
    s.github_token = "test"
    return s


@pytest.fixture
def components() -> MagicMock:
    """Фейковые компоненты (store, embedder, retriever, graph, llm)."""
    c = MagicMock()
    c.store = MagicMock()
    c.embedder = MagicMock()
    c.retriever = MagicMock()
    c.graph = MagicMock()
    c.llm_provider = MagicMock()
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


def _graph_return(
    *,
    findings: list[Finding] | None = None,
    verified: list[Finding] | None = None,
    summary: str = "",
    inline_comments: list[InlineComment] | None = None,
    published: bool | None = True,
    failed_units: list[str] | None = None,
) -> MagicMock:
    """Мок compiled-графа с заданным результатом invoke."""
    g = MagicMock()
    g.invoke.return_value = {
        "review_units": [],
        "findings": findings or [],
        "failed_units": failed_units or [],
        "verified": verified or [],
        "summary": summary,
        "inline_comments": inline_comments or [],
        "published": published,
    }
    return g


# ---------------------------------------------------------------------------
# Тесты draft-skip
# ---------------------------------------------------------------------------

def test_run_review_draft_skip(settings: Settings, components: MagicMock) -> None:
    """Draft-PR пропускается, is_draft_skip=True, VCS закрыт."""
    vcs = _vcs_with_files([_changed()], draft=True)
    service = ReviewService(settings, components)

    with patch.object(service, "_create_vcs_provider", return_value=vcs):
        result = service.run_review("owner", "repo", 1, dry_run=False)

    assert result.is_draft_skip is True
    assert result.duration_ms == 0
    vcs.close.assert_called_once()


# ---------------------------------------------------------------------------
# Тесты нормального прогона
# ---------------------------------------------------------------------------

@patch("reviewer.services.review_service.build_graph")
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_run_review_published(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    mock_graph_build: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Успешный прогон: published=True, cleanup выполнен."""
    vcs = _vcs_with_files([_changed("a.py")])
    finding = Finding("correctness", "high", "a.py", 2, "RIGHT", "bug", None, 0.9)
    graph = _graph_return(
        findings=[finding],
        verified=[finding],
        summary="OK",
        published=True,
    )
    mock_graph_build.return_value = graph

    service = ReviewService(settings, components)
    with patch.object(service, "_create_vcs_provider", return_value=vcs):
        result = service.run_review("owner", "repo", 1, dry_run=False)

    assert result.is_draft_skip is False
    assert result.published_flag is True
    assert result.state["summary"] == "OK"
    # delete_ref вызван дважды: self-healing в начале prepare() + cleanup в finally
    assert components.store.delete_ref.call_args_list == [
        call("pr:1"), call("pr:1"),
    ]
    # store и graph НЕ закрываются сервисом — caller управляет их жизненным циклом
    vcs.close.assert_called_once()


@patch("reviewer.services.review_service.build_graph")
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_run_review_dry_run(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    mock_graph_build: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Dry-run: граф собирается с publish=False."""
    vcs = _vcs_with_files([_changed("a.py")])
    graph = _graph_return(summary="dry", published=None)
    mock_graph_build.return_value = graph

    service = ReviewService(settings, components)
    with patch.object(service, "_create_vcs_provider", return_value=vcs):
        result = service.run_review("owner", "repo", 1, dry_run=True)

    assert result.dry_run is True
    assert result.published_flag is None
    # build_graph вызван с publish=False
    call_kwargs = mock_graph_build.call_args[1]
    assert call_kwargs.get("publish") is False


@patch("reviewer.services.review_service.build_graph")
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_run_review_publish_failure(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    mock_graph_build: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Ошибка публикации: published_flag=False, publish_failed заполнен."""
    vcs = _vcs_with_files([_changed("a.py")])
    graph = _graph_return(
        summary="err",
        published=False,
        failed_units=["publish: NetworkError"],
    )
    mock_graph_build.return_value = graph

    service = ReviewService(settings, components)
    with patch.object(service, "_create_vcs_provider", return_value=vcs):
        result = service.run_review("owner", "repo", 1, dry_run=False)

    assert result.published_flag is False
    assert result.publish_failed == ["publish: NetworkError"]


# ---------------------------------------------------------------------------
# Тесты max_files
# ---------------------------------------------------------------------------

@patch("reviewer.services.review_service.build_graph")
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_run_review_respects_max_files(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    mock_graph_build: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """При превышении max_files overlay и юниты содержат ровно max_files файлов."""
    settings.review_max_files = 2
    files = [
        _changed("a.py", patch="@@ -1 +1 @@\n x\n@@ -2 +2 @@\n y"),
        _changed("b.py", patch="@@ -1 +1 @@\n x"),
        _changed("c.py", patch="@@ -1 +1 @@\n x"),
    ]
    vcs = _vcs_with_files(files)
    graph = _graph_return(summary="limited")
    mock_graph_build.return_value = graph

    service = ReviewService(settings, components)
    with patch.object(service, "_create_vcs_provider", return_value=vcs):
        result = service.run_review("owner", "repo", 1, dry_run=False)

    # overlay вызван с 2 файлами (4-й позиционный аргумент)
    overlay_call = _mock_overlay.call_args
    assert len(overlay_call[0][3]) == 2
    # skipped_paths содержит 1 файл
    assert result.skipped_paths is not None
    assert len(result.skipped_paths) == 1


# ---------------------------------------------------------------------------
# Тесты cleanup
# ---------------------------------------------------------------------------

@patch("reviewer.services.review_service.build_graph")
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_cleanup_runs_even_on_exception(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    mock_graph_build: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Cleanup (delete_ref, close) выполняется даже при падении графа."""
    vcs = _vcs_with_files([_changed("a.py")])
    mock_graph_build.side_effect = RuntimeError("boom")

    service = ReviewService(settings, components)
    with patch.object(service, "_create_vcs_provider", return_value=vcs):
        with pytest.raises(RuntimeError, match="boom"):
            service.run_review("owner", "repo", 1, dry_run=False)

    # delete_ref вызван дважды: self-healing в начале prepare() + cleanup в finally
    assert components.store.delete_ref.call_args_list == [
        call("pr:1"), call("pr:1"),
    ]
    # store и graph НЕ закрываются сервисом — caller управляет их жизненным циклом
    vcs.close.assert_called_once()


# ---------------------------------------------------------------------------
# Тесты истории
# ---------------------------------------------------------------------------

@patch("reviewer.services.review_service.ReviewHistory")
@patch("reviewer.services.review_service.build_graph")
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_history_recorded_on_success(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    mock_graph_build: MagicMock,
    mock_history_cls: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """При review_history=True успешный прогон пишется в историю."""
    settings.review_history = True
    mock_history = MagicMock()
    mock_history_cls.return_value = mock_history

    vcs = _vcs_with_files([_changed("a.py")])
    finding = Finding("correctness", "high", "a.py", 2, "RIGHT", "bug", None, 0.9)
    comment = InlineComment(
        "a.py", 2, "RIGHT", "body <!-- ai-review:abc1234567890ab -->"
    )
    graph = _graph_return(
        findings=[finding],
        verified=[finding],
        summary="OK",
        inline_comments=[comment],
        published=True,
    )
    mock_graph_build.return_value = graph

    service = ReviewService(settings, components)
    with patch.object(service, "_create_vcs_provider", return_value=vcs):
        result = service.run_review("owner", "repo", 1, dry_run=False)

    assert result.is_draft_skip is False
    mock_history.record_run.assert_called_once()
    call_args = mock_history.record_run.call_args
    run_dict = call_args[0][0]
    assert run_dict["status"] == "ok"
    assert run_dict["files_reviewed"] == 1
    assert run_dict["comments_inline"] == 1
    # history закрыта, т.к. создана внутри сервиса
    mock_history.close.assert_called_once()


@patch("reviewer.services.review_service.ReviewHistory")
@patch("reviewer.services.review_service.build_graph")
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_history_fail_soft(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    mock_graph_build: MagicMock,
    mock_history_cls: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Ошибка записи в историю не ломает прогон."""
    settings.review_history = True
    mock_history = MagicMock()
    mock_history.record_run.side_effect = RuntimeError("db down")
    mock_history_cls.return_value = mock_history

    vcs = _vcs_with_files([_changed("a.py")])
    graph = _graph_return(summary="OK", published=True)
    mock_graph_build.return_value = graph

    service = ReviewService(settings, components)
    with patch.object(service, "_create_vcs_provider", return_value=vcs):
        result = service.run_review("owner", "repo", 1, dry_run=False)

    # Прогон завершился успешно несмотря на ошибку истории
    assert result.state["summary"] == "OK"


@patch("reviewer.services.review_service.ReviewHistory")
def test_draft_skip_records_history(
    mock_history_cls: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Draft-PR записывает draft_skip в историю."""
    settings.review_history = True
    mock_history = MagicMock()
    mock_history_cls.return_value = mock_history

    vcs = _vcs_with_files([_changed()], draft=True)
    service = ReviewService(settings, components)
    with patch.object(service, "_create_vcs_provider", return_value=vcs):
        result = service.run_review("owner", "repo", 1, dry_run=False)

    assert result.is_draft_skip is True
    mock_history.record_run.assert_called_once()
    run_dict = mock_history.record_run.call_args[0][0]
    assert run_dict["status"] == "draft_skip"


# ---------------------------------------------------------------------------
# Тесты: внешний vcs_provider (eval) не трогает прод base-индекс
# ---------------------------------------------------------------------------

@patch("reviewer.services.review_service.update_base")
@patch("reviewer.services.review_service.build_graph")
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_custom_vcs_provider_does_not_touch_base_index(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    mock_graph_build: MagicMock,
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
    mock_graph_build.return_value = _graph_return(summary="OK")

    service = ReviewService(settings, components)
    service.run_review("owner", "repo", 1, dry_run=True, vcs_provider=vcs)

    mock_update_base.assert_not_called()
    components.store.set_index_meta.assert_not_called()


@patch("reviewer.services.review_service.ReviewHistory")
@patch("reviewer.services.review_service.build_graph")
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_owned_history_reset_after_run(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    mock_graph_build: MagicMock,
    mock_history_cls: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Сервис, владеющий history, после прогона сбрасывает кэш (_history=None) —
    иначе повторный run_review вернул бы уже закрытый пул и тихо терял историю."""
    settings.review_history = True
    mock_history = MagicMock()
    mock_history_cls.return_value = mock_history

    vcs = _vcs_with_files([_changed("a.py")])
    mock_graph_build.return_value = _graph_return(summary="OK", published=True)

    service = ReviewService(settings, components)
    with patch.object(service, "_create_vcs_provider", return_value=vcs):
        service.run_review("owner", "repo", 1, dry_run=False)

    mock_history.close.assert_called_once()
    assert service._history is None


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
    """prepare() собирает юниты, policy и overlay без запуска LLM-графа."""
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
    components.store.delete_ref.assert_called_once_with("pr:7")


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
    components.store.set_index_meta.assert_called_once_with("base", "base123")


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
        call("pr:1"), call("pr:1"),
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
