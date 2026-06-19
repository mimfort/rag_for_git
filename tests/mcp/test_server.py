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
    """create_server регистрирует ровно 19 ожидаемых MCP-тулов."""
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
        "index_task",
        "index_tasks_batch",
        "purge_orphaned_tasks",
        "search_tasks",
        "get_task_context",
        "get_board_config",
        "publish_review",
        "search_codebase",
        "related_symbols",
        "callers",
        "definition",
        "get_pr_diff",
    }


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
    """Smoke: publish_review через MCP-слой — pydantic-валидация findings
    (list[dict]) и сериализация dict-отчёта в TextContent."""
    from reviewer.entrypoints.mcp_server import create_server

    server = create_server(_make_mcp_service())
    asyncio.run(server.call_tool("prepare_review", {"repo": "o/r", "pr": 7}))
    finding = {
        "category": "correctness", "severity": "high", "file": "a.py",
        "line": 1, "code_quote": None, "message": "bug", "suggestion": None,
        "fix": None, "confidence": 0.9,
    }
    result = asyncio.run(server.call_tool(
        "publish_review",
        {"repo": "o/r", "pr": 7, "summary": "s",
         "findings": [finding], "dry_run": True},
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
