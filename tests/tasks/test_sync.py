import inspect
import logging

import pytest

from reviewer.config.task_board import TaskSyncFilter
from reviewer.tasks.boards.base import RawTask, TaskListing, TaskListingStats
from reviewer.tasks.sync import SyncProvider, SyncService
from reviewer.tasks.sync_cursor import (
    parse_task_sync_cursor,
    serialize_task_sync_cursor,
)
from reviewer.tasks.sync_filter import DAY_MS, filter_fingerprint


NOW = 2_000_000_000_000


class FakeProvider:
    board_type = "fake"

    def __init__(self, raws, board_type="fake", stats=None):
        self._raws = raws
        self.board_type = board_type
        self._stats = stats or TaskListingStats()
        self.hints = []
        self.normalized = []
        self.meta_normalized = []

    def iter_raw(self, board, limit, *, sync_filter=None, now_ms=None):
        self.hints.append({"sync_filter": sync_filter, "now_ms": now_ms})
        listing_stats = TaskListingStats()

        def rows():
            n = 0
            try:
                for raw in self._raws:
                    if limit is not None and n >= limit:
                        return
                    yield raw
                    n += 1
            finally:
                listing_stats.filtered_by_age = self._stats.filtered_by_age
                listing_stats.filtered_archived = self._stats.filtered_archived
                listing_stats.warnings.extend(self._stats.warnings)

        return TaskListing(rows=rows(), stats=listing_stats)

    def normalize(self, raw):
        self.normalized.append(raw.key)
        return {"key": raw.key, "aliases": [raw.project_code], "title": raw.title,
                "description": raw.description, "criteria": [], "status": raw.status,
                "url": None, "links": []}

    def normalize_meta(self, raw):
        self.meta_normalized.append(raw.key)
        return {"key": raw.key, "aliases": [raw.project_code], "title": raw.title,
                "status": raw.status, "url": None,
                "project": raw.project_code.split("-")[0]}


class FakeTaskService:
    def __init__(self):
        self.indexed = []
        self.purged_with = None
        self.meta_refreshed = []

    def index_batch(self, tasks):
        self.indexed.append([t["key"] for t in tasks])
        return [{"key": t["key"], "embedded": True, "links_upserted": 0,
                 "prs_linked": 0, "warnings": [], "retry_required": False}
                for t in tasks]

    def refresh_meta_batch(self, metas):
        self.meta_refreshed.append([m["key"] for m in metas])
        return {"meta_refreshed": len(metas), "warnings": []}

    def purge_orphaned_tasks(self, active_keys, *, keep_with_prs=True, project=None):
        self.purged_with = (sorted(active_keys), keep_with_prs, project)
        return {"deleted_store": 1, "deleted_graph": 1, "protected_prs": 0,
                "warnings": []}


class FakeMeta:
    def __init__(self, init=None, *, read_error=False, write_error=False):
        self.store = dict(init or {})
        self.read_error = read_error
        self.write_error = write_error
        self.get_calls = []
        self.set_calls = []

    def get_index_meta(self, repo, ref):
        self.get_calls.append((repo, ref))
        if self.read_error:
            raise RuntimeError("cursor read failed")
        return self.store.get((repo, ref))

    def set_index_meta(self, repo, ref, sha):
        self.set_calls.append((repo, ref, sha))
        if self.write_error:
            raise RuntimeError("cursor write failed")
        self.store[(repo, ref)] = sha


def _raw(key, ts, *, archived=False):
    return RawTask(key=key, project_code=key.replace("ID", "PRI"), title=key,
                   description="", status="S", subtask_ids=[], timestamp=ts,
                   archived=archived)


def test_no_filter_preserves_legacy_summary_and_integer_cursor():
    clock_calls = 0

    def now_ms():
        nonlocal clock_calls
        clock_calls += 1
        return 1_000

    provider = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    tasks, meta = FakeTaskService(), FakeMeta()

    summary = SyncService([provider], tasks, meta, now_ms=now_ms).run()

    assert clock_calls == 1
    assert provider.hints == [{"sync_filter": None, "now_ms": 1_000}]
    assert meta.store[("", "tasks:fake:*")] == "200"
    assert summary["eligible"] == summary["enumerated"] == 2
    assert summary["filtered_by_age"] == 0
    assert summary["filtered_archived"] == 0
    assert summary["age_unknown"] == 0
    assert summary["archive_unknown"] == 0
    assert summary["filter_applied"] is False
    assert summary["filter_fingerprint"] is None
    assert summary["filter_source"] is None
    assert inspect.signature(SyncService.run).parameters["sync_filter"].kind is (
        inspect.Parameter.KEYWORD_ONLY
    )


def test_filtered_rows_never_reach_normalize_or_normalize_meta():
    sync_filter = TaskSyncFilter(max_age_days=30, include_archived=False)
    provider = FakeProvider([
        _raw("OLD-1", NOW - 31 * DAY_MS),
        _raw("ARCHIVED-1", NOW - DAY_MS, archived=True),
        _raw("ELIGIBLE-1", NOW - DAY_MS),
    ])
    tasks = FakeTaskService()
    meta = FakeMeta({("", "tasks:fake:*"): str(NOW)})

    summary = SyncService(
        [provider], tasks, meta, now_ms=lambda: NOW
    ).run(sync_filter=sync_filter)

    assert provider.normalized == []
    assert provider.meta_normalized == ["ELIGIBLE-1"]
    assert summary["enumerated"] == 3
    assert summary["eligible"] == 1
    assert summary["filtered_by_age"] == 1
    assert summary["filtered_archived"] == 1
    assert summary["enumerated"] == (
        summary["eligible"]
        + summary["filtered_by_age"]
        + summary["filtered_archived"]
    )


def test_provider_and_local_filter_counts_merge_exactly():
    secret = "provider-stats-secret"
    provider = FakeProvider(
        [
            _raw("OLD-1", NOW - 31 * DAY_MS),
            _raw("ARCHIVED-1", NOW, archived=True),
            _raw("ELIGIBLE-1", NOW),
        ],
        stats=TaskListingStats(
            filtered_by_age=2,
            filtered_archived=3,
            warnings=[f"provider warning contains {secret}"],
        ),
    )
    sync_filter = TaskSyncFilter(max_age_days=30, include_archived=False)

    summary = SyncService(
        [SyncProvider(provider, frozenset({secret}))],
        FakeTaskService(),
        FakeMeta(),
        now_ms=lambda: NOW,
    ).run(sync_filter=sync_filter)

    assert summary["enumerated"] == 8
    assert summary["eligible"] == 1
    assert summary["filtered_by_age"] == 3
    assert summary["filtered_archived"] == 4
    assert summary["enumerated"] == (
        summary["eligible"]
        + summary["filtered_by_age"]
        + summary["filtered_archived"]
    )
    assert secret not in repr(summary["warnings"])
    assert "[REDACTED]" in repr(summary["warnings"])


def test_unknown_timestamp_with_age_filter_is_full_normalized_and_warned_once():
    provider = FakeProvider([
        _raw("ID-1", None),
        _raw("ID-2", None),
    ])
    meta = FakeMeta({("", "tasks:fake:*"): str(NOW)})

    summary = SyncService(
        [provider], FakeTaskService(), meta, now_ms=lambda: NOW
    ).run(sync_filter=TaskSyncFilter(max_age_days=30))

    assert provider.normalized == ["ID-1", "ID-2"]
    assert provider.meta_normalized == []
    assert summary["eligible"] == 2
    assert summary["age_unknown"] == 2
    assert sum("timestamp" in warning for warning in summary["warnings"]) == 1


def test_unknown_archive_is_eligible_counted_and_warned_once():
    provider = FakeProvider([
        _raw("ID-1", NOW, archived=None),
        _raw("ID-2", NOW + 1, archived=None),
    ])

    summary = SyncService(
        [provider], FakeTaskService(), FakeMeta(), now_ms=lambda: NOW
    ).run(sync_filter=TaskSyncFilter(include_archived=False))

    assert summary["enumerated"] == summary["eligible"] == 2
    assert summary["filtered_archived"] == 0
    assert summary["archive_unknown"] == 2
    assert sum("archived" in warning for warning in summary["warnings"]) == 1


def test_age_filtered_row_does_not_increment_archive_unknown():
    provider = FakeProvider([
        _raw("OLD-1", NOW - 31 * DAY_MS, archived=None),
    ])

    summary = SyncService(
        [provider], FakeTaskService(), FakeMeta(), now_ms=lambda: NOW
    ).run(sync_filter=TaskSyncFilter(max_age_days=30, include_archived=False))

    assert summary["filtered_by_age"] == 1
    assert summary["filtered_archived"] == 0
    assert summary["archive_unknown"] == 0
    assert not any("archived" in warning for warning in summary["warnings"])


def test_by_board_contains_retention_counts_and_source():
    sync_filter = TaskSyncFilter(include_archived=False)
    source = "repo:.review.yml"
    provider = FakeProvider([_raw("ID-1", NOW)])

    summary = SyncService(
        [provider], FakeTaskService(), FakeMeta(), now_ms=lambda: NOW
    ).run(sync_filter=sync_filter, filter_source=source)

    expected = {
        "eligible": 1,
        "filtered_by_age": 0,
        "filtered_archived": 0,
        "age_unknown": 0,
        "archive_unknown": 0,
        "filter_applied": True,
        "filter_fingerprint": filter_fingerprint(sync_filter),
        "filter_source": source,
    }
    assert {key: summary[key] for key in expected} == expected
    assert {key: summary["by_board"][0][key] for key in expected} == expected


def test_limit_passes_no_filter_hint_and_remains_raw_cap():
    sync_filter = TaskSyncFilter(max_age_days=30)
    provider = FakeProvider([
        _raw("ID-1", NOW),
        _raw("ID-2", NOW - 31 * DAY_MS),
    ])

    summary = SyncService(
        [provider], FakeTaskService(), FakeMeta(), now_ms=lambda: NOW
    ).run(limit=1, sync_filter=sync_filter)

    assert provider.hints == [{"sync_filter": None, "now_ms": None}]
    assert provider.normalized == ["ID-1"]
    assert summary["enumerated"] == summary["eligible"] == 1


def test_looser_age_filter_backfills_newly_eligible_row_below_watermark():
    old_filter = TaskSyncFilter(max_age_days=30)
    new_filter = TaskSyncFilter(max_age_days=180)
    provider = FakeProvider([_raw("ID-1", NOW - 60 * DAY_MS)])
    tasks = FakeTaskService()
    meta = FakeMeta({
        ("", "tasks:fake:*"): serialize_task_sync_cursor(NOW, old_filter),
    })

    summary = SyncService(
        [provider], tasks, meta, now_ms=lambda: NOW
    ).run(sync_filter=new_filter)

    assert provider.normalized == ["ID-1"]
    assert provider.meta_normalized == []
    assert tasks.indexed == [["ID-1"]]
    assert summary["changed"] == 1
    assert summary["cursor_advanced"] is False
    parsed = parse_task_sync_cursor(meta.store[("", "tasks:fake:*")])
    assert parsed.cursor.old_filter == new_filter


def test_enabling_archived_tasks_backfills_archived_row_below_watermark():
    old_filter = TaskSyncFilter(max_age_days=365, include_archived=False)
    new_filter = TaskSyncFilter(max_age_days=365, include_archived=True)
    provider = FakeProvider([_raw("ID-1", NOW - DAY_MS, archived=True)])
    meta = FakeMeta({
        ("", "tasks:fake:*"): serialize_task_sync_cursor(NOW, old_filter),
    })

    summary = SyncService(
        [provider], FakeTaskService(), meta, now_ms=lambda: NOW
    ).run(sync_filter=new_filter)

    assert provider.normalized == ["ID-1"]
    assert provider.meta_normalized == []
    assert summary["changed"] == 1
    assert parse_task_sync_cursor(
        meta.store[("", "tasks:fake:*")]
    ).cursor.old_filter == new_filter


def test_removing_filter_backfills_and_writes_legacy_integer():
    old_filter = TaskSyncFilter(max_age_days=30)
    provider = FakeProvider([_raw("ID-1", NOW - 60 * DAY_MS)])
    meta = FakeMeta({
        ("", "tasks:fake:*"): serialize_task_sync_cursor(NOW, old_filter),
    })

    summary = SyncService(
        [provider], FakeTaskService(), meta, now_ms=lambda: NOW
    ).run()

    assert provider.normalized == ["ID-1"]
    assert provider.meta_normalized == []
    assert meta.store[("", "tasks:fake:*")] == str(NOW)
    assert summary["cursor_advanced"] is False


def test_row_eligible_under_old_and_new_filter_uses_metadata_refresh():
    old_filter = TaskSyncFilter(max_age_days=30)
    new_filter = TaskSyncFilter(max_age_days=180)
    provider = FakeProvider([_raw("ID-1", NOW - DAY_MS)])
    tasks = FakeTaskService()
    meta = FakeMeta({
        ("", "tasks:fake:*"): serialize_task_sync_cursor(NOW, old_filter),
    })

    summary = SyncService(
        [provider], tasks, meta, now_ms=lambda: NOW
    ).run(sync_filter=new_filter)

    assert provider.normalized == []
    assert provider.meta_normalized == ["ID-1"]
    assert tasks.meta_refreshed == [["ID-1"]]
    assert summary["changed"] == 0
    assert summary["unchanged"] == 1


def test_tightening_filter_excludes_row_without_normalization():
    old_filter = TaskSyncFilter(max_age_days=180)
    new_filter = TaskSyncFilter(max_age_days=30)
    provider = FakeProvider([_raw("ID-1", NOW - 60 * DAY_MS)])
    meta = FakeMeta({
        ("", "tasks:fake:*"): serialize_task_sync_cursor(NOW, old_filter),
    })

    summary = SyncService(
        [provider], FakeTaskService(), meta, now_ms=lambda: NOW
    ).run(sync_filter=new_filter)

    assert provider.normalized == []
    assert provider.meta_normalized == []
    assert summary["filtered_by_age"] == 1
    assert parse_task_sync_cursor(
        meta.store[("", "tasks:fake:*")]
    ).cursor.old_filter == new_filter


def test_natural_aging_filters_without_fingerprint_change():
    sync_filter = TaskSyncFilter(max_age_days=30)
    stored = serialize_task_sync_cursor(NOW, sync_filter)
    provider = FakeProvider([_raw("ID-1", NOW - 31 * DAY_MS)])
    meta = FakeMeta({("", "tasks:fake:*"): stored})

    summary = SyncService(
        [provider], FakeTaskService(), meta, now_ms=lambda: NOW
    ).run(sync_filter=sync_filter)

    assert provider.normalized == []
    assert provider.meta_normalized == []
    assert summary["filtered_by_age"] == 1
    assert meta.set_calls == []
    assert meta.store[("", "tasks:fake:*")] == stored


def test_unknown_old_filter_full_normalizes_every_currently_eligible_row():
    sync_filter = TaskSyncFilter(include_archived=False)
    provider = FakeProvider([
        _raw("ID-1", None, archived=False),
        _raw("ID-2", None, archived=None),
    ])
    meta = FakeMeta({("", "tasks:fake:*"): "corrupt-state"})

    summary = SyncService(
        [provider], FakeTaskService(), meta, now_ms=lambda: NOW
    ).run(sync_filter=sync_filter)

    assert provider.normalized == ["ID-1", "ID-2"]
    assert provider.meta_normalized == []
    assert summary["changed"] == 2
    assert any("cursor" in warning for warning in summary["warnings"])


def test_filter_state_rewrites_without_marking_cursor_advanced():
    sync_filter = TaskSyncFilter(max_age_days=30)
    provider = FakeProvider([_raw("ID-1", NOW - DAY_MS)])
    meta = FakeMeta({("", "tasks:fake:*"): str(NOW)})

    summary = SyncService(
        [provider], FakeTaskService(), meta, now_ms=lambda: NOW
    ).run(sync_filter=sync_filter)

    assert provider.meta_normalized == ["ID-1"]
    assert len(meta.set_calls) == 1
    assert parse_task_sync_cursor(
        meta.store[("", "tasks:fake:*")]
    ).cursor.old_filter == sync_filter
    assert summary["cursor_advanced"] is False


def test_limit_zero_disables_purge_pushdown_and_all_cursor_state_writes():
    sync_filter = TaskSyncFilter(max_age_days=30)
    provider = FakeProvider([_raw("ID-1", NOW), _raw("ID-2", NOW + 1)])
    tasks = FakeTaskService()
    meta = FakeMeta()

    summary = SyncService(
        [provider], tasks, meta, now_ms=lambda: NOW
    ).run(limit=0, purge_orphaned=True, sync_filter=sync_filter)

    assert provider.hints == [{"sync_filter": None, "now_ms": None}]
    assert summary["enumerated"] == 0
    assert meta.set_calls == []
    assert tasks.purged_with is None
    assert any("limit" in warning for warning in summary["warnings"])


@pytest.mark.parametrize("fail_during_iteration", [False, True])
def test_listing_failure_skips_index_purge_and_cursor_write(
    fail_during_iteration, caplog
):
    secret = "listing-secret"

    class _FailingProvider(FakeProvider):
        def iter_raw(self, board, limit, *, sync_filter=None, now_ms=None):
            self.hints.append({"sync_filter": sync_filter, "now_ms": now_ms})
            if not fail_during_iteration:
                raise RuntimeError(f"listing construction exposed {secret}")

            def rows():
                yield _raw("ID-1", 100)
                raise RuntimeError(f"listing stream exposed {secret}")

            return TaskListing(rows=rows())

    provider = _FailingProvider([])
    tasks = FakeTaskService()
    meta = FakeMeta()
    service = SyncService(
        [SyncProvider(provider, frozenset({secret}))], tasks, meta
    )

    with caplog.at_level(logging.WARNING):
        summary = service.run(
            board="PRI", board_type="fake", purge_orphaned=True
        )

    assert tasks.indexed == []
    assert tasks.meta_refreshed == []
    assert tasks.purged_with is None
    assert meta.set_calls == []
    assert summary["changed"] == 0
    assert "[REDACTED]" in repr(summary["warnings"])
    assert secret not in repr(summary)
    assert secret not in caplog.text
    assert sum("purge" in warning for warning in summary["warnings"]) == 1


def test_any_incomplete_provider_skips_deploy_wide_union_purge():
    class _FailingProvider(FakeProvider):
        def iter_raw(self, board, limit, *, sync_filter=None, now_ms=None):
            raise RuntimeError("second provider failed")

    healthy = FakeProvider([_raw("ID-1", 100)], board_type="healthy")
    failing = _FailingProvider([], board_type="failing")
    tasks = FakeTaskService()
    meta = FakeMeta()

    summary = SyncService([healthy, failing], tasks, meta).run(
        purge_orphaned=True
    )

    assert tasks.indexed == [["ID-1"]]
    assert tasks.purged_with is None
    assert ("", "tasks:healthy:*") in meta.store
    assert ("", "tasks:failing:*") not in meta.store
    assert sum("purge" in warning for warning in summary["warnings"]) == 1


def test_cursor_read_failure_warns_and_full_backfills():
    provider = FakeProvider([_raw("ID-1", None)])
    meta = FakeMeta(read_error=True)

    summary = SyncService([provider], FakeTaskService(), meta).run()

    assert provider.normalized == ["ID-1"]
    assert provider.meta_normalized == []
    assert any("cursor" in warning for warning in summary["warnings"])
    assert len(meta.set_calls) == 1


def test_corrupt_cursor_warns_full_backfills_and_repairs_state():
    provider = FakeProvider([_raw("ID-1", None)])
    meta = FakeMeta({("", "tasks:fake:*"): "not-a-cursor"})

    summary = SyncService([provider], FakeTaskService(), meta).run()

    assert provider.normalized == ["ID-1"]
    assert provider.meta_normalized == []
    assert any("cursor" in warning for warning in summary["warnings"])
    assert meta.store[("", "tasks:fake:*")] == "0"
    assert summary["cursor_advanced"] is False


def test_cursor_write_failure_keeps_indexed_results_and_warns():
    provider = FakeProvider([_raw("ID-1", 100)])
    tasks = FakeTaskService()
    meta = FakeMeta(write_error=True)

    summary = SyncService([provider], tasks, meta).run()

    assert tasks.indexed == [["ID-1"]]
    assert summary["changed"] == 1
    assert summary["cursor_advanced"] is False
    assert any("cursor" in warning for warning in summary["warnings"])


def test_normalize_failure_keeps_eligible_key_active_for_purge():
    class _FailingProvider(FakeProvider):
        def normalize(self, raw):
            raise RuntimeError("normalize failed")

    provider = _FailingProvider([_raw("ID-1", 100)])
    tasks = FakeTaskService()

    summary = SyncService([provider], tasks, FakeMeta()).run(
        purge_orphaned=True
    )

    assert summary["changed"] == 0
    assert tasks.purged_with == (["ID-1"], True, None)


def test_transition_normalize_failure_preserves_cursor_and_retries_full_normalize():
    class _RetryingProvider(FakeProvider):
        def __init__(self, raws):
            super().__init__(raws)
            self.fail_normalize = True
            self.normalize_attempts = []

        def normalize(self, raw):
            self.normalize_attempts.append(raw.key)
            if self.fail_normalize:
                raise RuntimeError("normalize retry required")
            return super().normalize(raw)

    old_filter = TaskSyncFilter(max_age_days=30)
    new_filter = TaskSyncFilter(max_age_days=180)
    stored = serialize_task_sync_cursor(NOW, old_filter)
    provider = _RetryingProvider([_raw("ID-1", NOW - 60 * DAY_MS)])
    tasks = FakeTaskService()
    meta = FakeMeta({("", "tasks:fake:*"): stored})
    service = SyncService([provider], tasks, meta, now_ms=lambda: NOW)

    first = service.run(
        purge_orphaned=True,
        sync_filter=new_filter,
    )

    assert first["changed"] == 0
    assert any("normalize" in warning for warning in first["warnings"])
    assert meta.set_calls == []
    assert meta.store[("", "tasks:fake:*")] == stored
    assert tasks.purged_with == (["ID-1"], True, None)

    provider.fail_normalize = False
    second = service.run(sync_filter=new_filter)

    assert provider.normalize_attempts == ["ID-1", "ID-1"]
    assert second["changed"] == 1
    assert tasks.indexed == [["ID-1"]]
    assert parse_task_sync_cursor(
        meta.store[("", "tasks:fake:*")]
    ).cursor.old_filter == new_filter


def test_store_failure_result_preserves_cursor_and_retries_full_normalize():
    class _RetryingTaskService(FakeTaskService):
        def __init__(self):
            super().__init__()
            self.fail_index = True

        def index_batch(self, tasks):
            self.indexed.append([task["key"] for task in tasks])
            if self.fail_index:
                return [
                    {
                        "key": task["key"],
                        "embedded": False,
                        "warnings": ["store retry required"],
                        "retry_required": True,
                    }
                    for task in tasks
                ]
            return [
                {
                    "key": task["key"],
                    "embedded": True,
                    "warnings": [],
                    "retry_required": False,
                }
                for task in tasks
            ]

    old_filter = TaskSyncFilter(max_age_days=30)
    new_filter = TaskSyncFilter(max_age_days=180)
    stored = serialize_task_sync_cursor(NOW, old_filter)
    provider = FakeProvider([_raw("ID-1", NOW - 60 * DAY_MS)])
    tasks = _RetryingTaskService()
    meta = FakeMeta({("", "tasks:fake:*"): stored})
    service = SyncService([provider], tasks, meta, now_ms=lambda: NOW)

    first = service.run(sync_filter=new_filter)

    assert first["changed"] == 1
    assert first["failed"] == 1
    assert "store retry required" in first["warnings"]
    assert meta.set_calls == []
    assert meta.store[("", "tasks:fake:*")] == stored

    tasks.fail_index = False
    second = service.run(sync_filter=new_filter)

    assert provider.normalized == ["ID-1", "ID-1"]
    assert second["changed"] == 1
    assert second["failed"] == 0
    assert tasks.indexed == [["ID-1"], ["ID-1"]]
    assert parse_task_sync_cursor(
        meta.store[("", "tasks:fake:*")]
    ).cursor.old_filter == new_filter


def test_graph_warning_result_still_persists_transition_cursor():
    class _GraphWarningTaskService(FakeTaskService):
        def index_batch(self, tasks):
            self.indexed.append([task["key"] for task in tasks])
            return [
                {
                    "key": task["key"],
                    "embedded": True,
                    "warnings": ["graph unavailable"],
                    "retry_required": False,
                }
                for task in tasks
            ]

    old_filter = TaskSyncFilter(max_age_days=30)
    new_filter = TaskSyncFilter(max_age_days=180)
    provider = FakeProvider([_raw("ID-1", NOW - 60 * DAY_MS)])
    tasks = _GraphWarningTaskService()
    meta = FakeMeta({
        ("", "tasks:fake:*"): serialize_task_sync_cursor(NOW, old_filter),
    })

    summary = SyncService(
        [provider], tasks, meta, now_ms=lambda: NOW
    ).run(sync_filter=new_filter)

    assert summary["failed"] == 1
    assert summary["warnings"] == ["graph unavailable"]
    assert len(meta.set_calls) == 1
    assert parse_task_sync_cursor(
        meta.store[("", "tasks:fake:*")]
    ).cursor.old_filter == new_filter


@pytest.mark.parametrize("keep_with_prs", [True, False])
def test_purge_receives_only_eligible_keys(keep_with_prs):
    sync_filter = TaskSyncFilter(max_age_days=30, include_archived=False)
    provider = FakeProvider([
        _raw("OLD", NOW - 31 * DAY_MS),
        _raw("ARCHIVED", NOW, archived=True),
        _raw("ARCHIVE-UNKNOWN", NOW, archived=None),
        _raw("ELIGIBLE", NOW),
    ])
    tasks = FakeTaskService()

    SyncService(
        [provider], tasks, FakeMeta(), now_ms=lambda: NOW
    ).run(
        board="PRI",
        board_type="fake",
        purge_orphaned=True,
        keep_with_prs=keep_with_prs,
        sync_filter=sync_filter,
    )

    assert tasks.purged_with == (
        ["ARCHIVE-UNKNOWN", "ELIGIBLE"],
        keep_with_prs,
        "PRI",
    )


def test_first_sync_indexes_all_and_advances_cursor():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts, meta = FakeTaskService(), FakeMeta()
    summary = SyncService([prov], ts, meta).run()
    assert ts.indexed == [["ID-1", "ID-2"]]
    assert summary["changed"] == 2 and summary["unchanged"] == 0
    assert summary["embedded"] == 2
    assert summary["cursor_advanced"] is True
    assert meta.store[("", "tasks:fake:*")] == "200"
    assert summary["meta_refreshed"] == 0        # все changed, unchanged нет
    assert ts.meta_refreshed == []


def test_watermark_skips_unchanged():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts = FakeTaskService()
    meta = FakeMeta({("", "tasks:fake:*"): "150"})
    summary = SyncService([prov], ts, meta).run()
    assert ts.indexed == [["ID-2"]]
    assert summary["changed"] == 1 and summary["unchanged"] == 1
    assert meta.store[("", "tasks:fake:*")] == "200"
    assert summary["meta_refreshed"] == 1        # ID-1 (ниже курсора) meta-refreshнут
    assert ts.meta_refreshed == [["ID-1"]]


def test_no_changes_does_not_advance_cursor():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts = FakeTaskService()
    meta = FakeMeta({("", "tasks:fake:*"): "200"})
    summary = SyncService([prov], ts, meta).run()
    assert ts.indexed == []
    assert summary["changed"] == 0 and summary["unchanged"] == 2
    assert summary["cursor_advanced"] is False   # meta-refresh курсор НЕ двигает
    assert summary["meta_refreshed"] == 2        # обе задачи ниже курсора
    assert ts.meta_refreshed == [["ID-1", "ID-2"]]


def test_purge_uses_full_active_keys():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts = FakeTaskService()
    meta = FakeMeta({("", "tasks:fake:*"): "999"})
    summary = SyncService([prov], ts, meta).run(purge_orphaned=True, keep_with_prs=False)
    assert ts.purged_with == (["ID-1", "ID-2"], False, None)
    assert summary["purge"]["deleted"] == 2


def test_limit_disables_purge_and_cursor():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts, meta = FakeTaskService(), FakeMeta()
    summary = SyncService([prov], ts, meta).run(limit=1, purge_orphaned=True)
    assert ts.purged_with is None
    assert summary["cursor_advanced"] is False
    assert ("", "tasks:fake:*") not in meta.store
    assert any("limit" in w for w in summary["warnings"])


def test_board_scoped_cursor_ref():
    prov = FakeProvider([_raw("ID-1", 100)])
    ts, meta = FakeTaskService(), FakeMeta()
    SyncService([prov], ts, meta).run(board="MyBoard")
    assert ("", "tasks:fake:MyBoard") in meta.store


def test_multi_provider_separate_cursors_and_union_purge():
    a = FakeProvider([_raw("ID-1", 100)], board_type="yougile")
    b = FakeProvider([_raw("ID-2", 300)], board_type="youtrack")
    ts, meta = FakeTaskService(), FakeMeta()
    summary = SyncService([a, b], ts, meta).run(purge_orphaned=True)
    # каждый провайдер — свой курсор
    assert meta.store[("", "tasks:yougile:*")] == "100"
    assert meta.store[("", "tasks:youtrack:*")] == "300"
    # counts агрегированы
    assert summary["enumerated"] == 2 and summary["changed"] == 2
    # purge — по ОБЪЕДИНЕНИЮ ключей обеих досок (иначе A удалит задачи B)
    assert ts.purged_with == (["ID-1", "ID-2"], True, None)


def test_empty_providers_no_crash():
    ts, meta = FakeTaskService(), FakeMeta()
    summary = SyncService([], ts, meta).run()
    assert summary["enumerated"] == 0 and summary["changed"] == 0
    assert summary["purge"] is None


def test_board_type_scopes_to_one_provider():
    a = FakeProvider([_raw("ID-1", 100)], board_type="yougile")
    b = FakeProvider([_raw("ID-2", 300)], board_type="youtrack")
    ts, meta = FakeTaskService(), FakeMeta()
    summary = SyncService([a, b], ts, meta).run(board_type="yougile")
    assert ts.indexed == [["ID-1"]]            # только yougile-провайдер
    assert summary["enumerated"] == 1
    assert ("", "tasks:youtrack:*") not in meta.store
    assert len(summary["by_board"]) == 1
    assert summary["by_board"][0]["board_type"] == "yougile"


def test_board_type_none_syncs_all_providers():
    yougile = FakeProvider([_raw("ID-1", 100)], board_type="yougile")
    youtrack = FakeProvider([_raw("TES-1", 200)], board_type="youtrack")
    ts, meta = FakeTaskService(), FakeMeta()
    result = SyncService([yougile, youtrack], ts, meta).run(board_type=None)
    assert result["enumerated"] == 2
    assert len(result["by_board"]) == 2
    types = {b["board_type"] for b in result["by_board"]}
    assert types == {"yougile", "youtrack"}



def test_by_board_includes_counts_per_provider():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)], board_type="yougile")
    meta = FakeMeta({("", "tasks:yougile:*"): "150"})  # ID-1 уже в курсоре
    ts = FakeTaskService()
    result = SyncService([prov], ts, meta).run()
    assert len(result["by_board"]) == 1
    entry = result["by_board"][0]
    assert entry["board_type"] == "yougile"
    assert entry["board"] == "*"
    assert entry["enumerated"] == 2
    assert entry["changed"] == 1
    assert entry["unchanged"] == 1


def test_scoped_purge_passes_project():
    a = FakeProvider([_raw("ID-1", 100)], board_type="yougile")
    ts, meta = FakeTaskService(), FakeMeta({("", "tasks:yougile:PRI"): "999"})
    SyncService([a], ts, meta).run(board="PRI", board_type="yougile",
                                   purge_orphaned=True)
    assert ts.purged_with == (["ID-1"], True, "PRI")


def test_unknown_board_type_warns_and_indexes_nothing():
    a = FakeProvider([_raw("ID-1", 100)], board_type="yougile")
    ts, meta = FakeTaskService(), FakeMeta()
    summary = SyncService([a], ts, meta).run(board_type="jira")
    assert summary["enumerated"] == 0
    assert any("jira" in w for w in summary["warnings"])


def test_sync_run_has_no_provider_specific_mutation_option():
    assert "status_field" not in inspect.signature(SyncService.run).parameters


def test_scoped_sync_redacts_normalize_exception_from_warning_and_log(caplog):
    secret = "scoped-sync-secret"

    class _FailingProvider(FakeProvider):
        def normalize(self, raw):
            raise RuntimeError(f"normalize rejected credential {secret}")

    provider = _FailingProvider([_raw("ID-1", 100)])
    service = SyncService(
        [SyncProvider(provider, frozenset({secret}))],
        FakeTaskService(),
        FakeMeta(),
    )

    with caplog.at_level(logging.WARNING):
        result = service.run(board_type="fake")

    assert result["changed"] == 0
    assert "[REDACTED]" in repr(result["warnings"])
    assert secret not in repr(result)
    assert secret not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_deploy_wide_sync_redacts_normalize_meta_exception_from_warning_and_log(caplog):
    secret = "deploy-sync-secret"

    class _FailingProvider(FakeProvider):
        def normalize_meta(self, raw):
            raise RuntimeError(f"normalize_meta rejected credential {secret}")

    failing = _FailingProvider([_raw("ID-1", 100)], board_type="first")
    healthy = FakeProvider([_raw("ID-2", 200)], board_type="second")
    meta = FakeMeta({("", "tasks:first:*"): "999"})
    service = SyncService(
        [
            SyncProvider(failing, frozenset({secret})),
            SyncProvider(healthy, frozenset({"other-secret"})),
        ],
        FakeTaskService(),
        meta,
    )

    with caplog.at_level(logging.WARNING):
        result = service.run()

    assert {entry["board_type"] for entry in result["by_board"]} == {
        "first",
        "second",
    }
    assert "[REDACTED]" in repr(result["warnings"])
    assert secret not in repr(result)
    assert secret not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_by_board_includes_meta_refreshed():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)], board_type="yougile")
    meta = FakeMeta({("", "tasks:yougile:*"): "150"})   # ID-1 ниже курсора
    ts = FakeTaskService()
    result = SyncService([prov], ts, meta).run()
    assert result["meta_refreshed"] == 1
    assert result["by_board"][0]["meta_refreshed"] == 1


def test_force_renormalize_reindexes_tasks_below_watermark():
    """Разовая перенормализация: задачи ниже курсора идут через полный normalize,
    а не через дешёвый meta-refresh (иначе смена нормализации не доедет до стора)."""
    from reviewer.tasks.sync import SyncService

    class _Provider:
        board_type = "yougile"

        def iter_raw(self, board, limit, *, sync_filter=None, now_ms=None):
            return TaskListing(rows=[_raw("ID-1", 10)])  # ниже курсора

        def normalize(self, raw):
            return {"key": raw.key, "description": "## Проблема\n\nчисто"}

        def normalize_meta(self, raw):
            return {"key": raw.key}

    class _Tasks:
        def __init__(self):
            self.indexed = []
            self.meta = []

        def index_batch(self, items):
            self.indexed.extend(items)
            return [{"key": i["key"], "embedded": True} for i in items]

        def refresh_meta_batch(self, items):
            self.meta.extend(items)
            return {"meta_refreshed": len(items), "warnings": []}

    class _Meta:
        def get_index_meta(self, repo, ref):
            return "100"                              # курсор выше timestamp задачи

        def set_index_meta(self, repo, ref, value):
            pass

    tasks = _Tasks()
    svc = SyncService([_Provider()], tasks, _Meta())

    out = svc.run(force_renormalize=True)
    assert out["changed"] == 1
    assert tasks.indexed and "чисто" in tasks.indexed[0]["description"]
    assert not tasks.meta                              # meta-refresh не вызывался

    tasks2 = _Tasks()
    svc2 = SyncService([_Provider()], tasks2, _Meta())
    svc2.run()                                         # обычный прогон — как раньше
    assert not tasks2.indexed
    assert tasks2.meta


class _CloseProvider:
    board_type = "fake"

    def __init__(self, name, events, error=None):
        self.name = name
        self.events = events
        self.error = error
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.events.append(self.name)
        if self.error is not None:
            raise self.error


def test_sync_service_close_is_idempotent_and_closes_only_owned_providers_once():
    events = []
    first = _CloseProvider("first", events)
    borrowed = _CloseProvider("borrowed", events)
    last = _CloseProvider("last", events)
    service = SyncService(
        [
            SyncProvider(first, owned=True),
            SyncProvider(first, owned=True),
            SyncProvider(borrowed),
            SyncProvider(last, owned=True),
        ],
        FakeTaskService(),
        FakeMeta(),
    )

    service.close()
    service.close()

    assert events == ["first", "last"]
    assert first.close_calls == 1
    assert borrowed.close_calls == 0
    assert last.close_calls == 1


def test_sync_service_close_attempts_all_owned_providers_and_raises_first_error():
    events = []
    first_error = RuntimeError("first close failed")
    first = _CloseProvider("first", events, first_error)
    second = _CloseProvider("second", events, RuntimeError("second close failed"))
    last = _CloseProvider("last", events)
    service = SyncService(
        [
            SyncProvider(first, owned=True),
            SyncProvider(second, owned=True),
            SyncProvider(last, owned=True),
        ],
        FakeTaskService(),
        FakeMeta(),
    )

    with pytest.raises(RuntimeError, match="first close failed") as captured:
        service.close()
    service.close()

    assert captured.value is first_error
    assert events == ["first", "second", "last"]
    assert [first.close_calls, second.close_calls, last.close_calls] == [1, 1, 1]


class _EmbedderDownTaskService(FakeTaskService):
    """Батч, где эмбеддер лёг: index_batch отдаёт класс структурно."""

    def index_batch(self, tasks):
        rows = super().index_batch(tasks)
        for row in rows:
            row.update({"embedded": False, "failure": "embedder",
                        "retry_required": True,
                        "warnings": ["embedder: APIError: HTTP code 403"]})
        return rows


def test_sync_aggregates_embedder_failure_flag():
    """Свод синка несёт булев признак, а не только текст в warnings."""
    provider = FakeProvider([_raw("ID-1", 100)])
    summary = SyncService([provider], _EmbedderDownTaskService(), FakeMeta()).run()

    assert summary["embedder_failed"] is True
    assert summary["by_board"][0]["embedder_failed"] is True


def test_sync_embedder_flag_false_without_embedder_failure():
    provider = FakeProvider([_raw("ID-1", 100)])
    summary = SyncService([provider], FakeTaskService(), FakeMeta()).run()

    assert summary["embedder_failed"] is False
    assert summary["by_board"][0]["embedder_failed"] is False
