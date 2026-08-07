from __future__ import annotations

import hashlib
import json
from typing import Literal

from reviewer.config.task_board import TaskSyncFilter
from reviewer.tasks.boards.base import RawTask

DAY_MS = 86_400_000
FILTER_SEMANTICS_VERSION = 1
FilterDecision = Literal["eligible", "filtered_by_age", "filtered_archived"]


def filter_is_active(sync_filter: TaskSyncFilter | None) -> bool:
    return bool(
        sync_filter is not None
        and (
            sync_filter.max_age_days is not None
            or not sync_filter.include_archived
        )
    )


def classify_task(
    sync_filter: TaskSyncFilter | None,
    raw: RawTask,
    now_ms: int,
) -> FilterDecision:
    if sync_filter is None:
        return "eligible"
    if sync_filter.max_age_days is not None and raw.timestamp is not None:
        cutoff = now_ms - sync_filter.max_age_days * DAY_MS
        if raw.timestamp < cutoff:
            return "filtered_by_age"
    if not sync_filter.include_archived and raw.archived is True:
        return "filtered_archived"
    return "eligible"


def filter_fingerprint(
    sync_filter: TaskSyncFilter | None,
    *,
    semantics_version: int = FILTER_SEMANTICS_VERSION,
) -> str | None:
    if not filter_is_active(sync_filter):
        return None
    assert sync_filter is not None
    payload = {
        "semantics_version": semantics_version,
        "sync_filter": sync_filter.canonical_dict(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
