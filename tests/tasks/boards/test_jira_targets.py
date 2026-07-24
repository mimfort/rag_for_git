from __future__ import annotations

import httpx

from tests.tasks.boards.jira_helpers import board, fixture


def test_discovery_deduplicates_status_ids_and_lists_issue_type_choices() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
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
