"""Создание задачи Linear: issueCreate с teamId/stateId, fallback с warning."""
from __future__ import annotations

import json

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.linear import LinearBoard

SECRET = "linear-create-secret"

STATES = [
    {"id": "state-backlog", "name": "Backlog", "type": "backlog", "position": 0},
    {"id": "state-started", "name": "In Progress", "type": "started", "position": 1},
    {"id": "state-done", "name": "Done", "type": "completed", "position": 2},
]
TEAM = {
    "id": "team-uuid-eng",
    "key": "ENG",
    "name": "Команда ENG",
    "states": {"nodes": STATES},
}
DOC = "## Проблема\n\nСломан ретрив"


def _board(handler, **kwargs) -> LinearBoard:
    return LinearBoard(
        api_key=SECRET,
        api_base="https://api.linear.app",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
        **kwargs,
    )


def _handler(
    bodies: list[dict],
    *,
    teams: list[dict] | None = None,
    success: bool = True,
    issue: dict | None = None,
):
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        if "teams(" in body["query"]:
            nodes = TEAM if teams is None else teams
            return httpx.Response(
                200,
                json={"data": {"teams": {"nodes": [TEAM] if teams is None else nodes}}},
            )
        state_id = (body["variables"]["input"] or {}).get("stateId") or "state-backlog"
        state = next(item for item in STATES if item["id"] == state_id)
        return httpx.Response(
            200,
            json={
                "data": {
                    "issueCreate": {
                        "success": success,
                        "issue": issue
                        if issue is not None
                        else {
                            "id": "issue-uuid-77",
                            "identifier": "ENG-77",
                            "url": "https://linear.app/acme/issue/ENG-77",
                            "state": {"id": state["id"], "name": state["name"]},
                        },
                    }
                }
            },
        )

    return handle


def test_create_sends_team_id_markdown_and_resolved_state() -> None:
    bodies: list[dict] = []
    result = _board(_handler(bodies)).create(
        DOC,
        title="Новая задача",
        target="state-done",
        project="ENG",
    )

    mutation = bodies[-1]
    assert "issueCreate(input: $input)" in mutation["query"]
    assert mutation["variables"]["input"] == {
        "teamId": "team-uuid-eng",
        "title": "Новая задача",
        "description": DOC,
        "stateId": "state-done",
    }
    assert result == {
        "key": "ENG-77",
        "url": "https://linear.app/acme/issue/ENG-77",
        "board_id": "issue-uuid-77",
        "target_resolved": "state-done",
        "warnings": [],
    }


def test_create_resolves_target_by_state_name() -> None:
    bodies: list[dict] = []
    result = _board(_handler(bodies)).create(
        DOC, title="Новая", target="In Progress", project="ENG"
    )

    assert bodies[-1]["variables"]["input"]["stateId"] == "state-started"
    assert result["target_resolved"] == "state-started"
    assert result["warnings"] == []


def test_missing_target_falls_back_to_team_default_state_with_warning() -> None:
    bodies: list[dict] = []
    result = _board(_handler(bodies)).create(
        DOC, title="Новая", target="Missing", project="ENG"
    )

    assert "stateId" not in bodies[-1]["variables"]["input"]
    assert result["target_resolved"] == "state-backlog"
    assert result["warnings"]


def test_create_without_target_omits_state_id() -> None:
    bodies: list[dict] = []
    result = _board(_handler(bodies)).create(DOC, title="Новая", target=None, project="ENG")

    assert "stateId" not in bodies[-1]["variables"]["input"]
    assert result["warnings"] == []


def test_configured_team_key_is_used_when_project_is_absent() -> None:
    bodies: list[dict] = []
    board = _board(_handler(bodies), team_key="ENG")

    assert board.create(DOC, title="Новая", target=None, project=None)["key"] == "ENG-77"
    assert bodies[-1]["variables"]["input"]["teamId"] == "team-uuid-eng"


def test_create_without_any_team_raises_before_mutation() -> None:
    bodies: list[dict] = []
    board = _board(_handler(bodies, teams=[]))

    with pytest.raises(BoardProviderError) as exc_info:
        board.create(DOC, title="Новая", target=None, project=None)

    assert exc_info.value.category == "configuration"
    assert all("issueCreate" not in body["query"] for body in bodies)


def test_unknown_team_key_raises_configuration_error() -> None:
    bodies: list[dict] = []
    board = _board(_handler(bodies, teams=[]))

    with pytest.raises(BoardProviderError) as exc_info:
        board.create(DOC, title="Новая", target=None, project="ZZZ")

    assert exc_info.value.category == "configuration"


def test_unsuccessful_mutation_is_not_silently_swallowed() -> None:
    board = _board(_handler([], success=False))

    with pytest.raises(BoardProviderError) as exc_info:
        board.create(DOC, title="Новая", target=None, project="ENG")

    assert exc_info.value.category == "unsupported"


def test_missing_identifier_in_response_is_reported() -> None:
    board = _board(_handler([], issue={"id": "issue-uuid-77", "identifier": ""}))

    with pytest.raises(BoardProviderError) as exc_info:
        board.create(DOC, title="Новая", target=None, project="ENG")

    assert exc_info.value.category == "unsupported"
