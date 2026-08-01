"""Unit-тесты MCP-сервера reviewer-mcp (FastMCP).

Проверяем регистрацию 8 тулов и базовую маршрутизацию вызовов через MCP-слой.
Фабрика фейков адаптирована из tests/mcp/test_service.py.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from reviewer.config.settings import Settings
from reviewer.mcp.schemas import SubtaskIn, SummaryFragmentIn
from reviewer.mcp.service import MCPReviewService
from reviewer.vcs.base import ChangedFile, PullRequest

# ---------------------------------------------------------------------------
# Фейки (по образцу tests/mcp/test_service.py)
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
    c.retriever = MagicMock()
    c.retriever.retrieve.return_value.as_context.return_value = "(результат поиска)"
    c.graph = MagicMock()
    c.graph.expand.return_value = set()
    c.graph.callers.return_value = set()
    c.graph.find_symbol.return_value = []
    c.llm_provider = MagicMock()
    return c


def _fake_vcs(number: int = 7) -> MagicMock:
    vcs = MagicMock()
    vcs.get_pull_request.return_value = PullRequest(
        number=number,
        base_sha="base123",
        head_sha="head456",
        base_ref="main",
        title="Test PR",
        body="",
        draft=False,
    )
    vcs.get_changed_files.return_value = [
        ChangedFile(path="a.py", status="modified", patch="@@ -1 +1 @@\n x"),
    ]
    vcs.get_file_at_ref.return_value = "def foo(): pass"
    return vcs


class _FakeChunk:
    def __init__(self, node_id: str) -> None:
        self.node_id = node_id


def _fake_chunk(path: str, source: bytes) -> list[_FakeChunk]:
    return [_FakeChunk(f"{path}#foo")]


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
# Тесты
# ---------------------------------------------------------------------------

def test_server_registers_all_tools() -> None:
    """create_server регистрирует ровно 38 ожидаемых MCP-тулов."""
    from reviewer.entrypoints.mcp_server import create_server

    server = create_server(_make_mcp_service())
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "prepare_review",
        "search_code",
        "get_related_symbols",
        "read_file",
        "get_definition",
        "find_callers",
        "get_changed_file_diff",
        "get_impact",
        "index_task",
        "index_tasks_batch",
        "purge_orphaned_tasks",
        "sync_board",
        "finish_task",
        "create_task",
        "create_subtasks",
        "search_tasks",
        "get_task_context",
        "get_task",
        "count_tasks",
        "get_board_config",
        "get_board_targets",
        "publish_review",
        "submit_findings",
        "submit_verdicts",
        "get_candidate_findings",
        "post_pr_walkthrough",
        "search_codebase",
        "related_symbols",
        "callers",
        "implementations",
        "definition",
        "get_pr_diff",
        "list_subsystem_clusters",
        "get_subsystem_summary_work",
        "index_subsystem_summary",
        "get_subsystem_summaries",
        "prune_subsystem_summaries",
        "backfill_summary_embeddings",
    }


def test_create_subtasks_routes_validated_items_to_service_once() -> None:
    from reviewer.entrypoints.mcp_server import create_server

    service = MagicMock(spec=MCPReviewService)
    service.create_subtasks.return_value = {"status": "ok"}
    server = create_server(service)
    payload = {
        "parent_key": "PRI-224",
        "subtasks": [{
            "title": " Child ",
            "problem": " Problem ",
            "steps": [" Step "],
            "criteria": [" Done "],
            "context": " ",
        }],
        "idempotency_key": "attempt-1",
        "board_type": "yougile",
        "project": "PRI",
        "provider_options": {"lane": "Backend"},
    }

    result = asyncio.run(server.call_tool("create_subtasks", payload))

    assert json.loads(result[0].text) == {"status": "ok"}
    service.create_subtasks.assert_called_once_with(
        "PRI-224",
        [
            SubtaskIn(
                title="Child",
                problem="Problem",
                steps=["Step"],
                criteria=["Done"],
                context=None,
            ).model_dump()
        ],
        "attempt-1",
        "yougile",
        "PRI",
        {"lane": "Backend"},
    )


def test_list_subsystem_clusters_tool_describes_layout_token() -> None:
    from reviewer.entrypoints.mcp_server import create_server

    tools = asyncio.run(create_server(_make_mcp_service()).list_tools())
    tool = next(item for item in tools if item.name == "list_subsystem_clusters")

    assert "layout_token" in (tool.description or "")


def test_index_subsystem_summary_routes_typed_fragments_to_service() -> None:
    """FastMCP валидирует file fragments и передаёт Pydantic-модели сервису."""
    from reviewer.entrypoints.mcp_server import create_server

    service = MagicMock(spec=MCPReviewService)
    service.index_subsystem_summary.return_value = {"stored": True}
    server = create_server(service)

    result = asyncio.run(
        server.call_tool(
            "index_subsystem_summary",
            {
                "repo": "o/r",
                "branch": "dev",
                "cluster_key": "reviewer/index",
                "title": "Индекс",
                "summary": "Тело",
                "source_hash": "hash",
                "fragments": [
                    {
                        "path": "reviewer/index/a.py",
                        "fingerprint": "file-hash",
                        "summary": "A",
                        "provenance": {"generator": "test"},
                    }
                ],
            },
        )
    )

    assert json.loads(result[0].text) == {"stored": True}
    args = service.index_subsystem_summary.call_args.args
    assert args[:6] == (
        "o/r",
        "dev",
        "reviewer/index",
        "Индекс",
        "Тело",
        "hash",
    )
    assert args[6] == [
        SummaryFragmentIn(
            path="reviewer/index/a.py",
            fingerprint="file-hash",
            summary="A",
            provenance={"generator": "test"},
        )
    ]


def test_prune_subsystem_summaries_routes_verified_snapshot_to_service() -> None:
    from reviewer.entrypoints.mcp_server import create_server

    service = MagicMock(spec=MCPReviewService)
    service.prune_subsystem_summaries.return_value = {"completed": True}
    server = create_server(service)

    result = asyncio.run(
        server.call_tool(
            "prune_subsystem_summaries",
            {
                "repo": "o/r",
                "branch": "dev",
                "layout_token": "layout-token",
                "expected_source_hashes": {
                    "reviewer/index": "source-hash",
                },
            },
        )
    )

    assert json.loads(result[0].text) == {"completed": True}
    service.prune_subsystem_summaries.assert_called_once_with(
        "o/r",
        "dev",
        "layout-token",
        {"reviewer/index": "source-hash"},
    )


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_callable_via_mcp(_ov, _ch) -> None:
    """prepare_review вызывается через MCP-слой и возвращает данные с units.

    call_tool возвращает list[ContentBlock] (TextContent с JSON-сериализованным dict).
    Из первого элемента извлекаем text и проверяем ключевые поля.
    """
    from reviewer.entrypoints.mcp_server import create_server

    server = create_server(_make_mcp_service())
    result = asyncio.run(server.call_tool("prepare_review", {"repo": "o/r", "pr": 7}))

    # call_tool возвращает Sequence[ContentBlock] — первый элемент TextContent
    assert result, "ожидали непустой результат"
    first = result[0]
    # TextContent имеет атрибут .text с JSON-строкой
    data = json.loads(first.text)
    assert "units" in data
    assert data["pr"]["number"] == 7


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_review_dry_run_callable_via_mcp(_ov, _ch) -> None:
    """Smoke: submit_findings + publish_review через MCP-слой (PRI-156).

    Находки сдаются через submit_findings (schema-enforced FindingIn),
    publish_review вызывается без findings и возвращает dict-отчёт.
    """
    from reviewer.entrypoints.mcp_server import create_server
    from tests.mcp.test_publish import RAW

    server = create_server(_make_mcp_service())
    asyncio.run(server.call_tool("prepare_review", {"repo": "o/r", "pr": 7}))
    asyncio.run(server.call_tool(
        "submit_findings",
        {"repo": "o/r", "pr": 7, "findings": [RAW]},
    ))
    result = asyncio.run(server.call_tool(
        "publish_review",
        {"repo": "o/r", "pr": 7, "summary": "s", "dry_run": True},
    ))

    assert result, "ожидали непустой результат"
    data = json.loads(result[0].text)
    assert data["dry_run"] is True
    assert data["posted"] is False
    assert "inline" in data


def test_search_code_without_prepare_reports_error() -> None:
    """Вызов search_code без prepare_review через MCP-слой поднимает ToolError.

    FastMCP оборачивает исключение тула в ToolError (mcp.server.fastmcp.exceptions).
    Сообщение ошибки должно содержать «prepare_review».
    """
    from mcp.server.fastmcp.exceptions import ToolError

    from reviewer.entrypoints.mcp_server import create_server

    server = create_server(_make_mcp_service())

    with pytest.raises(ToolError, match="prepare_review"):
        asyncio.run(server.call_tool("search_code", {"repo": "o/r", "pr": 7, "query": "foo"}))
