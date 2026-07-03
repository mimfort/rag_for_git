"""Unit-тесты для TaskService.index_batch."""
from reviewer.tasks.service import TaskService
from reviewer.tasks.store import build_task_text, task_content_hash


class _FakeStore:
    def __init__(self, hashes=None):
        self._hashes = hashes or {}
        self.upserted = []
        self.meta_updates = []
        self.meta_batch = None

    def existing_hash(self, key):
        return self._hashes.get(key)

    def upsert_task(self, row):
        self.upserted.append(row)

    def update_meta(self, key, title, status, url, aliases, project=""):
        self.meta_updates.append((key, title, status, url, aliases, project))

    def update_meta_batch(self, metas):
        self.meta_batch = list(metas)


class _FakeGraph:
    def __init__(self, raise_on=()):
        self.tasks = []
        self.task_projects: list[str] = []
        self.links = []
        self.pr_links = []
        self.pr_batch_links: list[tuple[str, object]] = []
        self._raise_on = set(raise_on)

    def upsert_task(self, key, aliases, title, status, url, project=""):
        if "upsert_task" in self._raise_on:
            raise RuntimeError("neo4j down")
        self.tasks.append(key)
        self.task_projects.append(project)

    def upsert_links(self, key, links):
        self.links.append((key, links))
        return len(links)

    def link_pr(self, task_key, pr, touched):
        self.pr_links.append((task_key, pr, touched))

    def link_prs_batch(self, pairs):
        self.pr_batch_links.extend(pairs)


class _FakeEmbedder:
    def __init__(self):
        self.doc_calls = []

    def embed_documents(self, texts):
        self.doc_calls.append(list(texts))
        return [[0.1] * 8 for _ in texts]


def _brief(key="ID-1", alias="PRI-1", **over):
    b = {"key": key, "aliases": [alias], "title": "Add logout",
         "description": "Clear session", "criteria": ["redirects"],
         "status": "Open", "url": "http://example.com",
         "links": [{"key": "ID-2", "title": "child", "type": "subtask"}]}
    b.update(over)
    return b


def test_index_batch_empty_returns_empty():
    svc = TaskService(_FakeStore(), _FakeGraph(), _FakeEmbedder())
    assert svc.index_batch([]) == []


def test_index_batch_single_embed_call_for_new_tasks():
    """N новых задач → ровно один вызов embed_documents со всеми текстами."""
    store, graph, emb = _FakeStore(), _FakeGraph(), _FakeEmbedder()
    tasks = [_brief("ID-1", "PRI-1"), _brief("ID-2", "PRI-2", title="Fix bug",
                                              description="desc", links=[])]
    results = TaskService(store, graph, emb).index_batch(tasks)

    assert len(results) == 2
    assert all(r["embedded"] is True for r in results)
    assert len(emb.doc_calls) == 1          # ровно один Voyage-вызов
    assert len(emb.doc_calls[0]) == 2       # оба текста в одном вызове
    assert len(store.upserted) == 2


def test_index_batch_no_embed_when_all_unchanged():
    """Все задачи без изменений → embed_documents не вызывается."""
    t1 = build_task_text("Add logout", "Clear session", ["redirects"])
    t2 = build_task_text("Fix bug", "desc", ["redirects"])
    store = _FakeStore(hashes={
        "ID-1": task_content_hash(t1),
        "ID-2": task_content_hash(t2),
    })
    emb = _FakeEmbedder()
    tasks = [_brief("ID-1", "PRI-1"), _brief("ID-2", "PRI-2", title="Fix bug",
                                              description="desc", links=[])]
    results = TaskService(store, _FakeGraph(), emb).index_batch(tasks)

    assert all(r["embedded"] is False for r in results)
    assert emb.doc_calls == []              # нет Voyage-вызовов
    assert len(store.meta_updates) == 2


def test_index_batch_embeds_only_changed():
    """Одна задача изменилась, одна нет → embed_documents с одним текстом."""
    t1 = build_task_text("Add logout", "Clear session", ["redirects"])
    store = _FakeStore(hashes={"ID-1": task_content_hash(t1)})
    emb = _FakeEmbedder()
    tasks = [_brief("ID-1", "PRI-1"),
             _brief("ID-2", "PRI-2", title="Fix bug", description="desc", links=[])]
    results = TaskService(store, _FakeGraph(), emb).index_batch(tasks)

    assert results[0]["embedded"] is False   # без изменений
    assert results[1]["embedded"] is True    # новая
    assert len(emb.doc_calls) == 1
    assert len(emb.doc_calls[0]) == 1        # только один текст


def test_index_batch_task_no_key_gets_warning_others_continue():
    """Задача без key → warning в результате; остальные продолжают."""
    store, graph, emb = _FakeStore(), _FakeGraph(), _FakeEmbedder()
    tasks = [{"title": "no key"}, _brief("ID-2", "PRI-2", title="Fix bug",
                                          description="desc", links=[])]
    results = TaskService(store, graph, emb).index_batch(tasks)

    assert results[0]["key"] is None
    assert any("has no key" in w for w in results[0]["warnings"])
    assert results[1]["embedded"] is True    # вторая задача обработана


def test_index_batch_embed_error_marks_changed_warns_but_meta_only_ok():
    """Сбой embed_documents → changed-задачи получают warning; unchanged — update_meta."""
    t1 = build_task_text("Add logout", "Clear session", ["redirects"])
    store = _FakeStore(hashes={"ID-1": task_content_hash(t1)})

    class _BrokenEmbedder(_FakeEmbedder):
        def embed_documents(self, texts):
            super().embed_documents(texts)
            raise RuntimeError("voyage down")

    tasks = [_brief("ID-1", "PRI-1"),
             _brief("ID-2", "PRI-2", title="Fix bug", description="desc", links=[])]
    results = TaskService(store, _FakeGraph(), _BrokenEmbedder()).index_batch(tasks)

    assert results[0]["embedded"] is False   # unchanged → update_meta, не затронута
    assert results[0]["warnings"] == []      # нет ошибки у unchanged
    assert store.meta_updates                # meta обновлена
    assert results[1]["embedded"] is False
    assert any("embedder:" in w for w in results[1]["warnings"])


def test_index_batch_store_error_one_task_others_continue():
    """Сбой upsert_task для одной задачи → warning только у неё."""
    call_count = {"n": 0}

    class _PartiallyBrokenStore(_FakeStore):
        def upsert_task(self, row):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("pg write error")
            super().upsert_task(row)

    store = _PartiallyBrokenStore()
    emb = _FakeEmbedder()
    tasks = [_brief("ID-1", "PRI-1"),
             _brief("ID-2", "PRI-2", title="Fix bug", description="desc", links=[])]
    results = TaskService(store, _FakeGraph(), emb).index_batch(tasks)

    assert any("store:" in w for w in results[0]["warnings"])
    assert results[1]["embedded"] is True


def test_index_batch_graph_none_adds_warning():
    """graph=None → warning для каждой задачи, store-слой работает."""
    store, emb = _FakeStore(), _FakeEmbedder()
    results = TaskService(store, None, emb).index_batch([_brief()])

    assert results[0]["embedded"] is True
    assert any("graph unavailable" in w for w in results[0]["warnings"])


def test_index_batch_result_order_matches_input():
    """Порядок результатов совпадает с порядком входного списка."""
    store, emb = _FakeStore(), _FakeEmbedder()
    keys = ["ID-10", "ID-20", "ID-30"]
    tasks = [_brief(k, k.replace("ID-", "PRI-"), title=f"T{k}",
                    description="d", links=[]) for k in keys]
    results = TaskService(store, _FakeGraph(), emb).index_batch(tasks)

    assert [r["key"] for r in results] == keys


def test_index_batch_links_prs_for_embedded_only():
    """embedded=True → PR из description линкуются; embedded=False (без изменений) → нет."""
    unchanged = build_task_text("T2", "https://github.com/o/r/pull/9", [])
    store = _FakeStore(hashes={"ID-2": task_content_hash(unchanged)})
    graph, emb = _FakeGraph(), _FakeEmbedder()
    tasks = [
        {"key": "ID-1", "title": "T1", "description": "https://github.com/o/r/pull/7"},
        {"key": "ID-2", "title": "T2", "description": "https://github.com/o/r/pull/9"},
    ]
    results = TaskService(store, graph, emb).index_batch(tasks)

    assert results[0]["embedded"] is True and results[0]["prs_linked"] == 1
    assert results[1]["embedded"] is False and results[1]["prs_linked"] == 0
    assert [tk for tk, _ in graph.pr_batch_links] == ["ID-1"]


def test_index_batch_stamps_project_on_meta_only():
    text = build_task_text("Add logout", "Clear session", ["redirects"])
    store = _FakeStore(hashes={"ID-1": task_content_hash(text)})  # хэш совпал → meta_only
    g = _FakeGraph()
    TaskService(store, g, _FakeEmbedder()).index_batch([_brief(project="PRI")])
    assert store.meta_updates[0][-1] == "PRI"     # project прокинут в update_meta
    assert g.tasks == ["ID-1"]
    assert g.task_projects[0] == "PRI"            # project достиг граф-write-path


def test_index_batch_passes_attachments_to_row():
    """Поле attachments из брифа прокидывается в TaskRow при батчевом upsert."""
    store, graph, emb = _FakeStore(), _FakeGraph(), _FakeEmbedder()
    attachments = [{"name": "spec.md", "content_text": "spec"}]
    task = _brief(attachments=attachments)
    TaskService(store, graph, emb).index_batch([task])
    assert store.upserted[-1].attachments == attachments


def test_refresh_meta_batch_stamps_project_store_and_graph():
    store, graph = _FakeStore(), _FakeGraph()
    metas = [{"key": "ID-1", "aliases": ["PRI-1"], "title": "T", "status": "Open",
              "url": "u", "project": "PRI"}]
    res = TaskService(store, graph, _FakeEmbedder()).refresh_meta_batch(metas)
    assert res["meta_refreshed"] == 1
    assert store.meta_batch == metas               # батч ушёл в стор
    assert graph.tasks == ["ID-1"]
    assert graph.task_projects == ["PRI"]          # project достиг графа


def test_refresh_meta_batch_never_embeds_or_upserts():
    store, graph, emb = _FakeStore(), _FakeGraph(), _FakeEmbedder()
    TaskService(store, graph, emb).refresh_meta_batch([{"key": "ID-1", "project": "PRI"}])
    assert emb.doc_calls == []                     # НИКОГДА не эмбедит
    assert store.upserted == []                    # и не upsert-ит (не воскрешает задачу)


def test_refresh_meta_batch_empty():
    store = _FakeStore()
    res = TaskService(store, _FakeGraph(), _FakeEmbedder()).refresh_meta_batch([])
    assert res == {"meta_refreshed": 0, "warnings": []}
    assert store.meta_batch is None


def test_refresh_meta_batch_graph_none_warns():
    store = _FakeStore()
    res = TaskService(store, None, _FakeEmbedder()).refresh_meta_batch(
        [{"key": "ID-1", "project": "PRI"}])
    assert res["meta_refreshed"] == 1
    assert any("graph unavailable" in w for w in res["warnings"])


def test_refresh_meta_batch_graph_failsoft():
    store, graph = _FakeStore(), _FakeGraph(raise_on=("upsert_task",))
    res = TaskService(store, graph, _FakeEmbedder()).refresh_meta_batch(
        [{"key": "ID-1", "project": "PRI"}])
    assert res["meta_refreshed"] == 1              # store прошёл, граф — fail-soft
    assert any("graph" in w for w in res["warnings"])
