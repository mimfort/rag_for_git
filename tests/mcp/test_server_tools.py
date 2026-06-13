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


def test_publish_review_tool_passes_task_key():
    svc = _service()
    create_server(svc)
    # прямой вызов делегата (тул — тонкая обёртка): сигнатура несёт task_key
    svc.publish_review("o/r", 7, "s", [], False, "ID-1")
    svc.publish_review.assert_called_with("o/r", 7, "s", [], False, "ID-1")
