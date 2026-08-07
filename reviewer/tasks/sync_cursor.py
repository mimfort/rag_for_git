from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from reviewer.config.task_board import TaskSyncFilter, normalize_task_sync_filter
from reviewer.tasks.sync_filter import filter_fingerprint, filter_is_active

CURSOR_VERSION = 1


@dataclass(frozen=True)
class TaskSyncCursor:
    watermark: int
    old_filter: TaskSyncFilter | None
    old_filter_known: bool
    stored_value: str | None


@dataclass(frozen=True)
class CursorParseResult:
    cursor: TaskSyncCursor
    warning: str | None = None


def _invalid(value: str | None, reason: str) -> CursorParseResult:
    return CursorParseResult(
        TaskSyncCursor(0, None, False, value),
        f"sync cursor invalid: {reason}; full backfill enabled",
    )


def parse_task_sync_cursor(value: str | None) -> CursorParseResult:
    if value is None:
        return CursorParseResult(TaskSyncCursor(0, None, True, None))

    try:
        watermark = int(value)
    except (TypeError, ValueError):
        watermark = None
    if watermark is not None:
        if watermark < 0:
            return _invalid(value, "negative legacy watermark")
        return CursorParseResult(TaskSyncCursor(watermark, None, True, value))

    try:
        payload = json.loads(value)
        if not isinstance(payload, Mapping):
            return _invalid(value, "JSON value is not an object")

        version = payload.get("version")
        if type(version) is not int or version != CURSOR_VERSION:
            return _invalid(value, "unsupported version")

        raw_watermark = payload.get("watermark")
        if (
            not isinstance(raw_watermark, int)
            or isinstance(raw_watermark, bool)
            or raw_watermark < 0
        ):
            return _invalid(value, "watermark is not a non-negative integer")

        old_filter = normalize_task_sync_filter(payload.get("sync_filter"))
        expected_fingerprint = filter_fingerprint(old_filter)
        if (
            expected_fingerprint is None
            or payload.get("filter_fingerprint") != expected_fingerprint
        ):
            return _invalid(value, "filter fingerprint mismatch")
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return _invalid(value, "unparseable value")

    return CursorParseResult(TaskSyncCursor(raw_watermark, old_filter, True, value))


def serialize_task_sync_cursor(
    watermark: int,
    sync_filter: TaskSyncFilter | None,
) -> str:
    if not filter_is_active(sync_filter):
        return str(watermark)

    assert sync_filter is not None
    payload = {
        "filter_fingerprint": filter_fingerprint(sync_filter),
        "sync_filter": sync_filter.canonical_dict(),
        "version": CURSOR_VERSION,
        "watermark": watermark,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
