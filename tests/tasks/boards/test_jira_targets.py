from __future__ import annotations

import httpx

from tests.tasks.boards.jira_helpers import board, fixture


def test_discovery_deduplicates_status_ids_and_lists_issue_type_choices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/mypermissions"):
            return httpx.Response(
                200,
                json={
                    "permissions": {
                        name: {"havePermission": True}
                        for name in (
                            "BROWSE_PROJECTS",
                            "CREATE_ISSUES",
                            "TRANSITION_ISSUES",
                        )
                    }
                },
            )
        return httpx.Response(200, json=fixture("project-statuses.json"))

    result = board(handler).list_targets("PRI")

    assert result["targets"] == [
        {"id": "1", "label": "Open", "purposes": ["create", "done"]},
        {"id": "2", "label": "Done", "purposes": ["create", "done"]},
        {"id": "3", "label": "Done", "purposes": ["create", "done"]},
        {"id": "4", "label": "Subtask Done", "purposes": ["create", "done"]},
    ]
    assert result["options"] == [
        {
            "key": "issue_type",
            "label": "Issue type",
            "required_for": ["create"],
            "choices": [
                {"id": "10001", "label": "Task"},
                {"id": "10002", "label": "Bug"},
            ],
        }
    ]
    assert result["warnings"] == []


def test_discovery_warns_when_account_is_read_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/mypermissions"):
            return httpx.Response(
                200,
                json={
                    "permissions": {
                        "BROWSE_PROJECTS": {"havePermission": True},
                        "CREATE_ISSUES": {"havePermission": False},
                        "TRANSITION_ISSUES": {"havePermission": False},
                    }
                },
            )
        return httpx.Response(200, json=fixture("project-statuses.json"))

    result = board(handler).list_targets("PRI")

    assert result["warnings"] == [
        "missing Jira permission: CREATE_ISSUES",
        "missing Jira permission: TRANSITION_ISSUES",
    ]
    assert result["targets"]
    assert result["options"][0]["choices"]


def test_discovery_warns_when_transition_permission_is_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/mypermissions"):
            return httpx.Response(
                200,
                json={
                    "permissions": {
                        "BROWSE_PROJECTS": {"havePermission": True},
                        "CREATE_ISSUES": {"havePermission": True},
                        "TRANSITION_ISSUES": {"havePermission": False},
                    }
                },
            )
        return httpx.Response(200, json=fixture("project-statuses.json"))

    result = board(handler).list_targets("PRI")

    assert result["warnings"] == [
        "missing Jira permission: TRANSITION_ISSUES",
    ]


def test_discovery_does_not_request_statuses_without_browse_permission() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "permissions": {
                    "BROWSE_PROJECTS": {"havePermission": False},
                    "CREATE_ISSUES": {"havePermission": False},
                    "TRANSITION_ISSUES": {"havePermission": False},
                }
            },
        )

    result = board(handler).list_targets("PRI")

    assert paths == ["/rest/api/3/mypermissions"]
    assert result["targets"] == []
    assert result["options"][0]["choices"] == []
    assert result["warnings"] == [
        "missing Jira permission: BROWSE_PROJECTS",
        "missing Jira permission: CREATE_ISSUES",
        "missing Jira permission: TRANSITION_ISSUES",
    ]
