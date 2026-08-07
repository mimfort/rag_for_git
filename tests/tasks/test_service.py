from reviewer.tasks.graph import PRRef
from reviewer.tasks.service import TaskService
from reviewer.tasks.store import build_task_text, task_content_hash


class _FakeStore:
    def __init__(self, hashes=None, search_result=None, rows=None):
        self._hashes = dict(hashes or {})
        self.upserted = []
        self.meta_updates = []
        self.link_updates = []
        self.deleted = []
        self._search_result = search_result or []
        self._rows = list(rows or [])      # list[TaskRow] для get_task
        self.search_project = self.list_keys_project = self.get_task_project = "unset"

    def existing_hash(self, key):
        return self._hashes.get(key)

    def upsert_task(self, row):
        self.upserted.append(row)
        self._hashes[row.key] = row.content_hash

    def update_meta(self, key, title, status, url, aliases, project=""):
        self.meta_updates.append((key, title, status, url, aliases, project))

    def update_links(self, key, links):
        self.link_updates.append((key, links))
        return key in self._hashes

    def search(self, q, vec, top_k=5, project=None):
        self.search_project = project
        return self._search_result

    def list_keys(self, project=None):
        self.list_keys_project = project
        return list(self._hashes.keys())

    def delete_tasks(self, keys):
        count = 0
        for k in list(keys):
            if k in self._hashes:
                del self._hashes[k]
                count += 1
        self.deleted.extend(keys)
        return count

    def get_task(self, key, project=None):
        self.get_task_project = project
        for r in self._rows:
            if r.key == key or key in (r.aliases or []):
                return r
        return None


class _FakeGraph:
    def __init__(self, context=None, raise_on=(), pr_keys=(), keys=(), count=0):
        self.tasks = []
        self.links = []
        self.replaced_links = []
        self.pr_links = []
        self.deleted_tasks = []
        self._context = context or {}
        self._raise_on = set(raise_on)
        self._pr_keys = set(pr_keys)
        self._keys = set(keys)
        self._count = count
        self.task_context_project = "unset"
        self.list_keys_project = "unset"
        self.keys_with_prs_project = "unset"
        self.count_project = "unset"

    def upsert_task(self, key, aliases, title, status, url, project=""):
        if "upsert_task" in self._raise_on:
            raise RuntimeError("neo4j down")
        self.tasks.append((key, aliases, title, status, url, project))

    def upsert_links(self, key, links):
        self.links.append((key, links))
        return len(links)

    def replace_links(self, key, links):
        self.replaced_links.append((key, links))
        return len({(link["key"], link.get("type") or "relates")
                    for link in links if link.get("key")})

    def link_pr(self, task_key, pr, touched):
        self.pr_links.append((task_key, pr, touched))

    def task_context(self, key, project=""):
        if "task_context" in self._raise_on:
            raise RuntimeError("neo4j down")
        self.task_context_project = project
        return self._context

    def keys_with_prs(self, project=""):
        if "keys_with_prs" in self._raise_on:
            raise RuntimeError("neo4j down")
        self.keys_with_prs_project = project
        return set(self._pr_keys)

    def list_keys(self, project=""):
        if "list_keys" in self._raise_on:
            raise RuntimeError("neo4j down")
        self.list_keys_project = project
        return set(self._keys)

    def delete_tasks(self, keys):
        if "delete_tasks" in self._raise_on:
            raise RuntimeError("neo4j down")
        self.deleted_tasks.extend(keys)
        return len(list(keys))

    def count(self, project=""):
        if "count" in self._raise_on:
            raise RuntimeError("neo4j down")
        self.count_project = project
        return self._count


class _FakeEmbedder:
    def __init__(self):
        self.doc_calls = []

    def embed_documents(self, texts):
        self.doc_calls.append(texts)
        return [[0.1] * 8 for _ in texts]

    def embed_query(self, text):
        return [0.2] * 8


def _brief(**over):
    b = {"key": "ID-1", "aliases": ["PRI-1"], "title": "Add logout",
         "description": "Clear session", "criteria": ["redirects"],
         "status": "Open", "url": "u",
         "links": [{"key": "ID-2", "title": "child", "type": "subtask"}]}
    b.update(over)
    return b


def test_index_task_embeds_and_upserts_on_new_task():
    store, graph, emb = _FakeStore(), _FakeGraph(), _FakeEmbedder()
    out = TaskService(store, graph, emb).index_task(_brief())
    assert out["key"] == "ID-1"
    assert out["embedded"] is True
    assert out["links_upserted"] == 1
    assert store.upserted and store.upserted[0].key == "ID-1"
    assert emb.doc_calls  # embedding computed
    assert graph.tasks[0][0] == "ID-1"


def test_index_task_skips_embed_when_hash_unchanged():
    text = build_task_text("Add logout", "Clear session", ["redirects"])
    store = _FakeStore(hashes={"ID-1": task_content_hash(text)})
    graph, emb = _FakeGraph(), _FakeEmbedder()
    out = TaskService(store, graph, emb).index_task(_brief())
    assert out["embedded"] is False
    assert store.upserted == []           # no re-embed/upsert
    assert store.meta_updates and store.meta_updates[0][0] == "ID-1"  # meta refreshed


def test_index_task_unchanged_hash_still_replaces_links_without_embedding():
    text = build_task_text("Add logout", "Clear session", ["redirects"])
    links = [{"key": "ID-3", "title": "new child", "type": "subtask"}]
    store = _FakeStore(hashes={"ID-1": task_content_hash(text)})
    graph, emb = _FakeGraph(), _FakeEmbedder()

    out = TaskService(store, graph, emb).index_task(_brief(links=links))

    assert out["embedded"] is False
    assert emb.doc_calls == []
    assert store.link_updates == [("ID-1", links)]
    assert graph.replaced_links == [("ID-1", links)]
    assert out["links_stored"] is True
    assert out["links_upserted"] == 1


def test_index_task_explicit_empty_links_clears_store_and_graph():
    text = build_task_text("Add logout", "Clear session", ["redirects"])
    store = _FakeStore(hashes={"ID-1": task_content_hash(text)})
    graph = _FakeGraph()

    out = TaskService(store, graph, _FakeEmbedder()).index_task(_brief(links=[]))

    assert store.link_updates == [("ID-1", [])]
    assert graph.replaced_links == [("ID-1", [])]
    assert out["links_stored"] is True
    assert out["links_upserted"] == 0


def test_index_task_missing_links_preserves_store_and_graph():
    text = build_task_text("Add logout", "Clear session", ["redirects"])
    task = _brief()
    task.pop("links")
    store = _FakeStore(hashes={"ID-1": task_content_hash(text)})
    graph = _FakeGraph()

    out = TaskService(store, graph, _FakeEmbedder()).index_task(task)

    assert store.link_updates == []
    assert graph.replaced_links == []
    assert out["links_stored"] is None
    assert out["links_upserted"] == 0


def test_index_task_normalizes_links_once_for_store_and_graph():
    links = [
        {"key": "ID-2", "title": "child"},
        {"key": "ID-2", "title": "duplicate", "type": ""},
        {"key": "ID-2", "title": "subtask", "type": "subtask"},
        {"key": "ID-2", "title": "duplicate subtask", "type": "subtask"},
        {"title": "keyless"},
        {"key": ""},
        "not a link",
    ]
    expected = [
        {"key": "ID-2", "title": "child"},
        {"key": "ID-2", "title": "subtask", "type": "subtask"},
    ]
    store, graph = _FakeStore(), _FakeGraph()

    TaskService(store, graph, _FakeEmbedder()).index_task(_brief(links=links))

    assert store.upserted[0].links == expected
    assert graph.replaced_links == [("ID-1", expected)]


def test_index_task_warns_when_links_row_is_missing_but_still_updates_graph():
    text = build_task_text("Add logout", "Clear session", ["redirects"])

    class _MissingRowStore(_FakeStore):
        def update_links(self, key, links):
            self.link_updates.append((key, links))
            return False

    links = [{"key": "ID-2"}]
    store = _MissingRowStore(hashes={"ID-1": task_content_hash(text)})
    graph = _FakeGraph()

    out = TaskService(store, graph, _FakeEmbedder()).index_task(_brief(links=links))

    assert out["links_stored"] is False
    assert any("store links" in warning and "not found" in warning
               for warning in out["warnings"])
    assert graph.replaced_links == [("ID-1", links)]


def test_index_task_graph_none_still_embeds_and_warns():
    store, emb = _FakeStore(), _FakeEmbedder()
    out = TaskService(store, None, emb).index_task(_brief())
    assert out["embedded"] is True
    assert any("graph unavailable" in w for w in out["warnings"])


def test_index_task_graph_error_is_warning_not_raise():
    store, emb = _FakeStore(), _FakeEmbedder()
    graph = _FakeGraph(raise_on=("upsert_task",))
    out = TaskService(store, graph, emb).index_task(_brief())
    assert out["embedded"] is True       # store layer succeeded
    assert any("graph:" in w for w in out["warnings"])


def test_index_task_store_error_is_warning_not_raise():
    class _BrokenStore(_FakeStore):
        def existing_hash(self, key):
            raise RuntimeError("pg down")
    out = TaskService(_BrokenStore(), _FakeGraph(), _FakeEmbedder()).index_task(_brief())
    assert out["embedded"] is False
    assert any("store:" in w for w in out["warnings"])
    # graph layer still runs despite the store failure


def test_index_task_no_key():
    out = TaskService(_FakeStore(), _FakeGraph(), _FakeEmbedder()).index_task({"title": "x"})
    assert out["key"] is None
    assert out["embedded"] is False


def test_search_tasks_formats_hits():
    from reviewer.tasks.store import TaskHit
    store = _FakeStore(search_result=[TaskHit("ID-1", "Add logout", "Open", 0.83)])
    out = TaskService(store, _FakeGraph(), _FakeEmbedder()).search_tasks("logout")
    assert "ID-1" in out and "Add logout" in out and "Open" in out


def test_search_tasks_empty():
    out = TaskService(_FakeStore(), _FakeGraph(), _FakeEmbedder()).search_tasks("x")
    assert out == "(no similar tasks found)"


def test_search_tasks_shows_rank_and_precise_score():
    # RRF-скоры лежат в ≈0.016–0.033 — при грубой точности (.2f) близкие задачи
    # схлопываются в одно число и прунить по score нельзя. Выдача должна показывать
    # ранг-ординал (стабильный сигнал для relevance-фильтра) и различимый score.
    from reviewer.tasks.store import TaskHit
    store = _FakeStore(search_result=[
        TaskHit("ID-1", "First", "Open", 0.0328),
        TaskHit("ID-2", "Second", "Done", 0.0164),
    ])
    out = TaskService(store, _FakeGraph(), _FakeEmbedder()).search_tasks("q")
    lines = out.splitlines()
    assert lines[0].startswith("1. ") and "ID-1" in lines[0]
    assert lines[1].startswith("2. ") and "ID-2" in lines[1]
    # близкие RRF-скоры различимы (не оба «0.03»)
    assert "0.0328" in out and "0.0164" in out


def test_get_task_context_graph_none():
    out = TaskService(_FakeStore(), None, _FakeEmbedder()).get_task_context("ID-1")
    assert out == "(task graph unavailable)"


def test_get_task_context_not_found():
    out = TaskService(_FakeStore(), _FakeGraph(context={}), _FakeEmbedder()).get_task_context("ZZ-9")
    assert "ZZ-9" in out


def test_get_task_context_formats():
    ctx = {"key": "ID-1", "title": "Add logout", "status": "Open", "url": "u",
           "prs": [{"id": "o/r#7", "url": "pr", "sha": "abc", "touched": ["a.py#foo"]}],
           "linked": [{"key": "ID-2", "title": "child", "status": "Done",
                       "type": "subtask", "prs": [{"id": "o/r#8", "url": "pr8"}]}]}
    out = TaskService(_FakeStore(), _FakeGraph(context=ctx), _FakeEmbedder()).get_task_context("ID-1")
    assert "ID-1" in out and "o/r#7" in out and "a.py#foo" in out
    assert "subtask" in out and "ID-2" in out


def test_format_task_context_truncates_on_block_boundary():
    # Усечение не должно резать середину строки/блока: выбрасываются целые
    # элементы (PR/связанная задача) с хвоста, заголовок секции не осиротеет.
    from reviewer.tasks.service import _format_task_context
    ctx = {"key": "ID-1", "title": "T", "status": "Open",
           "prs": [{"id": "o/r#1", "touched": ["a.py#foo"]},
                   {"id": "o/r#2", "touched": ["b.py#bar"]},
                   {"id": "o/r#3", "touched": ["c.py#baz"]}]}
    full = _format_task_context(ctx, 100000)
    valid_lines = set(full.splitlines())
    # бюджет режет ВНУТРИ строки второго PR — место, где наивный truncate оборвал
    # бы строку посередине; блочное усечение должно выбросить весь блок PR2.
    budget = full.index("b.py#bar")
    out = _format_task_context(ctx, budget)
    lines = out.splitlines()
    assert lines[-1].startswith("… (truncated")  # нота на отдельной строке
    for ln in lines[:-1]:                         # ни одной оборванной строки
        assert ln in valid_lines
    assert "o/r#1" in out                         # первый PR сохранён целиком
    assert "b.py#bar" not in out                  # оборванный хвост не просочился


def test_format_task_context_keeps_head_when_over_budget():
    # Голова (ключ/статус/заголовок) обязательна и не режется посреди строки,
    # даже если одна она превышает бюджет.
    from reviewer.tasks.service import _format_task_context
    ctx = {"key": "ID-1", "title": "Очень длинный заголовок " * 5,
           "prs": [{"id": "o/r#1"}]}
    out = _format_task_context(ctx, 5)
    assert out.splitlines()[0].startswith("Task ID-1")  # голова цела
    assert "o/r#1" not in out
    assert "truncated" in out


def test_link_review_calls_graph():
    graph = _FakeGraph()
    pr = PRRef(repo="o/r", number=7, url="https://github.com/o/r/pull/7", sha="abc")
    TaskService(_FakeStore(), graph, _FakeEmbedder()).link_review("ID-1", pr, ["a.py#foo"])
    assert graph.pr_links == [("ID-1", pr, ["a.py#foo"])]


def test_link_review_noop_without_graph_or_key():
    pr = PRRef(repo="o/r", number=7, url="u", sha="abc")
    TaskService(_FakeStore(), None, _FakeEmbedder()).link_review("ID-1", pr, [])  # no graph
    g = _FakeGraph()
    TaskService(_FakeStore(), g, _FakeEmbedder()).link_review("", pr, [])         # no key
    assert g.pr_links == []


def test_purge_deletes_orphaned():
    store = _FakeStore(hashes={"ID-1": "h1", "ID-2": "h2"})
    graph = _FakeGraph()
    result = TaskService(store, graph, _FakeEmbedder()).purge_orphaned_tasks(["ID-1"])
    assert result["deleted_store"] == 1
    assert result["deleted_graph"] == 1
    assert "ID-2" in store.deleted
    assert "ID-1" not in store.deleted


def test_purge_deletes_graph_only_stub():
    # Стаб :Task, созданный upsert_links (link-only, без стора и без PR), должен
    # удаляться из графа, даже если его нет в Postgres-сторе. Это и есть баг:
    # вселенная ключей бралась только из стора, и стабы оставались навсегда.
    store = _FakeStore(hashes={"ID-1": "h1"})
    graph = _FakeGraph(keys={"ID-1", "PRI-42"})
    result = TaskService(store, graph, _FakeEmbedder()).purge_orphaned_tasks(["ID-1"])
    assert "PRI-42" in graph.deleted_tasks
    assert result["deleted_graph"] == 1
    assert result["deleted_store"] == 0   # стаба в сторе не было — нечего удалять


def test_purge_graph_only_stub_protected_by_pr():
    # Стаб с PR-историей (link_pr) защищён keep_with_prs, как и обычные задачи.
    store = _FakeStore(hashes={"ID-1": "h1"})
    graph = _FakeGraph(keys={"ID-1", "PRI-42"}, pr_keys={"PRI-42"})
    result = TaskService(store, graph, _FakeEmbedder()).purge_orphaned_tasks(["ID-1"])
    assert "PRI-42" not in graph.deleted_tasks
    assert result["protected_prs"] == 1


def test_purge_keeps_tasks_with_prs():
    store = _FakeStore(hashes={"ID-1": "h1", "ID-2": "h2"})
    graph = _FakeGraph(pr_keys={"ID-2"})
    result = TaskService(store, graph, _FakeEmbedder()).purge_orphaned_tasks([])
    assert result["deleted_store"] == 1
    assert result["protected_prs"] == 1
    assert "ID-2" not in store.deleted
    assert "ID-1" in store.deleted


def test_purge_no_keep_with_prs():
    store = _FakeStore(hashes={"ID-1": "h1", "ID-2": "h2"})
    graph = _FakeGraph(pr_keys={"ID-2"})
    result = TaskService(store, graph, _FakeEmbedder()).purge_orphaned_tasks(
        [], keep_with_prs=False
    )
    assert result["deleted_store"] == 2
    assert "ID-1" in store.deleted and "ID-2" in store.deleted


def test_purge_all_active_no_delete():
    store = _FakeStore(hashes={"ID-1": "h1"})
    result = TaskService(store, _FakeGraph(), _FakeEmbedder()).purge_orphaned_tasks(["ID-1"])
    assert result["deleted_store"] == 0
    assert result["deleted_graph"] == 0


def test_purge_empty_active_keys_deletes_all():
    store = _FakeStore(hashes={"ID-1": "h1", "ID-2": "h2"})
    result = TaskService(store, _FakeGraph(), _FakeEmbedder()).purge_orphaned_tasks([])
    assert result["deleted_store"] == 2


def test_purge_store_list_keys_error_returns_warning():
    class _BrokenStore(_FakeStore):
        def list_keys(self):
            raise RuntimeError("pg down")

    result = TaskService(
        _BrokenStore(hashes={"ID-1": "h1"}), _FakeGraph(), _FakeEmbedder()
    ).purge_orphaned_tasks([])
    assert result["deleted_store"] == 0
    assert any("store:" in w for w in result["warnings"])


def test_purge_graph_keys_with_prs_error_continues_without_protection():
    store = _FakeStore(hashes={"ID-1": "h1", "ID-2": "h2"})
    graph = _FakeGraph(raise_on=("keys_with_prs",))
    result = TaskService(store, graph, _FakeEmbedder()).purge_orphaned_tasks([])
    assert any("graph:" in w for w in result["warnings"])
    assert result["deleted_store"] == 2


def test_purge_graph_delete_error_is_warning_store_cleaned():
    store = _FakeStore(hashes={"ID-1": "h1"})
    graph = _FakeGraph(raise_on=("delete_tasks",))
    result = TaskService(store, graph, _FakeEmbedder()).purge_orphaned_tasks([])
    assert result["deleted_store"] == 1
    assert result["deleted_graph"] == 0
    assert any("graph:" in w for w in result["warnings"])


def test_purge_graph_none_works_store_only():
    store = _FakeStore(hashes={"ID-1": "h1"})
    result = TaskService(store, None, _FakeEmbedder()).purge_orphaned_tasks([])
    assert result["deleted_store"] == 1
    assert result["deleted_graph"] == 0
    assert result["warnings"] == []


def test_index_task_links_prs_when_embedded():
    graph = _FakeGraph()
    task = {"key": "ID-1", "title": "T",
            "description": "https://github.com/o/r/pull/7 и https://github.com/o/r/pull/8"}
    res = TaskService(_FakeStore(), graph, _FakeEmbedder()).index_task(task)
    assert res["embedded"] is True
    assert res["prs_linked"] == 2
    assert [tk for tk, _, _ in graph.pr_links] == ["ID-1", "ID-1"]
    assert all(touched == [] for _, _, touched in graph.pr_links)


def test_index_task_no_pr_link_when_unchanged():
    text = build_task_text("T", "https://github.com/o/r/pull/7", [])
    store = _FakeStore(hashes={"ID-1": task_content_hash(text)})
    graph = _FakeGraph()
    task = {"key": "ID-1", "title": "T", "description": "https://github.com/o/r/pull/7"}
    res = TaskService(store, graph, _FakeEmbedder()).index_task(task)
    assert res["embedded"] is False
    assert res["prs_linked"] == 0
    assert graph.pr_links == []


def test_index_task_pr_link_noop_without_graph():
    task = {"key": "ID-1", "title": "T",
            "description": "https://github.com/o/r/pull/7"}
    res = TaskService(_FakeStore(), None, _FakeEmbedder()).index_task(task)
    assert res["embedded"] is True
    assert res["prs_linked"] == 0


def test_get_task_hit_returns_normalized_brief():
    from reviewer.tasks.store import TaskRow
    row = TaskRow(key="ID-1", aliases=["PRI-1"], title="Add logout",
                  description="Clear session", status="Open", url="u",
                  content_hash="h", text="t", embedding=[],
                  links=[{"key": "ID-2", "type": "subtask"}])
    svc = TaskService(_FakeStore(rows=[row]), _FakeGraph(), _FakeEmbedder())
    out = svc.get_task("ID-1")
    assert out == {"key": "ID-1", "aliases": ["PRI-1"], "title": "Add logout",
                   "description": "Clear session", "criteria": [],
                   "status": "Open", "url": "u", "attachments": [],
                   "links": [{"key": "ID-2", "type": "subtask"}]}


def test_get_task_resolves_by_alias():
    from reviewer.tasks.store import TaskRow
    row = TaskRow(key="ID-1", aliases=["PRI-1"], title="T", description="d",
                  status=None, url=None, content_hash="h", text="t", embedding=[])
    out = TaskService(_FakeStore(rows=[row]), _FakeGraph(), _FakeEmbedder()).get_task("PRI-1")
    assert out is not None and out["key"] == "ID-1"


def test_get_task_miss_returns_none():
    out = TaskService(_FakeStore(rows=[]), _FakeGraph(), _FakeEmbedder()).get_task("ZZ-9")
    assert out is None


def test_get_task_store_error_returns_none_not_raise():
    class _BrokenStore(_FakeStore):
        def get_task(self, key, project=None):
            raise RuntimeError("pg down")
    out = TaskService(_BrokenStore(), _FakeGraph(), _FakeEmbedder()).get_task("ID-1")
    assert out is None


def test_search_tasks_threads_project():
    from reviewer.tasks.store import TaskHit
    store = _FakeStore(search_result=[TaskHit(key="ID-1", title="t", status="Open", score=0.1)])
    svc = TaskService(store, _FakeGraph(), _FakeEmbedder())
    svc.search_tasks("q", project="PRI")
    assert store.search_project == "PRI"


def test_get_task_context_threads_project():
    g = _FakeGraph(context={"key": "ID-1", "title": "t", "status": None,
                            "url": None, "prs": [], "linked": []})
    svc = TaskService(_FakeStore(), g, _FakeEmbedder())
    svc.get_task_context("ID-1", project="PRI")
    assert g.task_context_project == "PRI"


def test_get_task_threads_project():
    from reviewer.tasks.store import TaskRow
    row = TaskRow(key="ID-1", aliases=[], title="t", description="d", status=None,
                  url=None, content_hash="h", text="t", embedding=[], project="PRI")
    store = _FakeStore(rows=[row])
    svc = TaskService(store, _FakeGraph(), _FakeEmbedder())
    svc.get_task("ID-1", project="PRI")
    assert store.get_task_project == "PRI"


def test_index_task_stamps_project_in_store_and_graph():
    store, graph, emb = _FakeStore(), _FakeGraph(), _FakeEmbedder()
    TaskService(store, graph, emb).index_task(_brief(project="PRI"))
    assert store.upserted[0].project == "PRI"
    assert graph.tasks[0][-1] == "PRI"


def test_purge_threads_project_to_store_and_graph():
    store = _FakeStore(hashes={"ID-1": "h"})
    g = _FakeGraph(keys={"ID-1"}, pr_keys=set())
    svc = TaskService(store, g, _FakeEmbedder())
    svc.purge_orphaned_tasks(["ID-1"], project="PRI")
    assert store.list_keys_project == "PRI"
    assert g.list_keys_project == "PRI"
    assert g.keys_with_prs_project == "PRI"


def test_index_task_passes_attachments_to_row():
    """Поле attachments из брифа прокидывается в TaskRow при upsert."""
    from reviewer.tasks.store import TaskRow  # noqa: F401
    store, graph, emb = _FakeStore(), _FakeGraph(), _FakeEmbedder()
    attachments = [{"name": "spec.md", "mime_type": "text/markdown",
                    "size": 4, "content_text": "spec"}]
    task = {"key": "ID-1", "title": "t", "description": "d",
            "attachments": attachments}
    TaskService(store, graph, emb).index_task(task)
    assert store.upserted[-1].attachments == attachments


def test_get_task_returns_attachments():
    """get_task возвращает dict с ключом attachments из сохранённой строки."""
    from reviewer.tasks.store import TaskRow
    atts = [{"name": "a.txt", "content_text": "x"}]
    row = TaskRow(key="ID-2", aliases=[], title="t", description="d",
                  status=None, url=None, content_hash="h", text="t",
                  embedding=[], attachments=atts)
    svc = TaskService(_FakeStore(rows=[row]), _FakeGraph(), _FakeEmbedder())
    out = svc.get_task("ID-2")
    assert out["attachments"] == atts


def test_count_tasks_delegates_scoped():
    store, graph, emb = _FakeStore(), _FakeGraph(count=7), _FakeEmbedder()
    assert TaskService(store, graph, emb).count_tasks("PRI") == 7
    assert graph.count_project == "PRI"


def test_count_tasks_zero_when_no_graph():
    store, emb = _FakeStore(), _FakeEmbedder()
    assert TaskService(store, None, emb).count_tasks("PRI") == 0


def test_count_tasks_fail_soft_on_graph_error():
    store, graph, emb = _FakeStore(), _FakeGraph(raise_on=("count",)), _FakeEmbedder()
    assert TaskService(store, graph, emb).count_tasks() == 0
