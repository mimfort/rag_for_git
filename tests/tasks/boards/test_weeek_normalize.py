"""Нормализация задачи Weeek: HTML→markdown, подзадачи, вложения, meta без I/O."""
from __future__ import annotations

from typing import Any

import httpx

from reviewer.tasks.boards.weeek import WeeekBoard

TOKEN = "weeek-secret-token"
COLUMNS = {
    "success": True,
    "boardColumns": [
        {"id": 8, "name": "Backlog", "boardId": 6},
        {"id": 9, "name": "Done", "boardId": 6},
    ],
}


def _board(handler, **kwargs) -> WeeekBoard:
    params: dict[str, Any] = {
        "api_token": TOKEN,
        "project_id": "4",
        "board_id": "6",
        "key_prefix": "WEEEK",
        "key_pattern": r"WEEEK-\d+",
        "url_template": "https://app.weeek.net/ws/1/task/{code}",
        "attachment_max_bytes": 1000,
        "attachment_timeout": 1.0,
        "attachment_store_chars": 1000,
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _seconds: None,
    }
    params.update(kwargs)
    return WeeekBoard(**params)


def _task(number: int, **overrides: Any) -> dict[str, Any]:
    task = {
        "id": number,
        "parentId": None,
        "title": f"Задача {number}",
        "description": f"<p>Описание задачи {number}</p>",
        "isCompleted": False,
        "isDeleted": False,
        "projectId": 4,
        "boardId": 6,
        "boardColumnId": 8,
        "locations": [{"projectId": 4, "boardId": 6, "boardColumnId": 8}],
        "createdAt": "2026-07-20T08:00:00Z",
        "updatedAt": "2026-07-23T09:15:00Z",
        "completedAt": None,
        "subTasks": [],
        "attachments": [],
    }
    task.update(overrides)
    return task


def _handler(task: dict[str, Any], *, extra: dict[str, Any] | None = None):
    """Роутер: колонки, список задач, единичные задачи, файлы вложений."""
    tasks = {task["id"]: task, **(extra or {})}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tm/board-columns"):
            return httpx.Response(200, json=COLUMNS)
        if path.endswith("/tm/tasks"):
            return httpx.Response(200, json={"success": True, "tasks": [task], "hasMore": False})
        if "/tm/tasks/" in path:
            task_id = int(path.rsplit("/", 1)[-1])
            if task_id not in tasks:
                return httpx.Response(404, json={"success": False})
            return httpx.Response(200, json={"success": True, "task": tasks[task_id]})
        if path.endswith("/ws/attachments/att-1"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "id": "att-1",
                        "name": "spec.txt",
                        "service": "weeek",
                        "url": "https://files.weeek.net/spec.txt",
                        "size": 24,
                    },
                },
            )
        if path.endswith("/spec.txt"):
            return httpx.Response(200, text="Критерий из вложения")
        return httpx.Response(404, json={"success": False})

    return handle


def _normalize(task: dict[str, Any], *, extra: dict[str, Any] | None = None) -> dict:
    provider = _board(_handler(task, extra=extra))
    raw = next(iter(provider.iter_raw(None, None)))
    return provider.normalize(raw)


def test_description_is_converted_from_html_to_markdown() -> None:
    task = _task(
        1,
        description="<p>Первый абзац</p><ul><li>пункт</li></ul>",
    )

    result = _normalize(task)

    assert "<p>" not in result["description"]
    assert "Первый абзац" in result["description"]
    assert "- пункт" in result["description"]


def test_subtasks_become_criteria_and_subtask_links() -> None:
    task = _task(1, subTasks=[9, 10])
    extra = {
        9: _task(9, title="Подзадача A"),
        10: _task(10, title="Подзадача B"),
    }

    result = _normalize(task, extra=extra)

    assert result["criteria"] == ["Подзадача A", "Подзадача B"]
    subtasks = [link for link in result["links"] if link["type"] == "subtask"]
    assert [(link["key"], link["title"]) for link in subtasks] == [
        ("WEEEK-9", "Подзадача A"),
        ("WEEEK-10", "Подзадача B"),
    ]


def test_unresolved_subtask_keeps_the_link_and_warns() -> None:
    task = _task(1, subTasks=[404])

    result = _normalize(task)

    assert [link["key"] for link in result["links"] if link["type"] == "subtask"] == ["WEEEK-404"]
    assert result["criteria"] == []
    assert any("WEEEK-404" in warning for warning in result["warnings"])


def test_parent_and_related_keys_are_linked() -> None:
    task = _task(1, parentId=3, description="<p>см. WEEEK-42</p>")

    result = _normalize(task, extra={3: _task(3, title="Родитель")})

    kinds = {(link["type"], link["key"]) for link in result["links"]}
    assert ("parent", "WEEEK-3") in kinds
    assert ("related", "WEEEK-42") in kinds


def test_attachment_text_is_extracted_from_the_board_host() -> None:
    task = _task(
        1,
        attachments=[
            {
                "id": "att-1",
                "name": "spec.txt",
                "service": "weeek",
                "size": 24,
                "url": "https://files.weeek.net/spec.txt",
                "createdAt": "2026-07-20T08:00:00Z",
            }
        ],
    )

    result = _normalize(task)

    assert result["attachments"] == [
        {
            "name": "spec.txt",
            "mime_type": None,
            "size": 24,
            "content_text": "Критерий из вложения",
            "url": "https://files.weeek.net/spec.txt",
        }
    ]
    assert result["warnings"] == []


def test_attachment_url_is_refreshed_when_the_task_payload_has_none() -> None:
    task = _task(
        1,
        attachments=[
            {"id": "att-1", "name": "spec.txt", "service": "weeek", "size": 24, "url": ""}
        ],
    )

    result = _normalize(task)

    assert result["attachments"][0]["content_text"] == "Критерий из вложения"


def test_off_host_attachment_keeps_metadata_and_warns() -> None:
    task = _task(
        1,
        attachments=[
            {
                "id": "att-2",
                "name": "spec.txt",
                "service": "google_drive",
                "url": "https://drive.google.com/file/spec.txt",
            }
        ],
    )

    result = _normalize(task)

    assert result["attachments"] == [
        {
            "name": "spec.txt",
            "mime_type": None,
            "size": None,
            "content_text": None,
            "url": "https://drive.google.com/file/spec.txt",
        }
    ]
    assert any("spec.txt" in warning for warning in result["warnings"])


def test_unsupported_and_oversized_attachments_keep_metadata_and_warn() -> None:
    task = _task(
        1,
        attachments=[
            {
                "id": "att-3",
                "name": "design.psd",
                "service": "weeek",
                "size": 10,
                "url": "https://files.weeek.net/design.psd",
            },
            {
                "id": "att-4",
                "name": "huge.txt",
                "service": "weeek",
                "size": 10_000,
                "url": "https://files.weeek.net/huge.txt",
            },
        ],
    )

    result = _normalize(task)

    assert [item["name"] for item in result["attachments"]] == ["design.psd", "huge.txt"]
    assert all(item["content_text"] is None for item in result["attachments"])
    assert len(result["warnings"]) == 2


def test_normalized_brief_carries_key_aliases_status_and_url() -> None:
    task = _task(1, isCompleted=True)

    result = _normalize(task)

    assert result["key"] == "WEEEK-1"
    assert result["aliases"] == ["1"]
    assert result["status"] == "done"
    assert result["url"] == "https://app.weeek.net/ws/1/task/WEEEK-1"
    assert result["project"] == "WEEEK"


def test_open_task_status_is_the_board_column_title() -> None:
    result = _normalize(_task(1))

    assert result["status"] == "Backlog"


def test_normalize_meta_makes_no_http_calls_and_skips_details() -> None:
    calls: list[str] = []
    task = _task(1, subTasks=[9], attachments=[{"id": "att-1", "name": "spec.txt", "url": ""}])

    inner = _handler(task)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return inner(request)

    provider = _board(handler)
    raw = next(iter(provider.iter_raw(None, None)))
    calls.clear()
    result = provider.normalize_meta(raw)

    assert calls == []
    assert result["criteria"] == []
    assert result["attachments"] == []
    assert result["key"] == "WEEEK-1"
    assert "<p>" not in result["description"]
    assert [link["key"] for link in result["links"] if link["type"] == "subtask"] == ["WEEEK-9"]
