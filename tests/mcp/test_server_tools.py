"""create_server регистрирует тулы задач и пробрасывает task_key в publish_review."""
from unittest.mock import MagicMock

from reviewer.entrypoints.mcp_server import create_server


def _service() -> MagicMock:
    s = MagicMock()
    s.index_task.return_value = {"key": "ID-1"}
    s.search_tasks.return_value = "tasks"
    s.get_task_context.return_value = "ctx"
    return s


def test_task_tools_registered():
    import asyncio

    svc = _service()
    server = create_server(svc)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"index_task", "search_tasks", "get_task_context"} <= names


def test_publish_review_tool_forwards_task_key():
    import asyncio

    svc = _service()
    # publish_review-тул сериализует dict-отчёт в TextContent — даём валидный return.
    svc.publish_review.return_value = {"posted": True}
    server = create_server(svc)
    # Гоним реальную обёртку тула через FastMCP.call_tool, а не мок напрямую.
    asyncio.run(server.call_tool(
        "publish_review",
        {"repo": "o/r", "pr": 7, "summary": "s", "findings": [],
         "dry_run": False, "task_key": "ID-1"},
    ))
    svc.publish_review.assert_called_once_with("o/r", 7, "s", [], False, "ID-1")


def test_get_board_config_tool_registered():
    import asyncio

    svc = _service()
    svc.board_config.return_value = {"task_board": {"type": "yougile", "mcp": "yougile"}}
    server = create_server(svc)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "get_board_config" in names


def test_get_board_config_tool_forwards_to_service():
    import asyncio

    svc = _service()
    svc.board_config.return_value = {"task_board": {"type": "yougile", "mcp": "yougile"}}
    server = create_server(svc)
    asyncio.run(server.call_tool("get_board_config", {}))
    svc.board_config.assert_called_once_with()


def test_search_codebase_tool_registered():
    import asyncio

    svc = _service()
    svc.search_codebase.return_value = "code"
    server = create_server(svc)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "search_codebase" in names


def test_search_codebase_tool_forwards_repo():
    import asyncio

    svc = _service()
    svc.search_codebase.return_value = "code"
    server = create_server(svc)
    asyncio.run(server.call_tool(
        "search_codebase",
        {"repo": "owner/name", "query": "token verification", "top_k": 5},
    ))
    svc.search_codebase.assert_called_once_with("owner/name", "token verification", 5, None)
