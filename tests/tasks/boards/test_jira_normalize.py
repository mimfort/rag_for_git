from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from reviewer.tasks.boards.jira import JiraCloudBoard

FIXTURES = Path(__file__).parents[2] / "fixtures" / "jira"


def _issue() -> dict:
    return json.loads((FIXTURES / "issue.json").read_text())


def _board(handler) -> JiraCloudBoard:
    return JiraCloudBoard(
        base_url="https://acme.atlassian.net",
        email="bot@example.test",
        api_token="jira-secret-token",
        key_pattern=r"[A-Z]+-\d+",
        issue_type="10001",
        attachment_max_bytes=100,
        attachment_timeout=1.0,
        attachment_store_chars=5,
        transport=httpx.MockTransport(handler),
    )


def test_normalize_maps_adf_status_links_subtasks_metadata_and_attachment() -> None:
    issue = _issue()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/attachment/content/20001"):
            return httpx.Response(200, content=b"hello world!")
        return httpx.Response(200, json=issue)

    board = _board(handler)
    brief = board.normalize(board.fetch_one("PRI-1"))

    assert brief["description"] == "## Требования\n\nСохранить **ADF**"
    assert brief["status"] == "В работе"
    assert brief["url"] == "https://acme.atlassian.net/browse/PRI-1"
    assert brief["criteria"] == ["Подзадача"]
    assert {"type": "subtask", "key": "PRI-2", "title": "Подзадача"} in brief["links"]
    assert {"type": "blocks", "key": "PRI-3", "title": "Зависимая задача"} in brief["links"]
    assert {"type": "is duplicated by", "key": "PRI-4", "title": "Оригинал"} in brief["links"]
    assert brief["provider_data"]["issue_type"] == {"id": "10001", "name": "Задача"}
    assert brief["provider_data"]["project"]["key"] == "PRI"
    assert brief["attachments"][0]["content_text"] == "hello"
    assert brief["warnings"] == []


def test_unknown_adf_and_each_attachment_skip_are_warnings() -> None:
    issue = _issue()
    issue["fields"]["description"]["content"].append(
        {"type": "panel", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "важно"}]}]}
    )
    issue["fields"]["attachment"] = [
        {"filename": "off.md", "mimeType": "text/markdown", "size": 1, "content": "https://evil.test/off.md"},
        {"filename": "denied.md", "mimeType": "text/markdown", "size": 1, "content": "https://acme.atlassian.net/denied"},
        {"filename": "big.md", "mimeType": "text/markdown", "size": 101, "content": "https://acme.atlassian.net/big"},
        {"filename": "image.png", "mimeType": "image/png", "size": 1, "content": "https://acme.atlassian.net/image"},
    ]
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path.endswith("/PRI-1"):
            return httpx.Response(200, json=issue)
        return httpx.Response(403, json={"Authorization": "must-not-leak"})

    board = _board(handler)
    brief = board.normalize(board.fetch_one("PRI-1"))

    assert "важно" in brief["description"]
    assert len(brief["warnings"]) == 5
    assert any("panel" in warning for warning in brief["warnings"])
    assert any("off.md" in warning for warning in brief["warnings"])
    assert any("denied.md" in warning for warning in brief["warnings"])
    assert any("big.md" in warning for warning in brief["warnings"])
    assert any("image.png" in warning for warning in brief["warnings"])
    assert "/off.md" not in requested and "/big" not in requested and "/image" not in requested


def test_normalize_meta_does_not_download_attachments() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=_issue())

    board = _board(handler)
    raw = board.fetch_one("PRI-1")
    calls.clear()

    brief = board.normalize_meta(raw)

    assert calls == []
    assert brief["attachments"] == []
    assert brief["criteria"] == []


@pytest.mark.parametrize(
    "content_url",
    [
        "http://acme.atlassian.net/rest/api/3/attachment/content/20001",
        "https://files.acme.atlassian.net/rest/api/3/attachment/content/20001",
        "https://acme.atlassian.net:444/rest/api/3/attachment/content/20001",
        "https://user@acme.atlassian.net/rest/api/3/attachment/content/20001",
        "https://[broken/rest/api/3/attachment/content/20001",
    ],
    ids=["plaintext", "subdomain", "alternate-port", "userinfo", "malformed"],
)
def test_attachment_rejects_non_exact_origin_before_basic_auth_request(
    content_url: str,
) -> None:
    issue = _issue()
    issue["fields"]["attachment"][0]["content"] = content_url
    requests: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((str(request.url), request.headers.get("Authorization")))
        return httpx.Response(200, content=b"must not be requested")

    brief = _board(handler).normalize(JiraCloudBoard._raw_from_issue(issue))

    assert requests == []
    assert brief["attachments"] == []
    assert any(
        "spec.md" in warning and "origin" in warning
        for warning in brief["warnings"]
    )


def test_attachment_allows_explicit_default_port_of_configured_https_origin() -> None:
    issue = _issue()
    issue["fields"]["attachment"][0]["content"] = (
        "https://acme.atlassian.net:443/rest/api/3/attachment/content/20001"
    )
    authorization: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization.append(request.headers.get("Authorization"))
        return httpx.Response(200, content=b"hello world!")

    brief = _board(handler).normalize(JiraCloudBoard._raw_from_issue(issue))

    assert brief["attachments"][0]["content_text"] == "hello"
    assert len(authorization) == 1
    assert authorization[0] is not None and authorization[0].startswith("Basic ")
