from typing import get_args

import pytest

from reviewer.config.task_board import TaskSyncFilter
from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.sync_filter import (
    DAY_MS,
    FILTER_SEMANTICS_VERSION,
    FilterDecision,
    classify_task,
    filter_fingerprint,
    filter_is_active,
)

NOW = 2_000_000_000_000


def _raw(
    *,
    timestamp: int | None = NOW,
    archived: bool | None = False,
    terminal: bool | None = None,
) -> RawTask:
    return RawTask(
        "ID-1",
        "PRI-1",
        "T",
        "",
        None,
        [],
        timestamp,
        archived=archived,
        terminal=terminal,
    )


def test_filter_contract_constants_and_decisions() -> None:
    assert DAY_MS == 86_400_000
    assert FILTER_SEMANTICS_VERSION == 1
    assert get_args(FilterDecision) == (
        "eligible",
        "filtered_by_age",
        "filtered_archived",
    )


def test_absent_filter_is_a_noop() -> None:
    raw = _raw(timestamp=0, archived=True, terminal=True)

    assert classify_task(None, raw, NOW) == "eligible"


@pytest.mark.parametrize(
    ("offset_ms", "expected"),
    [
        (-1, "filtered_by_age"),
        (0, "eligible"),
        (1, "eligible"),
    ],
)
def test_age_cutoff_is_strict(offset_ms: int, expected: FilterDecision) -> None:
    policy = TaskSyncFilter(max_age_days=30)
    cutoff = NOW - 30 * DAY_MS

    assert classify_task(policy, _raw(timestamp=cutoff + offset_ms), NOW) == expected


def test_age_classification_precedes_archive() -> None:
    policy = TaskSyncFilter(max_age_days=30, include_archived=False)

    assert (
        classify_task(
            policy,
            _raw(timestamp=NOW - 31 * DAY_MS, archived=True),
            NOW,
        )
        == "filtered_by_age"
    )


@pytest.mark.parametrize(
    ("archived", "expected"),
    [
        (True, "filtered_archived"),
        (False, "eligible"),
        (None, "eligible"),
    ],
)
def test_archive_classification_is_strictly_true(
    archived: bool | None,
    expected: FilterDecision,
) -> None:
    policy = TaskSyncFilter(include_archived=False)

    assert classify_task(policy, _raw(archived=archived), NOW) == expected


@pytest.mark.parametrize(
    ("archived", "expected"),
    [
        (True, "filtered_archived"),
        (False, "eligible"),
    ],
)
def test_unknown_timestamp_continues_to_archive_classification(
    archived: bool,
    expected: FilterDecision,
) -> None:
    policy = TaskSyncFilter(max_age_days=30, include_archived=False)

    assert classify_task(policy, _raw(timestamp=None, archived=archived), NOW) == expected


@pytest.mark.parametrize("terminal", [True, False, None])
def test_terminal_state_is_ignored(terminal: bool | None) -> None:
    policy = TaskSyncFilter(include_archived=False)

    assert classify_task(policy, _raw(archived=False, terminal=terminal), NOW) == "eligible"


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (None, False),
        (TaskSyncFilter(), False),
        (TaskSyncFilter(max_age_days=1), True),
        (TaskSyncFilter(include_archived=False), True),
        (TaskSyncFilter(max_age_days=1, include_archived=False), True),
    ],
)
def test_filter_is_active_only_for_effective_restrictions(
    policy: TaskSyncFilter | None,
    expected: bool,
) -> None:
    assert filter_is_active(policy) is expected


@pytest.mark.parametrize("policy", [None, TaskSyncFilter()])
def test_inactive_filter_has_no_fingerprint(policy: TaskSyncFilter | None) -> None:
    assert filter_fingerprint(policy) is None


def test_filter_fingerprint_is_stable_and_versioned() -> None:
    policy = TaskSyncFilter(max_age_days=180, include_archived=False)
    current = filter_fingerprint(policy)

    assert current == (
        "sha256:cfd52772183d125dd19da907efcef6c88c6023c6b39163e70dee466383848079"
    )
    assert current == filter_fingerprint(policy)
    assert current != filter_fingerprint(
        policy,
        semantics_version=FILTER_SEMANTICS_VERSION + 1,
    )


def test_filter_fingerprint_changes_with_either_policy_field() -> None:
    current = filter_fingerprint(TaskSyncFilter(max_age_days=180, include_archived=False))

    assert current != filter_fingerprint(
        TaskSyncFilter(max_age_days=181, include_archived=False)
    )
    assert current != filter_fingerprint(
        TaskSyncFilter(max_age_days=180, include_archived=True)
    )
