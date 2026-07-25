"""Фейк доски YouTrack для общего contract-набора."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from reviewer.tasks.boards.youtrack import YouTrackBoard
from tests.tasks.boards.fakes.base import (
    FakeState,
    ProviderAdapter,
    RecordingTransport,
    record,
    request_json,
)

SECRET = "perm:youtrack-contract-secret"
API_BASE = "https://youtrack.test/api"


@dataclass
class State(FakeState):
    """Состояние фейка YouTrack: изменяемые поля задачи PRI-2 и счётчик созданий."""

    issue_description: str = "Описание PRI-2"
    issue_state: str = "Open"
    created: int = 0


def _youtrack_issue(
    number: int,
    *,
    description: str | None = None,
    state_name: str = "Open",
) -> dict[str, Any]:
    return {
        "idReadable": f"PRI-{number}",
        "summary": f"Задача {number}",
        "description": description if description is not None else f"Описание PRI-{number}",
        "updated": 2000 + number,
        "customFields": [
            {
                "name": "State",
                "$type": "StateIssueCustomField",
                "value": {"name": state_name, "$type": "StateBundleElement"},
            }
        ],
        "links": [
            {
                "linkType": {"name": "Subtask"},
                "issues": [{"idReadable": "PRI-9"}],
            }
        ]
        if number == 1
        else [],
        "attachments": [
            {
                "name": "spec.txt",
                "mimeType": "text/plain",
                "size": 24,
                "url": "/files/spec.txt",
            }
        ]
        if number == 1
        else [],
    }


def _youtrack_handler(
    state: State,
    *,
    error_status: int | None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        record(state, request)
        path = request.url.path.removeprefix("/api")
        params = request.url.params
        if error_status is not None:
            return httpx.Response(error_status, json={"token": "must-not-leak"})
        if request.method == "GET" and path == "/users/me":
            return httpx.Response(200, json={"id": "user-1", "login": "robot", "name": "Robot"})
        if request.method == "GET" and path == "/admin/projects":
            return httpx.Response(
                200,
                json=[{"id": "project-1", "shortName": "PRI", "name": "PRI"}],
            )
        if request.method == "GET" and path == "/admin/projects/project-1/customFields":
            return httpx.Response(
                200,
                json=[
                    {
                        "$type": "StateProjectCustomField",
                        "field": {"name": "State"},
                        "bundle": {
                            "values": [
                                {"name": "Open", "$type": "StateBundleElement"},
                                {"name": "Done", "$type": "StateBundleElement"},
                            ]
                        },
                    }
                ],
            )
        if request.method == "GET" and path == "/issues" and "$skip" in params:
            skip = int(params.get("$skip", "0"))
            issues = [_youtrack_issue(n) for n in range(1, 202)]
            return httpx.Response(200, json=issues[skip : skip + 200])
        if request.method == "GET" and path == "/issues":
            return httpx.Response(200, json=[_youtrack_issue(1)])
        if request.method == "GET" and path == "/issues/PRI-1":
            return httpx.Response(200, json=_youtrack_issue(1))
        if request.method == "GET" and path == "/issues/PRI-2":
            return httpx.Response(
                200,
                json=_youtrack_issue(
                    2,
                    description=state.issue_description,
                    state_name=state.issue_state,
                ),
            )
        if request.method == "GET" and path == "/issues/PRI-77":
            return httpx.Response(200, json=_youtrack_issue(77))
        if request.method == "GET" and path == "/files/spec.txt":
            return httpx.Response(200, text="Критерий из вложения")
        if request.method == "POST" and path == "/issues" and "fields" in params:
            state.created += 1
            return httpx.Response(200, json={"idReadable": "PRI-77"})
        if request.method == "POST" and path in {"/issues/PRI-2", "/issues/PRI-77"}:
            payload = request_json(request)
            if "description" in payload:
                state.issue_description = payload["description"]
            for custom_field in payload.get("customFields", []):
                requested = (custom_field.get("value") or {}).get("name")
                if requested == "Missing":
                    return httpx.Response(400, json={})
                state.issue_state = requested or state.issue_state
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    return handle


def build(
    *,
    state: State | None = None,
    forbidden: bool = False,
    error_status: int | None = None,
) -> tuple[YouTrackBoard, State]:
    """Собрать YouTrackBoard на записывающем MockTransport (без сети)."""
    state = state or State()
    status = error_status or (403 if forbidden else None)
    provider = YouTrackBoard(
        token=SECRET,
        base_url=API_BASE,
        key_pattern=r"PRI-\d+",
        status_field="State",
    )
    provider._client.close()  # type: ignore[attr-defined]
    provider._client = httpx.Client(  # type: ignore[attr-defined]
        base_url=API_BASE,
        transport=RecordingTransport(_youtrack_handler(state, error_status=status), state),
    )
    return provider, state


ADAPTER = ProviderAdapter(
    board_type="youtrack",
    secret=SECRET,
    project="PRI",
    key="PRI-1",
    finish_key="PRI-2",
    target_id="Done",
    target_label="Done",
    missing_target="Missing",
    factory=build,
    min_rows=200,
    page_paths=("/issues",),
)
