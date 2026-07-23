from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.sync import SyncService


class FakeProvider:
    board_type = "fake"

    def __init__(self, raws, board_type="fake"):
        self._raws = raws
        self.board_type = board_type

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

    def normalize_meta(self, raw):
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
                 "prs_linked": 0, "warnings": []} for t in tasks]

    def refresh_meta_batch(self, metas):
        self.meta_refreshed.append([m["key"] for m in metas])
        return {"meta_refreshed": len(metas), "warnings": []}

    def purge_orphaned_tasks(self, active_keys, *, keep_with_prs=True, project=None):
        self.purged_with = (sorted(active_keys), keep_with_prs, project)
        return {"deleted_store": 1, "deleted_graph": 1, "protected_prs": 0,
                "warnings": []}


class FakeMeta:
    def __init__(self, init=None):
        self.store = dict(init or {})

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


def test_sync_run_resets_youtrack_status_field():
    class _YT:
        board_type = "youtrack"

        def __init__(self):
            self._status_field = "State"

        def set_status_field(self, f):
            self._status_field = f or "State"

        def iter_raw(self, board, limit):
            return iter([])

    yt = _YT()
    meta = type("M", (), {"get_index_meta": lambda *a: None,
                          "set_index_meta": lambda *a: None})()
    tasks = type("T", (), {"index_batch": staticmethod(lambda x: [])})()
    svc = SyncService([yt], tasks, meta)
    svc.run(board_type="youtrack", status_field="Stage")
    assert yt._status_field == "Stage"
    svc.run(board_type="youtrack")           # без status_field → сброс к State
    assert yt._status_field == "State"


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

        def iter_raw(self, board, limit):
            yield _raw("ID-1", 10)                    # ниже курсора

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
