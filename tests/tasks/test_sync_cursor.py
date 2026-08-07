import json
import sys
from dataclasses import FrozenInstanceError

import pytest

from reviewer.config.task_board import TaskSyncFilter
from reviewer.tasks.sync_cursor import (
    CURSOR_VERSION,
    CursorParseResult,
    TaskSyncCursor,
    parse_task_sync_cursor,
    serialize_task_sync_cursor,
)


def _json_cursor(*, omit: str | None = None, **overrides: object) -> str:
    sync_filter = TaskSyncFilter(max_age_days=180, include_archived=False)
    payload: dict[str, object] = {
        "filter_fingerprint": (
            "sha256:cfd52772183d125dd19da907efcef6c88c6023c6b39163e70dee466383848079"
        ),
        "sync_filter": sync_filter.canonical_dict(),
        "version": CURSOR_VERSION,
        "watermark": 200,
    }
    payload.update(overrides)
    if omit is not None:
        payload.pop(omit)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _assert_full_backfill(parsed: CursorParseResult, stored_value: str) -> None:
    assert parsed.cursor == TaskSyncCursor(
        watermark=0,
        old_filter=None,
        old_filter_known=False,
        stored_value=stored_value,
    )
    assert parsed.warning is not None
    assert "cursor" in parsed.warning
    assert "full backfill" in parsed.warning


def _deeply_nested_array(inner: str) -> str:
    depth = sys.getrecursionlimit() * 2
    return "[" * depth + inner + "]" * depth


def test_cursor_contract_is_versioned_and_frozen() -> None:
    cursor = TaskSyncCursor(0, None, True, None)
    result = CursorParseResult(cursor)

    assert CURSOR_VERSION == 1
    with pytest.raises(FrozenInstanceError):
        cursor.watermark = 1
    with pytest.raises(FrozenInstanceError):
        result.warning = "changed"


def test_parse_missing_cursor_is_known_no_filter() -> None:
    parsed = parse_task_sync_cursor(None)

    assert parsed == CursorParseResult(TaskSyncCursor(0, None, True, None))


def test_parse_legacy_integer_preserves_known_no_filter() -> None:
    parsed = parse_task_sync_cursor("200")

    assert parsed == CursorParseResult(TaskSyncCursor(200, None, True, "200"))


@pytest.mark.parametrize("policy", [None, TaskSyncFilter()])
def test_inactive_filter_serializes_as_legacy_integer(
    policy: TaskSyncFilter | None,
) -> None:
    assert serialize_task_sync_cursor(200, policy) == "200"


def test_active_filter_serialization_is_exact_and_deterministic() -> None:
    policy = TaskSyncFilter(max_age_days=180, include_archived=False)
    expected = (
        '{"filter_fingerprint":"sha256:'
        'cfd52772183d125dd19da907efcef6c88c6023c6b39163e70dee466383848079",'
        '"sync_filter":{"include_archived":false,"max_age_days":180},'
        '"version":1,"watermark":200}'
    )

    assert serialize_task_sync_cursor(200, policy) == expected
    assert serialize_task_sync_cursor(200, policy) == expected


def test_active_filter_cursor_round_trips_exactly() -> None:
    policy = TaskSyncFilter(max_age_days=180, include_archived=False)
    encoded = serialize_task_sync_cursor(200, policy)

    parsed = parse_task_sync_cursor(encoded)

    assert parsed == CursorParseResult(
        TaskSyncCursor(200, policy, True, encoded),
    )
    assert (
        serialize_task_sync_cursor(parsed.cursor.watermark, parsed.cursor.old_filter)
        == encoded
    )


@pytest.mark.parametrize("value", ["not-json-or-int", "[]", "true"])
def test_corrupt_cursor_resets_and_marks_old_filter_unknown(value: str) -> None:
    _assert_full_backfill(parse_task_sync_cursor(value), value)


@pytest.mark.parametrize(
    "value",
    [
        _deeply_nested_array("null"),
        (
            '{"filter_fingerprint":"sha256:invalid","sync_filter":{"unknown":'
            + _deeply_nested_array("null")
            + '},"version":1,"watermark":200}'
        ),
    ],
    ids=["json", "sync-filter"],
)
def test_deeply_nested_cursor_requires_full_backfill(value: str) -> None:
    _assert_full_backfill(parse_task_sync_cursor(value), value)


def test_unsupported_cursor_version_requires_full_backfill() -> None:
    value = _json_cursor(version=CURSOR_VERSION + 1)

    _assert_full_backfill(parse_task_sync_cursor(value), value)


@pytest.mark.parametrize(
    "value",
    [
        "-1",
        _json_cursor(watermark=-1),
        _json_cursor(watermark=True),
    ],
)
def test_invalid_watermark_requires_full_backfill(value: str) -> None:
    _assert_full_backfill(parse_task_sync_cursor(value), value)


@pytest.mark.parametrize(
    "value",
    [
        _json_cursor(omit="filter_fingerprint"),
        _json_cursor(filter_fingerprint="sha256:mismatch"),
    ],
)
def test_missing_or_mismatched_fingerprint_requires_full_backfill(value: str) -> None:
    _assert_full_backfill(parse_task_sync_cursor(value), value)


@pytest.mark.parametrize(
    "sync_filter",
    [
        None,
        [],
        {},
        {"max_age_days": True, "include_archived": False},
        {"max_age_days": 180, "unknown": False},
    ],
)
def test_invalid_or_inactive_json_filter_requires_full_backfill(
    sync_filter: object,
) -> None:
    value = _json_cursor(sync_filter=sync_filter)

    _assert_full_backfill(parse_task_sync_cursor(value), value)
