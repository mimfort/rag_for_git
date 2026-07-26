"""Фейк доски GitHub Issues для общего contract-набора."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from reviewer.tasks.boards.github import GitHubIssuesBoard
from tests.tasks.boards.fakes.base import (
    FakeState,
    ProviderAdapter,
    RecordingTransport,
    record,
    request_json,
)

SECRET = "github-contract-secret"
REPO = "acme/widgets"
PREFIX = "PRI"
FIRST_BODY = (
    "## Проблема\n\nПадает синк, см. PRI-42.\n\n"
    "- [x] #9 Подзадача\n\n"
    "[spec.txt](https://github.com/user-attachments/files/1/spec.txt)\n"
)


@dataclass
class State(FakeState):
    """Состояние фейка GitHub: тело/статус/метки issue и счётчик созданий."""

    issues: dict[int, dict[str, Any]] = field(default_factory=dict)
    created: int = 0


def _issue(number: int) -> dict[str, Any]:
    """Issue доски в форме REST-ответа; первая задача несёт чеклист и вложение."""
    return {
        "number": number,
        "node_id": f"I_kw{number}",
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "title": f"Задача {number}",
        "body": FIRST_BODY if number == 1 else f"Описание задачи {number}",
        "state": "open",
        "state_reason": None,
        "updated_at": f"2026-07-23T09:{number % 60:02d}:00Z",
        "labels": [{"name": "bug"}],
        "milestone": None,
        "sub_issues_summary": {"total": 0, "completed": 0, "percent_completed": 0},
    }


def _stateful_issue(state: State, number: int) -> dict[str, Any]:
    """Issue, живущая в памяти фейка: PATCH виден следующему GET (идемпотентность)."""
    return state.issues.setdefault(number, _issue(number))


def _github_handler(
    state: State,
    *,
    error_status: int | None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        record(state, request)
        if error_status is not None:
            return httpx.Response(error_status, json={"token": SECRET})
        method, path = request.method, request.url.path
        page = int(request.url.params.get("page", "1"))
        if method == "GET" and path == "/user":
            return httpx.Response(200, json={"login": "robot", "id": 1, "name": "Robot"})
        if method == "GET" and path == f"/repos/{REPO}":
            return httpx.Response(
                200,
                json={
                    "full_name": REPO,
                    "has_issues": True,
                    "permissions": {"push": True, "pull": True},
                },
            )
        if method == "GET" and path == f"/repos/{REPO}/labels":
            return httpx.Response(200, json=[{"name": "done"}] if page == 1 else [])
        if method == "GET" and path == f"/repos/{REPO}/milestones":
            return httpx.Response(
                200,
                json=[{"number": 3, "title": "v1.0"}] if page == 1 else [],
            )
        if method == "GET" and path == f"/repos/{REPO}/issues":
            issues = (
                [_issue(number) for number in range(1, 101)] if page == 1 else [_issue(101)]
            )
            return httpx.Response(200, json=issues)
        if method == "POST" and path == f"/repos/{REPO}/issues":
            state.created += 1
            payload = request_json(request)
            created = _stateful_issue(state, 77)
            created["title"] = payload.get("title") or created["title"]
            created["body"] = payload.get("body") or ""
            created["labels"] = [{"name": name} for name in payload.get("labels") or []]
            return httpx.Response(201, json=created)
        if method in {"GET", "PATCH"} and path.startswith(f"/repos/{REPO}/issues/"):
            tail = path.removeprefix(f"/repos/{REPO}/issues/")
            if not tail.isdigit():
                return httpx.Response(404, json={})
            issue = _issue(int(tail)) if int(tail) == 1 else _stateful_issue(state, int(tail))
            if method == "GET":
                return httpx.Response(200, json=issue)
            payload = request_json(request)
            if "labels" in payload:
                issue["labels"] = [{"name": name} for name in payload["labels"]]
            if "milestone" in payload:
                issue["milestone"] = {"number": payload["milestone"], "title": "v1.0"}
            for name in ("body", "state", "state_reason"):
                if name in payload:
                    issue[name] = payload[name]
            return httpx.Response(200, json=issue)
        return httpx.Response(404, json={})

    return handle


def build(
    *,
    state: State | None = None,
    forbidden: bool = False,
    error_status: int | None = None,
) -> tuple[GitHubIssuesBoard, State]:
    """Собрать GitHubIssuesBoard с транспортом через конструктор (без сети)."""
    state = state or State()
    status = error_status or (403 if forbidden else None)
    provider = GitHubIssuesBoard(
        token=SECRET,
        repo=REPO,
        key_prefix=PREFIX,
        key_pattern=r"PRI-\d+",
        transport=RecordingTransport(_github_handler(state, error_status=status), state),
        sleeper=lambda _seconds: None,
    )
    return provider, state


ADAPTER = ProviderAdapter(
    board_type="github",
    secret=SECRET,
    project=REPO,
    key=f"{PREFIX}-1",
    finish_key=f"{PREFIX}-2",
    target_id="label:done",
    target_label="done",
    missing_target="Missing",
    factory=build,
    min_rows=100,
    page_paths=("/issues",),
)
