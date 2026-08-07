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


def _create_board(enrichment: object, *, calls: list[tuple[str, str]] | None = None) -> YougileBoard:
    def handle(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/tasks"):
            return httpx.Response(200, json={"id": "uuid-created"})
        if request.method == "GET" and request.url.path.endswith("/tasks/uuid-created"):
            return httpx.Response(200, json=enrichment)
        return httpx.Response(404, json={})

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
    return provider


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


@pytest.mark.parametrize(
    "enrichment",
    [
        {
            "idTaskCommon": "ID-999",
            "idTaskProject": "PRI-999",
            "title": "Чужая задача",
        },
        {
            "id": "different-uuid",
            "idTaskCommon": "ID-999",
            "idTaskProject": "PRI-999",
            "title": "Чужая задача",
        },
        [{"id": "uuid-created", "idTaskCommon": "ID-999"}],
    ],
)
def test_create_native_subtask_discards_unconfirmed_enrichment_identity(enrichment):
    calls: list[tuple[str, str]] = []
    provider = _create_board(enrichment, calls=calls)

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
    assert len(identity.warnings) == 2
    assert any("enrichment" in warning for warning in identity.warnings)
    assert any("каноничес" in warning for warning in identity.warnings)
    assert sum(method == "POST" for method, _path in calls) == 1


@pytest.mark.parametrize(
    ("enrichment", "expected_aliases", "expected_url"),
    [
        ({"id": "uuid-created", "title": "Дочерняя"}, (), None),
        (
            {
                "id": "uuid-created",
                "idTaskCommon": None,
                "idTaskProject": "PRI-77",
                "title": "Дочерняя",
            },
            ("PRI-77",),
            "https://yougile.test/#task/PRI-77",
        ),
        (
            {
                "id": {"unsupported": "uuid"},
                "idTaskCommon": {"unsupported": "ID-77"},
                "idTaskProject": ["PRI-77"],
                "title": ["Дочерняя"],
            },
            (),
            None,
        ),
    ],
)
def test_create_native_subtask_missing_common_key_uses_uuid_with_warning(
    enrichment,
    expected_aliases,
    expected_url,
):
    provider = _create_board(enrichment)

    identity = provider.create_native_subtask(
        "Текст",
        title="Дочерняя",
        source_column_id="open-id",
        marker=MARKER,
    )

    assert identity.board_id == "uuid-created"
    assert identity.key == "uuid-created"
    assert identity.title == "Дочерняя"
    assert identity.aliases == expected_aliases
    assert identity.url == expected_url
    assert any("каноничес" in warning for warning in identity.warnings)


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


def test_reconcile_matches_markers_only_in_visible_description_text():
    href_only = _child(
        "child-href", "ID-2100", "PRI-2100", MARKER, title="Href child"
    )
    href_only["description"] = f'<a href="https://example.test/{MARKER}">Ссылка</a>'
    attribute_only = _child(
        "child-attribute", "ID-2101", "PRI-2101", MARKER, title="Attribute child"
    )
    attribute_only["description"] = f'<p data-marker="{MARKER}">Обычный текст</p>'
    comment_only = _child(
        "child-comment", "ID-2102", "PRI-2102", MARKER, title="Comment child"
    )
    comment_only["description"] = f"<!-- {MARKER} --><p>Обычный текст</p>"
    script_only = _child(
        "child-script", "ID-2103", "PRI-2103", MARKER, title="Script child"
    )
    script_only["description"] = f"<script>{MARKER}</script><p>Обычный текст</p>"
    style_only = _child(
        "child-style", "ID-2104", "PRI-2104", MARKER, title="Style child"
    )
    style_only["description"] = f"<style>.x{{content:'{MARKER}'}}</style><p>Текст</p>"
    malformed_attribute = _child(
        "child-malformed", "ID-2108", "PRI-2108", MARKER, title="Malformed child"
    )
    malformed_attribute["description"] = f'<a href="https://example.test/{MARKER}"'
    visible_anchor = _child(
        "child-visible-anchor", "ID-2105", "PRI-2105", MARKER, title="Anchor child"
    )
    visible_anchor["description"] = (
        f'<a href="https://example.test/task">{MARKER}</a>'
    )
    visible_small = _child(
        "child-visible-small", "ID-2106", "PRI-2106", MARKER, title="Small child"
    )
    lookalike = _child(
        "child-lookalike", "ID-2107", "PRI-2107", MARKER, title="Lookalike child"
    )
    lookalike["description"] = "<small>reviewer-subtask:not-a-hash</small>"
    provider, _ = build(state=State(tasks_by_column={
        "done-id": [
            href_only,
            attribute_only,
            comment_only,
            script_only,
            style_only,
            malformed_attribute,
            visible_anchor,
            visible_small,
            lookalike,
        ],
    }))

    found = provider.reconcile_native_subtasks("board-1", frozenset({MARKER}))

    assert [item.identity.board_id for item in found] == [
        "child-visible-anchor",
        "child-visible-small",
    ]


def test_reconcile_ignores_matching_tasks_without_transport_id():
    missing_id = _child(
        "unused", "ID-2198", "PRI-2198", MARKER, title="Missing id"
    )
    missing_id.pop("id")
    blank_id = _child(
        "   ", "ID-2199", "PRI-2199", MARKER, title="Blank id"
    )
    invalid_id = _child(
        "unused", "ID-2201", "PRI-2201", MARKER, title="Invalid id"
    )
    invalid_id["id"] = {"unsupported": "child-id"}
    valid = _child(
        "child-valid", "ID-2200", "PRI-2200", MARKER, title="Valid child"
    )
    provider, _ = build(state=State(tasks_by_column={
        "done-id": [missing_id, blank_id, invalid_id, valid],
    }))

    found = provider.reconcile_native_subtasks("board-1", frozenset({MARKER}))

    assert [item.identity.board_id for item in found] == ["child-valid"]


def test_reconcile_scopes_task_scanning_to_source_board_columns():
    local = _child("child-local", "ID-2201", "PRI-2201", MARKER, title="Local child")
    local["columnId"] = "local-id"
    foreign = _child(
        "child-foreign", "ID-2202", "PRI-2202", MARKER, title="Foreign child"
    )
    foreign["columnId"] = "foreign-id"
    state = State(
        columns_by_board={
            "board-1": [{"id": "local-id", "title": "Local", "boardId": "board-1"}],
            "board-2": [{"id": "foreign-id", "title": "Foreign", "boardId": "board-2"}],
        },
        tasks_by_column={"local-id": [local], "foreign-id": [foreign]},
    )
    provider, state = build(state=state)

    found = provider.reconcile_native_subtasks("board-1", frozenset({MARKER}))

    assert [item.identity.board_id for item in found] == ["child-local"]
    task_columns = {
        call[2].get("columnId")
        for call in state.calls
        if call[0] == "GET" and call[1].endswith("/tasks")
    }
    assert task_columns == {"local-id"}


def test_reconcile_with_no_markers_does_not_scan_tasks():
    provider, state = build()

    assert provider.reconcile_native_subtasks("board-1", frozenset()) == []
    assert not any(call[1].endswith("/tasks") for call in state.calls)


def test_replace_native_subtasks_sends_exact_caller_union():
    state = State(parent_subtasks={"uuid-1": ["sub-1", "stale"]})
    provider, state = build(state=state)

    provider.replace_native_subtasks("uuid-1", ["sub-1", "new"])

    assert state.parent_subtasks["uuid-1"] == ["sub-1", "new"]
    fetched = provider.fetch_one("ID-1")
    assert fetched is not None
    assert fetched.subtask_ids == ["sub-1", "new"]
    listed = next(iter(provider.iter_raw("PRI", 1)))
    assert listed.subtask_ids == ["sub-1", "new"]


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
