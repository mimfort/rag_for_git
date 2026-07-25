"""Закрытие задачи Linear: идемпотентный append PR-ссылки + issueUpdate на done-state."""
from __future__ import annotations

import json

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.linear import LinearBoard

SECRET = "linear-finish-secret"
PR_URL = "https://github.test/pull/7"

STATES = [
    {"id": "state-backlog", "name": "Backlog", "type": "backlog", "position": 0},
    {"id": "state-started", "name": "In Progress", "type": "started", "position": 1},
    {"id": "state-done", "name": "Done", "type": "completed", "position": 2},
]


class Board:
    """Поведение Linear в памяти: описание и состояние одной задачи."""

    def __init__(self, *, update_fails: bool = False, missing: bool = False) -> None:
        self.description = "## Проблема\n\nСломан ретрив"
        self.state = STATES[1]
        self.bodies: list[dict] = []
        self.update_fails = update_fails
        self.missing = missing

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        self.bodies.append(body)
        if "issueUpdate(" in body["query"]:
            if self.update_fails:
                return httpx.Response(
                    200,
                    json={"errors": [{"message": "no", "extensions": {"code": "FORBIDDEN"}}]},
                )
            payload = body["variables"]["input"]
            if "description" in payload:
                self.description = payload["description"]
            if "stateId" in payload:
                self.state = next(
                    item for item in STATES if item["id"] == payload["stateId"]
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "issueUpdate": {
                            "success": True,
                            "issue": {"id": "issue-uuid-2", "identifier": "ENG-2"},
                        }
                    }
                },
            )
        if self.missing:
            return httpx.Response(200, json={"data": {"issue": None}})
        return httpx.Response(
            200,
            json={
                "data": {
                    "issue": {
                        "id": "issue-uuid-2",
                        "identifier": "ENG-2",
                        "description": self.description,
                        "state": dict(self.state),
                        "team": {
                            "id": "team-uuid-eng",
                            "key": "ENG",
                            "states": {"nodes": STATES},
                        },
                    }
                }
            },
        )

    def board(self, **kwargs) -> LinearBoard:
        return LinearBoard(
            api_key=SECRET,
            api_base="https://api.linear.app",
            transport=httpx.MockTransport(self.handler),
            sleeper=lambda _: None,
            **kwargs,
        )

    def mutations(self) -> list[dict]:
        return [body for body in self.bodies if "issueUpdate(" in body["query"]]


def test_first_finish_appends_pr_link_and_sets_done_state() -> None:
    fake = Board()
    result = fake.board().finish("ENG-2", PR_URL, note="Проверено", target="state-done")

    assert result["pr_link_added"] is True
    assert result["done_set"] is True
    assert result["already_closed"] is False
    assert result["board_id"] == "issue-uuid-2"
    assert result["warnings"] == []
    assert PR_URL in fake.description
    assert "Проверено" in fake.description
    assert fake.state["id"] == "state-done"

    updates = fake.mutations()
    assert [body["variables"]["id"] for body in updates] == ["issue-uuid-2", "issue-uuid-2"]
    assert "description" in updates[0]["variables"]["input"]
    assert updates[1]["variables"]["input"] == {"stateId": "state-done"}


def test_second_finish_is_idempotent() -> None:
    fake = Board()
    board = fake.board()
    board.finish("ENG-2", PR_URL, note="Проверено", target="state-done")
    fake.bodies.clear()
    second = board.finish("ENG-2", PR_URL, note="Проверено", target="state-done")

    assert second["pr_link_added"] is False
    assert second["done_set"] is False
    assert second["already_closed"] is True
    assert fake.mutations() == []


def test_target_resolved_by_state_name() -> None:
    fake = Board()
    result = fake.board().finish("ENG-2", PR_URL, target="Done")

    assert result["done_set"] is True
    assert fake.mutations()[-1]["variables"]["input"] == {"stateId": "state-done"}


def test_without_target_the_first_completed_state_is_used() -> None:
    fake = Board()
    result = fake.board().finish("ENG-2", PR_URL, target=None)

    assert result["done_set"] is True
    assert fake.state["id"] == "state-done"
    assert result["warnings"] == []


def test_unknown_target_keeps_state_and_warns_but_writes_pr_link() -> None:
    fake = Board()
    result = fake.board().finish("ENG-2", PR_URL, target="Missing")

    assert result["pr_link_added"] is True
    assert result["done_set"] is False
    assert result["already_closed"] is False
    assert result["warnings"]
    assert fake.state["id"] == "state-started"


def test_mark_done_false_only_updates_description() -> None:
    fake = Board()
    board = fake.board()
    first = board.finish("ENG-2", PR_URL, mark_done=False, target="state-done")
    second = board.finish("ENG-2", PR_URL, mark_done=False, target="state-done")

    assert first["done_set"] is False
    assert fake.state["id"] == "state-started"
    assert all("stateId" not in body["variables"]["input"] for body in fake.mutations())
    assert second["already_closed"] is True


def test_failed_state_update_degrades_to_warning() -> None:
    fake = Board(update_fails=True)
    result = fake.board().finish("ENG-2", PR_URL, target="state-done")

    assert result["done_set"] is False
    assert result["pr_link_added"] is False
    assert len(result["warnings"]) == 2


def test_missing_issue_is_reported_as_not_found() -> None:
    fake = Board(missing=True)

    with pytest.raises(BoardProviderError) as exc_info:
        fake.board().finish("ENG-404", PR_URL, target="state-done")

    assert exc_info.value.category == "not_found"


def test_secret_never_leaks_from_finish_errors() -> None:
    fake = Board(update_fails=True)
    result = fake.board().finish("ENG-2", PR_URL, target="state-done")

    assert SECRET not in repr(result)
