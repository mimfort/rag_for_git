"""Discovery целей Linear: workflow states команды, резолв по id и по имени."""
from __future__ import annotations

import json

import httpx

from reviewer.tasks.boards.linear import LinearBoard

SECRET = "linear-targets-secret"

STATES = [
    {"id": "state-done", "name": "Done", "type": "completed", "position": 2},
    {"id": "state-backlog", "name": "Backlog", "type": "backlog", "position": 0},
    {"id": "state-started", "name": "In Progress", "type": "started", "position": 1},
    {"id": "state-canceled", "name": "Canceled", "type": "canceled", "position": 3},
]


def _team(key: str = "ENG", *, states: list[dict] | None = None) -> dict:
    return {
        "id": f"team-uuid-{key.lower()}",
        "key": key,
        "name": f"Команда {key}",
        "states": {"nodes": states if states is not None else STATES},
    }


def _board(handler, **kwargs) -> LinearBoard:
    return LinearBoard(
        api_key=SECRET,
        api_base="https://api.linear.app",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
        **kwargs,
    )


def _teams_handler(teams: list[dict], captured: list[dict] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if captured is not None:
            captured.append(body)
        return httpx.Response(200, json={"data": {"teams": {"nodes": teams}}})

    return handler


def test_list_targets_filters_by_team_key_and_normalizes_states() -> None:
    bodies: list[dict] = []
    result = _board(_teams_handler([_team()], bodies)).list_targets("ENG")

    assert set(result) == {"targets", "options", "warnings"}
    assert bodies[0]["variables"] == {"filter": {"key": {"eq": "ENG"}}, "first": 50}
    assert [target["id"] for target in result["targets"]] == [
        "state-backlog",
        "state-started",
        "state-done",
        "state-canceled",
    ]
    assert result["targets"][0]["label"] == "Backlog"
    assert result["warnings"] == []


def test_every_target_can_create_and_only_terminal_states_can_close() -> None:
    result = _board(_teams_handler([_team()])).list_targets("ENG")
    purposes = {target["id"]: target["purposes"] for target in result["targets"]}

    assert all("create" in value for value in purposes.values())
    assert purposes["state-done"] == ["create", "done"]
    assert purposes["state-canceled"] == ["create", "done"]
    assert purposes["state-backlog"] == ["create"]


def test_options_expose_team_key_choices() -> None:
    result = _board(_teams_handler([_team(), _team("OPS")])).list_targets("ENG")

    assert result["options"][0]["key"] == "team_key"
    assert result["options"][0]["required_for"] == ["sync", "create"]
    assert {"id": "OPS", "label": "Команда OPS"} in result["options"][0]["choices"]


def test_unknown_team_key_yields_warning_without_targets() -> None:
    result = _board(_teams_handler([_team("OPS")])).list_targets("ENG")

    assert result["targets"] == []
    assert result["warnings"]


def test_single_team_is_used_when_project_is_absent() -> None:
    bodies: list[dict] = []
    result = _board(_teams_handler([_team()], bodies)).list_targets(None)

    assert bodies[0]["variables"]["filter"] is None
    assert [target["id"] for target in result["targets"]][0] == "state-backlog"


def test_ambiguous_workspace_without_project_warns() -> None:
    result = _board(_teams_handler([_team(), _team("OPS")])).list_targets(None)

    assert result["targets"] == []
    assert result["warnings"]


def test_configured_team_key_option_is_used_as_default_project() -> None:
    bodies: list[dict] = []
    board = _board(_teams_handler([_team()], bodies), team_key="ENG")

    assert board.list_targets(None)["targets"]
    assert bodies[0]["variables"]["filter"] == {"key": {"eq": "ENG"}}


def test_state_resolution_by_id_by_name_and_missing() -> None:
    by_id, warnings = LinearBoard._resolve_state(STATES, "state-done")
    assert (by_id["id"], warnings) == ("state-done", [])

    by_name, warnings = LinearBoard._resolve_state(STATES, "In Progress")
    assert (by_name["id"], warnings) == ("state-started", [])

    missing, warnings = LinearBoard._resolve_state(STATES, "Missing")
    assert missing is None
    assert warnings


def test_state_resolution_without_target_picks_first_completed() -> None:
    state, warnings = LinearBoard._resolve_state(STATES, None)

    assert (state["id"], warnings) == ("state-done", [])


def test_state_resolution_without_target_warns_when_no_completed_state() -> None:
    state, warnings = LinearBoard._resolve_state(
        [{"id": "state-backlog", "name": "Backlog", "type": "backlog", "position": 0}],
        None,
    )

    assert state is None
    assert warnings


def test_ambiguous_state_name_is_not_applied() -> None:
    duplicates = [
        {"id": "state-a", "name": "Done", "type": "completed", "position": 1},
        {"id": "state-b", "name": "Done", "type": "completed", "position": 2},
    ]
    state, warnings = LinearBoard._resolve_state(duplicates, "Done")

    assert state is None
    assert warnings
