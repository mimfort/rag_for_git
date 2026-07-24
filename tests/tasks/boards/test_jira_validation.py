from __future__ import annotations

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
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
    assert result["capabilities"] == {"read": True}
    assert result["warnings"] == [
        "Jira project was not checked; create and transition permissions are unknown."
    ]
    assert calls == ["/rest/api/3/myself"]


def test_direct_site_auth_401_suggests_unscoped_token_without_leaking_response() -> None:
    raw_body = "scoped token rejected: jira-secret-token"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=raw_body)

    with pytest.raises(BoardProviderError) as raised:
        board(handler).validate_connection("PRI")

    error = raised.value
    rendered = f"{error!r} {error}"
    assert error.category == "authentication"
    assert "token без scopes" in error.hint
    assert "direct Jira Cloud site URL" in error.hint
    assert "jira-secret-token" not in rendered
    assert raw_body not in rendered
