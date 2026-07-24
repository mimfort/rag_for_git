from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import json

import httpx

from reviewer.tasks.boards.jira import JiraCloudBoard

FIXTURES = Path(__file__).parents[2] / "fixtures" / "jira"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def board(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    issue_type: str | None = "10001",
    sleeper=lambda _: None,
) -> JiraCloudBoard:
    return JiraCloudBoard(
        base_url="https://acme.atlassian.net",
        email="bot@example.test",
        api_token="jira-secret-token",
        key_pattern=r"[A-Z]+-\d+",
        issue_type=issue_type,
        attachment_max_bytes=100,
        attachment_timeout=1.0,
        attachment_store_chars=10,
        transport=httpx.MockTransport(handler),
        sleeper=sleeper,
    )


def issue(*, description=None, status_id: str = "1", status_name: str = "Open") -> dict:
    result = fixture("issue.json")
    result["fields"]["description"] = (
        description
        if description is not None
        else {"type": "doc", "version": 1, "content": []}
    )
    result["fields"]["status"] = {"id": status_id, "name": status_name}
    return result
