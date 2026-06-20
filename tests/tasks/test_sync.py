from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.sync import SyncService


class FakeProvider:
    def __init__(self, raws):
        self._raws = raws

    def iter_raw(self, board, limit):
        n = 0
        for r in self._raws:
            yield r
            n += 1
            if limit and n >= limit:
                return

    def normalize(self, raw):
        return {"key": raw.key, "aliases": [raw.project_code], "title": raw.title,
                "description": raw.description, "criteria": [], "status": raw.status,
                "url": None, "links": []}


class FakeTaskService:
    def __init__(self):
        self.indexed = []
        self.purged_with = None

    def index_batch(self, tasks):
        self.indexed.append([t["key"] for t in tasks])
        return [{"key": t["key"], "embedded": True, "links_upserted": 0,
                 "prs_linked": 0, "warnings": []} for t in tasks]

    def purge_orphaned_tasks(self, active_keys, *, keep_with_prs=True):
        self.purged_with = (sorted(active_keys), keep_with_prs)
        return {"deleted_store": 1, "deleted_graph": 1, "protected_prs": 0,
                "warnings": []}


class FakeMeta:
    def __init__(self, val=None):
        self.store = {}
        if val is not None:
            self.store[("", "tasks:*")] = val

    def get_index_meta(self, repo, ref):
        return self.store.get((repo, ref))

    def set_index_meta(self, repo, ref, sha):
        self.store[(repo, ref)] = sha


def _raw(key, ts):
    return RawTask(key=key, project_code=key.replace("ID", "PRI"), title=key,
                   description="", status="S", subtask_ids=[], timestamp=ts)


def test_first_sync_indexes_all_and_advances_cursor():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts, meta = FakeTaskService(), FakeMeta()
    summary = SyncService(prov, ts, meta).run()
    assert ts.indexed == [["ID-1", "ID-2"]]
    assert summary["changed"] == 2 and summary["unchanged"] == 0
    assert summary["embedded"] == 2
    assert summary["cursor_advanced"] is True
    assert meta.store[("", "tasks:*")] == "200"


def test_watermark_skips_unchanged():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts, meta = FakeTaskService(), FakeMeta(val="150")
    summary = SyncService(prov, ts, meta).run()
    assert ts.indexed == [["ID-2"]]            # ID-1 (ts=100<=150) пропущена
    assert summary["changed"] == 1 and summary["unchanged"] == 1
    assert meta.store[("", "tasks:*")] == "200"


def test_no_changes_does_not_advance_cursor():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts, meta = FakeTaskService(), FakeMeta(val="200")
    summary = SyncService(prov, ts, meta).run()
    assert ts.indexed == []                    # index_batch не зван для пустого списка
    assert summary["changed"] == 0 and summary["unchanged"] == 2
    assert summary["cursor_advanced"] is False


def test_purge_uses_full_active_keys():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts, meta = FakeTaskService(), FakeMeta(val="999")  # обе unchanged
    summary = SyncService(prov, ts, meta).run(purge_orphaned=True, keep_with_prs=False)
    assert ts.purged_with == (["ID-1", "ID-2"], False)   # полный набор ключей
    assert summary["purge"]["deleted"] == 2


def test_limit_disables_purge_and_cursor():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    ts, meta = FakeTaskService(), FakeMeta()
    summary = SyncService(prov, ts, meta).run(limit=1, purge_orphaned=True)
    assert ts.purged_with is None                # purge выключен под limit
    assert summary["cursor_advanced"] is False
    assert ("", "tasks:*") not in meta.store     # курсор не записан
    assert any("limit" in w for w in summary["warnings"])


def test_board_scoped_cursor_ref():
    prov = FakeProvider([_raw("ID-1", 100)])
    ts, meta = FakeTaskService(), FakeMeta()
    SyncService(prov, ts, meta).run(board="MyBoard")
    assert ("", "tasks:MyBoard") in meta.store
