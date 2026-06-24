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
        {"repo": "o/r", "pr": 7, "summary": "s", "task_key": "ID-1"},
    ))
    svc.publish_review.assert_called_once_with("o/r", 7, "s", False, "ID-1")


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
    svc.search_codebase.assert_called_once_with("owner/name", "token verification", 5, None, False)


def test_purge_orphaned_tasks_tool_registered():
    import asyncio

    svc = _service()
    svc.purge_orphaned_tasks.return_value = {
        "deleted_store": 0, "deleted_graph": 0, "protected_prs": 0, "warnings": []
    }
    server = create_server(svc)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "purge_orphaned_tasks" in names


def test_purge_orphaned_tasks_tool_forwards_to_service():
    import asyncio

    svc = _service()
    svc.purge_orphaned_tasks.return_value = {
        "deleted_store": 2, "deleted_graph": 2, "protected_prs": 1, "warnings": []
    }
    server = create_server(svc)
    asyncio.run(server.call_tool(
        "purge_orphaned_tasks",
        {"active_keys": ["ID-1", "ID-2"], "keep_with_prs": True},
    ))
    svc.purge_orphaned_tasks.assert_called_once_with(["ID-1", "ID-2"], True)


def test_graph_base_tools_registered():
    import asyncio

    svc = _service()
    svc.related_symbols.return_value = "x"
    svc.callers.return_value = "x"
    svc.definition.return_value = "x"
    server = create_server(svc)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"related_symbols", "callers", "definition"} <= names


def test_related_symbols_tool_forwards():
    import asyncio

    svc = _service()
    svc.related_symbols.return_value = "neighbors"
    server = create_server(svc)
    asyncio.run(server.call_tool(
        "related_symbols", {"repo": "owner/name", "node_id": "a.py#foo"}))
    svc.related_symbols.assert_called_once_with("owner/name", "a.py#foo", None)


def test_callers_tool_forwards():
    import asyncio

    svc = _service()
    svc.callers.return_value = "callers"
    server = create_server(svc)
    asyncio.run(server.call_tool(
        "callers", {"repo": "owner/name", "node_id": "a.py#foo", "branch": "main"}))
    svc.callers.assert_called_once_with("owner/name", "a.py#foo", "main")


def test_definition_tool_forwards():
    import asyncio

    svc = _service()
    svc.definition.return_value = "def"
    server = create_server(svc)
    asyncio.run(server.call_tool(
        "definition", {"repo": "owner/name", "symbol": "foo"}))
    svc.definition.assert_called_once_with("owner/name", "foo", None)


def test_get_pr_diff_tool_registered():
    import asyncio

    svc = _service()
    svc.get_pr_diff.return_value = "diff"
    server = create_server(svc)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "get_pr_diff" in names


def test_get_pr_diff_tool_forwards():
    import asyncio

    svc = _service()
    svc.get_pr_diff.return_value = "diff"
    server = create_server(svc)
    asyncio.run(server.call_tool("get_pr_diff", {"repo": "o/r", "number": 7}))
    svc.get_pr_diff.assert_called_once_with("o/r", 7)


def test_get_task_tool_registered():
    import asyncio
    svc = _service()
    svc.get_task.return_value = {"key": "ID-1"}
    server = create_server(svc)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "get_task" in names


def test_get_task_tool_forwards_key():
    import asyncio
    svc = _service()
    svc.get_task.return_value = {"key": "ID-1", "title": "T"}
    server = create_server(svc)
    asyncio.run(server.call_tool("get_task", {"key": "PRI-1"}))
    svc.get_task.assert_called_once_with("PRI-1", project=None)
