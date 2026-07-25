"""Нормализация задачи Linear: markdown как есть, sub-issues, вложения, meta без I/O."""
from __future__ import annotations

import httpx

from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.boards.linear import LinearBoard

SECRET = "linear-normalize-secret"


def _board(handler=None, **kwargs) -> LinearBoard:
    handler = handler or (lambda _request: httpx.Response(200, json={"data": {}}))
    return LinearBoard(
        api_key=SECRET,
        api_base="https://api.linear.app",
        key_pattern=r"ENG-\d+",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
        **kwargs,
    )


def _raw(**overrides) -> RawTask:
    data = {
        "key": "ENG-1",
        "project_code": "ENG-1",
        "title": "Задача 1",
        "description": "## Проблема\n\nСломан ретрив, см. ENG-42 и ENG-1.",
        "status": "In Progress",
        "subtask_ids": ["ENG-9"],
        "timestamp": 1784797260000,
        "links": [],
        "attachments": [
            {
                "name": "spec.txt",
                "url": "https://uploads.linear.app/spec.txt",
                "mime": None,
                "size": None,
            }
        ],
        "board_id": "issue-uuid-1",
        "provider_data": {
            "subtasks": [{"key": "ENG-9", "title": "Подзадача"}],
            "state": {"id": "state-started", "name": "In Progress", "type": "started"},
            "team": {"id": "team-uuid-1", "key": "ENG"},
            "url": "https://linear.app/acme/issue/ENG-1",
        },
    }
    data.update(overrides)
    return RawTask(**data)


def test_description_stays_native_markdown() -> None:
    brief = _board().normalize(_raw())

    assert brief["description"].startswith("## Проблема")
    assert "<p>" not in brief["description"]


def test_sub_issues_become_criteria_and_subtask_links() -> None:
    brief = _board().normalize(_raw())

    assert brief["criteria"] == ["Подзадача"]
    assert {"type": "subtask", "key": "ENG-9", "title": "Подзадача"} in brief["links"]


def test_related_keys_from_description_are_linked_once() -> None:
    brief = _board().normalize(_raw())

    related = [link for link in brief["links"] if link["type"] == "related"]
    assert [link["key"] for link in related] == ["ENG-42"]


def test_attachments_are_metadata_only_with_explicit_warning() -> None:
    brief = _board().normalize(_raw())

    assert brief["attachments"][0]["name"] == "spec.txt"
    assert brief["attachments"][0]["content_text"] is None
    assert brief["attachments"][0]["url"] == "https://uploads.linear.app/spec.txt"
    assert brief["warnings"]


def test_identity_fields_keep_uuid_alias_project_and_issue_url() -> None:
    brief = _board().normalize(_raw())

    assert brief["key"] == "ENG-1"
    assert brief["aliases"] == ["issue-uuid-1"]
    assert brief["project"] == "ENG"
    assert brief["status"] == "In Progress"
    assert brief["url"] == "https://linear.app/acme/issue/ENG-1"


def test_url_falls_back_to_configured_template() -> None:
    board = _board(url_template="https://linear.app/acme/issue/{code}")
    raw = _raw(provider_data={"subtasks": [], "state": {}, "team": {}, "url": ""})

    assert board.normalize(raw)["url"] == "https://linear.app/acme/issue/ENG-1"


def test_normalize_meta_makes_no_requests_and_skips_details() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": {}})

    brief = _board(handler).normalize_meta(_raw())

    assert calls == []
    assert brief["criteria"] == []
    assert brief["attachments"] == []
    assert brief["warnings"] == []
    assert brief["key"] == "ENG-1"
    assert any(link["type"] == "subtask" for link in brief["links"])


def test_brief_shape_is_stable() -> None:
    assert set(_board().normalize(_raw())) == {
        "key",
        "aliases",
        "title",
        "description",
        "criteria",
        "status",
        "url",
        "links",
        "project",
        "attachments",
        "warnings",
    }
