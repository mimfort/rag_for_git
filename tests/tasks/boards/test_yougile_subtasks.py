from __future__ import annotations

import httpx
import pytest

from reviewer.tasks.boards.base import NativeSubtaskIdentity
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.registry import default_board_registry
from reviewer.tasks.boards.yougile import YougileBoard
from tests.tasks.boards.fakes.yougile import State, build

MARKER = f"reviewer-subtask:{'a' * 64}"


def _child(
    uuid: str,
    common_key: str,
    project_key: str,
    marker: str,
    *,
    title: str,
) -> dict:
    return {
        "id": uuid,
        "idTaskCommon": common_key,
        "idTaskProject": project_key,
        "title": title,
        "description": f"<p>Описание</p><p><small>{marker}</small></p>",
        "columnId": "open-id",
        "subtasks": [],
        "timestamp": 3000,
        "completed": False,
    }


def test_listing_preserves_source_board_and_column_metadata():
    provider, _ = build()

    raw = next(iter(provider.iter_raw("PRI", 1)))

    assert raw.provider_data == {
        "source_board_id": "board-1",
        "source_column_id": "open-id",
    }
    assert provider.normalize_meta(raw)["provider_data"] == raw.provider_data


def test_create_native_subtask_uses_fixed_column_and_returns_canonical_identity():
    provider, state = build()

    identity = provider.create_native_subtask(
        "## Проблема\n\nТекст",
        title="Дочерняя",
        source_column_id="open-id",
        marker=MARKER,
    )

    assert identity == NativeSubtaskIdentity(
        board_id="uuid-created",
        key="ID-77",
        title="Дочерняя",
        aliases=("PRI-77",),
        url="https://yougile.test/#task/PRI-77",
    )
    assert state.created_children["uuid-created"] == {
        "id": "uuid-created",
        "idTaskCommon": "ID-77",
        "idTaskProject": "PRI-77",
        "title": "Дочерняя",
        "description": (
            "<h2>Проблема</h2><p>Текст</p>"
            f"<p><small>{MARKER}</small></p>"
        ),
        "columnId": "open-id",
        "subtasks": [],
        "timestamp": 2001,
        "completed": False,
    }


def test_create_native_subtask_point_read_failure_returns_uuid_warning():
    provider, state = build(state=State(fail_created_reads=True))

    identity = provider.create_native_subtask(
        "Текст",
        title="Дочерняя",
        source_column_id="open-id",
        marker=MARKER,
    )

    assert identity.board_id == "uuid-created"
    assert identity.key == "uuid-created"
    assert identity.title == "Дочерняя"
    assert identity.aliases == ()
    assert identity.url is None
    assert identity.warnings
    assert state.created == 1


def test_create_native_subtask_requires_created_id():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    provider = YougileBoard(
        api_key="secret",
        api_base="https://yougile.test/api-v2",
        key_pattern=r"PRI-\d+",
        url_template="https://yougile.test/#task/{code}",
    )
    provider._client.close()  # type: ignore[attr-defined]
    provider._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="https://yougile.test/api-v2",
        transport=httpx.MockTransport(handle),
    )

    with pytest.raises(BoardProviderError):
        provider.create_native_subtask(
            "Текст",
            title="Дочерняя",
            source_column_id="open-id",
            marker=MARKER,
        )


def test_reconcile_scans_every_board_column_and_page_and_returns_duplicates():
    second_marker = f"reviewer-subtask:{'b' * 64}"
    state = State(tasks_by_column={
        "open-id": [
            _child("child-open", "ID-2001", "PRI-2001", MARKER, title="Open child"),
        ],
        "done-id": [
            _child("child-done", "ID-2002", "PRI-2002", MARKER, title="Done child"),
            _child(
                "child-other",
                "ID-2003",
                "PRI-2003",
                second_marker,
                title="Other child",
            ),
        ],
    })
    provider, state = build(state=state)

    found = provider.reconcile_native_subtasks("board-1", frozenset({MARKER}))

    assert [(item.marker, item.identity.key) for item in found] == [
        (MARKER, "ID-2001"),
        (MARKER, "ID-2002"),
    ]
    assert found[0].identity.aliases == ("PRI-2001",)
    assert found[0].identity.url == "https://yougile.test/#task/PRI-2001"
    task_calls = [call for call in state.calls if call[0] == "GET" and call[1].endswith("/tasks")]
    assert any(call[2].get("columnId") == "open-id" and call[2].get("offset") == "1000"
               for call in task_calls)
    assert any(call[2].get("columnId") == "done-id" for call in task_calls)


def test_reconcile_with_no_markers_does_not_scan_tasks():
    provider, state = build()

    assert provider.reconcile_native_subtasks("board-1", frozenset()) == []
    assert not any(call[1].endswith("/tasks") for call in state.calls)


def test_replace_native_subtasks_sends_exact_caller_union():
    state = State(parent_subtasks={"parent-1": ["existing", "stale"]})
    provider, state = build(state=state)

    provider.replace_native_subtasks("parent-1", ["existing", "new"])

    assert state.parent_subtasks["parent-1"] == ["existing", "new"]


def test_committed_create_timeout_is_not_retried_and_reconcile_finds_child():
    provider, state = build(state=State(commit_then_timeout=True))

    with pytest.raises(BoardProviderError, match="transport"):
        provider.create_native_subtask(
            "Текст",
            title="Дочерняя",
            source_column_id="open-id",
            marker=MARKER,
        )

    post_calls = [call for call in state.calls if call[:2] == ("POST", "/api-v2/tasks")]
    assert len(post_calls) == 1
    assert state.created == 1
    found = provider.reconcile_native_subtasks("board-1", frozenset({MARKER}))
    assert [item.identity.board_id for item in found] == ["uuid-created"]


def test_default_registry_exposes_yougile_as_only_native_subtask_provider():
    registry = default_board_registry()

    capable = {
        board_type
        for board_type in registry.registered_types()
        if "native_subtasks" in registry.get(board_type).capabilities
    }
    assert capable == {"yougile"}
    assert registry.get("yougile").capabilities == frozenset({"native_subtasks"})
