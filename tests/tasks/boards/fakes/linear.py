"""Фейк доски Linear (GraphQL) для общего contract-набора.

Все запросы идут одним POST ``/graphql``, поэтому роутинг — по подстроке в
теле запроса (``query``), а страницы — по значению ``variables["after"]``.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from reviewer.tasks.boards.linear import LinearBoard
from tests.tasks.boards.fakes.base import (
    FakeState,
    ProviderAdapter,
    RecordingTransport,
    record,
    request_json,
)

SECRET = "linear-contract-secret"
API_BASE = "https://api.linear.app"
TEAM_ID = "team-uuid-eng"
PAGE_SIZE = 50
TOTAL_ISSUES = 51

STATES = [
    {"id": "state-backlog", "name": "Backlog", "type": "backlog", "position": 0},
    {"id": "state-started", "name": "In Progress", "type": "started", "position": 1},
    {"id": "state-done", "name": "Done", "type": "completed", "position": 2},
]


@dataclass
class State(FakeState):
    """Состояние фейка: описание и workflow state задачи ENG-2, счётчик созданий."""

    descriptions: dict[str, str] = field(default_factory=dict)
    state_ids: dict[str, str] = field(default_factory=dict)
    created: int = 0


def _state_by_id(state_id: str) -> dict:
    return next(item for item in STATES if item["id"] == state_id)


def _issue(state: State, number: int) -> dict[str, Any]:
    """Узел issue: у ENG-1 есть sub-issue и вложение ``spec.txt``."""
    key = f"ENG-{number}"
    return {
        "id": f"issue-uuid-{number}",
        "identifier": key,
        "title": f"Задача {number}",
        "description": state.descriptions.get(key, f"## Проблема\n\nОписание {key}"),
        "updatedAt": f"2026-07-23T09:{number % 60:02d}:00.000Z",
        "url": f"https://linear.app/acme/issue/{key}",
        "state": dict(_state_by_id(state.state_ids.get(key, "state-started"))),
        "team": {"id": TEAM_ID, "key": "ENG"},
        "children": {
            "nodes": [{"identifier": "ENG-9", "title": "Подзадача"}] if number == 1 else []
        },
        "attachments": {
            "nodes": [
                {
                    "title": "spec.txt",
                    "url": "https://uploads.linear.app/spec.txt",
                }
            ]
            if number == 1
            else []
        },
    }


def _team() -> dict[str, Any]:
    return {
        "id": TEAM_ID,
        "key": "ENG",
        "name": "Команда ENG",
        "states": {"nodes": STATES},
    }


def _linear_handler(
    state: State,
    *,
    error_status: int | None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        record(state, request)
        if error_status is not None:
            return httpx.Response(error_status, json={"token": SECRET})
        body = request_json(request)
        query = str(body.get("query") or "")
        variables = body.get("variables") or {}

        if "viewer {" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "viewer": {"id": "user-1", "name": "robot", "displayName": "Робот"},
                        "organization": {"id": "org-1", "name": "Acme", "urlKey": "acme"},
                    }
                },
            )
        if "teams(" in query:
            return httpx.Response(200, json={"data": {"teams": {"nodes": [_team()]}}})
        if "issues(" in query:
            start = PAGE_SIZE if variables.get("after") else 0
            numbers = range(start + 1, min(start + PAGE_SIZE, TOTAL_ISSUES) + 1)
            has_next = start + PAGE_SIZE < TOTAL_ISSUES
            return httpx.Response(
                200,
                json={
                    "data": {
                        "issues": {
                            "nodes": [_issue(state, number) for number in numbers],
                            "pageInfo": {
                                "hasNextPage": has_next,
                                "endCursor": "cursor-1" if has_next else None,
                            },
                        }
                    }
                },
            )
        if "issueCreate(" in query:
            state.created += 1
            payload = variables.get("input") or {}
            created = _state_by_id(payload.get("stateId") or "state-backlog")
            return httpx.Response(
                200,
                json={
                    "data": {
                        "issueCreate": {
                            "success": True,
                            "issue": {
                                "id": "issue-uuid-77",
                                "identifier": "ENG-77",
                                "url": "https://linear.app/acme/issue/ENG-77",
                                "state": {"id": created["id"], "name": created["name"]},
                            },
                        }
                    }
                },
            )
        if "issueUpdate(" in query:
            payload = variables.get("input") or {}
            key = f"ENG-{str(variables.get('id') or '').rsplit('-', 1)[-1]}"
            if "description" in payload:
                state.descriptions[key] = payload["description"]
            if "stateId" in payload:
                state.state_ids[key] = payload["stateId"]
            return httpx.Response(
                200,
                json={
                    "data": {
                        "issueUpdate": {
                            "success": True,
                            "issue": {"id": variables.get("id"), "identifier": key},
                        }
                    }
                },
            )
        if "issue(id:" in query:
            requested = str(variables.get("id") or "")
            number = requested.rsplit("-", 1)[-1]
            if not number.isdigit() or int(number) > TOTAL_ISSUES:
                return httpx.Response(200, json={"data": {"issue": None}})
            issue = _issue(state, int(number))
            if "ReviewerLinearIssueForFinish" in query:
                # finish запрашивает у команды её workflow states — listing их не тянет.
                issue["team"] = {**issue["team"], "states": {"nodes": STATES}}
            return httpx.Response(200, json={"data": {"issue": issue}})
        return httpx.Response(200, json={"data": {}})

    return handle


def build(
    *,
    state: State | None = None,
    forbidden: bool = False,
    error_status: int | None = None,
) -> tuple[LinearBoard, State]:
    """Собрать LinearBoard с транспортом через конструктор (без сети)."""
    state = state or State()
    status = error_status or (403 if forbidden else None)
    provider = LinearBoard(
        api_key=SECRET,
        api_base=API_BASE,
        team_key="ENG",
        key_pattern=r"ENG-\d+",
        url_template="https://linear.app/acme/issue/{code}",
        transport=RecordingTransport(_linear_handler(state, error_status=status), state),
        sleeper=lambda _: None,
    )
    return provider, state


ADAPTER = ProviderAdapter(
    board_type="linear",
    secret=SECRET,
    project="ENG",
    key="ENG-1",
    finish_key="ENG-2",
    target_id="state-done",
    target_label="Done",
    missing_target="Missing",
    factory=build,
    min_rows=PAGE_SIZE,
    page_paths=("/graphql",),
)
