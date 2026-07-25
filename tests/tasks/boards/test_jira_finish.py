from __future__ import annotations

import json

import httpx

from reviewer.tasks.boards.adf import adf_contains_link
from tests.tasks.boards.jira_helpers import board, fixture, issue


def test_finish_adds_link_note_and_exact_transition_then_is_idempotent() -> None:
    current = issue()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/transitions"):
            return httpx.Response(200, json=fixture("transitions.json"))
        if request.method == "GET":
            return httpx.Response(200, json=current)
        payload = json.loads(request.content) if request.content else {}
        if request.method == "PUT":
            current["fields"]["description"] = payload["fields"]["description"]
        if request.method == "POST":
            current["fields"]["status"] = {"id": "2", "name": "Done"}
        return httpx.Response(204)

    provider = board(handler)
    first = provider.finish(
        "PRI-1", "https://github.test/pull/7", note="Проверено", target="2"
    )
    second = provider.finish(
        "PRI-1", "https://github.test/pull/7", note="Проверено", target="2"
    )

    assert first["pr_link_added"] is True and first["done_set"] is True
    assert adf_contains_link(current["fields"]["description"], "https://github.test/pull/7")
    assert second["pr_link_added"] is False and second["done_set"] is False
    assert second["already_closed"] is True


def test_finish_reports_partial_success_without_rollback() -> None:
    puts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal puts
        if request.method == "GET" and request.url.path.endswith("/transitions"):
            return httpx.Response(200, json=fixture("transitions.json"))
        if request.method == "GET":
            return httpx.Response(200, json=issue())
        if request.method == "PUT":
            puts += 1
            return httpx.Response(204)
        return httpx.Response(403, json={"token": "must-not-leak"})

    result = board(handler).finish("PRI-1", "https://github.test/pull/7", target="2")

    assert result["pr_link_added"] is True
    assert result["done_set"] is False
    assert result["already_closed"] is False
    assert result["warnings"]
    assert puts == 1


def test_finish_without_target_never_guesses_localized_done_status() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json=issue())

    result = board(handler).finish("PRI-1", "", target=None)

    assert result["done_set"] is False
    assert result["warnings"]
    assert calls == ["GET"]
