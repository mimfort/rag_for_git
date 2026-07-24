from __future__ import annotations

import httpx

from tests.tasks.boards.jira_helpers import board


def test_validation_reports_identity_project_and_independent_capabilities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/myself"):
            return httpx.Response(200, json={"accountId": "abc", "displayName": "Reviewer Bot"})
        if "/project/" in request.url.path:
            return httpx.Response(200, json={"id": "10000", "key": "PRI"})
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

    assert board(handler).validate_connection("PRI") == {
        "status": "ok",
        "identity": {"account_id": "abc", "display_name": "Reviewer Bot"},
        "project": "PRI",
        "capabilities": {"read": True, "create": False, "transition": False},
        "warnings": [
            "missing Jira permission: CREATE_ISSUES",
            "missing Jira permission: TRANSITION_ISSUES",
        ],
    }


def test_validation_without_project_only_checks_identity() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"accountId": "abc", "displayName": "Reviewer Bot"})

    result = board(handler).validate_connection()

    assert result["project"] is None
    assert result["capabilities"] == {"read": True, "create": False, "transition": False}
    assert calls == ["/rest/api/3/myself"]
