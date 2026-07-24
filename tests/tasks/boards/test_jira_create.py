from __future__ import annotations

import json

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from tests.tasks.boards.jira_helpers import board, fixture


@pytest.mark.parametrize(("project", "issue_type"), [(None, "10001"), ("PRI", None)])
def test_create_requires_project_and_explicit_issue_type(project, issue_type) -> None:
    with pytest.raises(BoardProviderError) as exc_info:
        board(lambda _: httpx.Response(500), issue_type=issue_type).create(
            "# Новая", title="Новая", target=None, project=project
        )
    assert exc_info.value.category == "configuration"


def test_create_sends_adf_and_resolves_exact_transition_id() -> None:
    calls: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else {}
        calls.append((request.method, request.url.path, payload))
        if request.method == "POST" and request.url.path.endswith("/issue"):
            return httpx.Response(201, json={"id": "10077", "key": "PRI-77"})
        if request.method == "GET":
            return httpx.Response(200, json=fixture("transitions.json"))
        return httpx.Response(204)

    result = board(handler).create(
        "## Требование\n\nТекст", title="Новая", target="2", project="PRI"
    )

    create_payload = calls[0][2]["fields"]
    assert create_payload["project"] == {"key": "PRI"}
    assert create_payload["issuetype"] == {"id": "10001"}
    assert create_payload["description"]["type"] == "doc"
    assert calls[-1][2] == {"transition": {"id": "21"}}
    assert result["key"] == "PRI-77"
    assert result["target_resolved"] == "2"
    assert result["warnings"] == []


@pytest.mark.parametrize("target", ["Missing", "Done"])
def test_create_keeps_issue_when_transition_is_unavailable_or_ambiguous(target: str) -> None:
    writes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal writes
        if request.method == "POST":
            writes += 1
            return httpx.Response(201, json={"key": "PRI-77"})
        return httpx.Response(200, json=fixture("transitions.json"))

    result = board(handler).create("# Новая", title="Новая", target=target, project="PRI")

    assert result["key"] == "PRI-77"
    assert result["target_resolved"] is None
    assert result["warnings"]
    assert writes == 1
