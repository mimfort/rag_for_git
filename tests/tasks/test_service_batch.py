"""Unit-тесты для TaskService.index_batch."""
import psycopg_pool

from reviewer.tasks.service import TaskService
from reviewer.tasks.store import build_task_text, task_content_hash


class _FakeStore:
    def __init__(self, hashes=None):
        self._hashes = hashes or {}
        self.upserted = []
        self.meta_updates = []
        self.meta_batch = None
        self.link_updates = []

    def existing_hash(self, key):
        return self._hashes.get(key)

    def upsert_task(self, row):
        self.upserted.append(row)
        self._hashes[row.key] = row.content_hash

    def update_meta(self, key, title, status, url, aliases, project=""):
        self.meta_updates.append((key, title, status, url, aliases, project))

    def update_meta_batch(self, metas):
        self.meta_batch = list(metas)

    def update_links(self, key, links):
        self.link_updates.append((key, links))
        return key in self._hashes


class _FakeGraph:
    def __init__(self, raise_on=()):
        self.tasks = []
        self.task_projects: list[str] = []
        self.links = []
        self.replaced_links = []
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

    def replace_links(self, key, links):
        self.replaced_links.append((key, links))
        return len({(link["key"], link.get("type") or "relates")
                    for link in links if link.get("key")})

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
    assert results[0]["retry_required"] is False
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
    assert results[1]["retry_required"] is True


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
    assert results[0]["retry_required"] is True
    assert results[1]["embedded"] is True


def test_index_batch_graph_none_adds_warning():
    """graph=None → warning для каждой задачи, store-слой работает."""
    store, emb = _FakeStore(), _FakeEmbedder()
    results = TaskService(store, None, emb).index_batch([_brief()])

    assert results[0]["embedded"] is True
    assert any("graph unavailable" in w for w in results[0]["warnings"])
    assert results[0]["retry_required"] is False


def test_index_batch_existing_hash_error_requires_retry():
    class _BrokenStore(_FakeStore):
        def existing_hash(self, key):
            raise RuntimeError("pg lookup error")

    result = TaskService(
        _BrokenStore(), _FakeGraph(), _FakeEmbedder()
    ).index_batch([_brief()])[0]

    assert any("store:" in warning for warning in result["warnings"])
    assert result["retry_required"] is True


def test_index_batch_update_meta_error_requires_retry():
    text = build_task_text("Add logout", "Clear session", ["redirects"])

    class _BrokenStore(_FakeStore):
        def update_meta(self, key, title, status, url, aliases, project=""):
            raise RuntimeError("pg metadata error")

    result = TaskService(
        _BrokenStore(hashes={"ID-1": task_content_hash(text)}),
        _FakeGraph(),
        _FakeEmbedder(),
    ).index_batch([_brief()])[0]

    assert any("store:" in warning for warning in result["warnings"])
    assert result["retry_required"] is True


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


def test_index_batch_meta_only_replaces_links_without_embedding():
    text = build_task_text("Add logout", "Clear session", ["redirects"])
    links = [{"key": "ID-3", "title": "new child", "type": "subtask"}]
    store = _FakeStore(hashes={"ID-1": task_content_hash(text)})
    graph, emb = _FakeGraph(), _FakeEmbedder()

    result = TaskService(store, graph, emb).index_batch([_brief(links=links)])[0]

    assert result["embedded"] is False
    assert emb.doc_calls == []
    assert store.link_updates == [("ID-1", links)]
    assert graph.replaced_links == [("ID-1", links)]
    assert result["links_stored"] is True
    assert result["links_upserted"] == 1


def test_index_batch_normalizes_links_once_for_store_and_graph():
    text = build_task_text("Add logout", "Clear session", ["redirects"])
    links = [
        {"key": "ID-3", "type": "subtask"},
        {"key": "ID-3", "title": "duplicate", "type": "subtask"},
        {"key": "ID-3", "title": "related"},
        {"key": "ID-3", "title": "duplicate related", "type": ""},
        {"title": "keyless"},
        {"key": None},
        42,
    ]
    expected = [
        {"key": "ID-3", "type": "subtask"},
        {"key": "ID-3", "title": "related"},
    ]
    store = _FakeStore(hashes={"ID-1": task_content_hash(text)})
    graph = _FakeGraph()

    TaskService(store, graph, _FakeEmbedder()).index_batch([_brief(links=links)])

    assert store.link_updates == [("ID-1", expected)]
    assert graph.replaced_links == [("ID-1", expected)]


def test_index_batch_warns_when_links_row_is_missing_but_still_updates_graph():
    text = build_task_text("Add logout", "Clear session", ["redirects"])

    class _MissingRowStore(_FakeStore):
        def update_links(self, key, links):
            self.link_updates.append((key, links))
            return False

    links = [{"key": "ID-2"}]
    store = _MissingRowStore(hashes={"ID-1": task_content_hash(text)})
    graph = _FakeGraph()

    result = TaskService(store, graph, _FakeEmbedder()).index_batch([
        _brief(links=links),
    ])[0]

    assert result["links_stored"] is False
    assert any("store links" in warning and "not found" in warning
               for warning in result["warnings"])
    assert graph.replaced_links == [("ID-1", links)]


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


def test_refresh_meta_batch_ignores_links_even_if_present():
    store, graph = _FakeStore(), _FakeGraph()
    TaskService(store, graph, _FakeEmbedder()).refresh_meta_batch([
        {"key": "ID-1", "title": "T", "links": [{"key": "ID-2"}]},
    ])

    assert store.link_updates == []
    assert graph.replaced_links == []
    assert "links" not in store.meta_batch[0]


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


class _TimingOutStore(_FakeStore):
    """Стор, у которого пул не отдаёт соединение: каждый заход — 30 с в проде."""

    def __init__(self):
        super().__init__()
        self.existing_hash_calls = 0

    def existing_hash(self, key):
        self.existing_hash_calls += 1
        raise psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec")


def test_first_pool_timeout_stops_further_store_calls():
    """Критерий 4: число попыток равно одной, а не числу задач."""
    store, graph, emb = _TimingOutStore(), _FakeGraph(), _FakeEmbedder()
    tasks = [_brief(f"ID-{n}", f"PRI-{n}", title=f"t{n}", description=f"d{n}", links=[])
             for n in range(1, 48)]
    results = TaskService(store, graph, emb).index_batch(tasks)

    assert store.existing_hash_calls == 1
    assert len(results) == len(tasks)
    assert all(r["retry_required"] is True for r in results)
    assert all(r["embedded"] is False for r in results)


def test_pool_timeout_skips_voyage_call_entirely():
    """Писать результат некуда — квоту Voyage (3 RPM / 10K TPM) не тратим."""
    store, graph, emb = _TimingOutStore(), _FakeGraph(), _FakeEmbedder()
    tasks = [_brief(f"ID-{n}", f"PRI-{n}", title=f"t{n}", description=f"d{n}", links=[])
             for n in range(1, 6)]
    TaskService(store, graph, emb).index_batch(tasks)

    assert emb.doc_calls == []


def test_pool_timeout_skips_graph_phase():
    """Флаг один на оба хранилища: иначе та же арифметика повторится на графе."""
    store, graph, emb = _TimingOutStore(), _FakeGraph(), _FakeEmbedder()
    tasks = [_brief(f"ID-{n}", f"PRI-{n}", title=f"t{n}", description=f"d{n}", links=[])
             for n in range(1, 6)]
    TaskService(store, graph, emb).index_batch(tasks)

    assert graph.tasks == []


def test_result_shape_survives_the_early_exit():
    """mcp/service.py:983 проверяет длину результата — форма обязана совпадать."""
    store, graph, emb = _TimingOutStore(), _FakeGraph(), _FakeEmbedder()
    tasks = [_brief("ID-1", "PRI-1"), _brief("ID-2", "PRI-2", title="t2",
                                              description="d2", links=[])]
    results = TaskService(store, graph, emb).index_batch(tasks)

    for result in results:
        assert set(result) == {"key", "embedded", "links_upserted", "links_stored",
                               "prs_linked", "warnings", "retry_required"}


def test_non_storage_error_still_processes_every_task():
    """Сбой не-хранилища прежнее пер-задачное поведение не меняет."""
    class _BrokenStore(_FakeStore):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def existing_hash(self, key):
            self.calls += 1
            raise RuntimeError("boom")

    store, graph, emb = _BrokenStore(), _FakeGraph(), _FakeEmbedder()
    tasks = [_brief(f"ID-{n}", f"PRI-{n}", title=f"t{n}", description=f"d{n}", links=[])
             for n in range(1, 6)]
    results = TaskService(store, graph, emb).index_batch(tasks)

    assert store.calls == 5
    assert len(results) == 5


def test_refresh_meta_batch_skips_graph_loop_when_store_is_down():
    """У refresh_meta_batch свой пер-задачный цикл по графу — он тоже гасится."""
    class _TimingOutMetaStore(_FakeStore):
        def update_meta_batch(self, metas):
            raise psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec")

    store, graph, emb = _TimingOutMetaStore(), _FakeGraph(), _FakeEmbedder()
    metas = [{"key": f"ID-{n}", "title": f"t{n}", "status": "Open",
              "url": None, "aliases": [], "project": "PRI"} for n in range(1, 48)]
    result = TaskService(store, graph, emb).refresh_meta_batch(metas)

    assert graph.tasks == []
    assert result["warnings"]


def test_result_shape_survives_mid_batch_storage_failure():
    """Регрессия: обрыв стора НА ВТОРОЙ задаче шага 4 не должен резать форму
    результата первой задаче. До правки `prs_linked` проставлялся только
    циклом шага 6, а `if storage_down: break` в нём (ранний выход) обрывает
    цикл раньше, чем тот дойдёт до уже обработанной первой задачи, — набор
    ключей результата должен быть полным и одинаковым у ВСЕХ задач пачки,
    независимо от того, где именно внутри пачки сломался стор."""
    class _FailsOnSecondUpsert(_FakeStore):
        def __init__(self):
            super().__init__()
            self.upsert_calls = 0

        def upsert_task(self, row):
            self.upsert_calls += 1
            if self.upsert_calls == 2:
                raise psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec")
            super().upsert_task(row)

    store, graph, emb = _FailsOnSecondUpsert(), _FakeGraph(), _FakeEmbedder()
    tasks = [_brief("ID-1", "PRI-1", title="t1", description="d1", links=[]),
             _brief("ID-2", "PRI-2", title="t2", description="d2", links=[])]
    results = TaskService(store, graph, emb).index_batch(tasks)

    expected_keys = {"key", "embedded", "links_upserted", "links_stored",
                      "prs_linked", "warnings", "retry_required"}
    for r in results:
        assert set(r) == expected_keys
    assert results[0]["embedded"] is True        # первая задача упсертилась успешно
    assert results[1]["retry_required"] is True  # вторая — упёрлась в PoolTimeout


def test_storage_down_mid_hash_phase_still_blocks_voyage_call():
    """Регрессия квоты Voyage: `_TimingOutStore` (падает на КАЖДОМ ключе) не
    ловит эту дыру — при ней первая же задача летит в except шага 2 и
    to_embed остаётся пустым независимо от guard'а. Здесь первая задача
    успешно проходит existing_hash и уходит в to_embed, а вторая валит
    PoolTimeout и взводит storage_down уже ПОСЛЕ того, как to_embed
    непуст, — guard шага 3 обязан проверять именно storage_down, а не
    только пустоту to_embed, иначе Voyage вызывается для уже собранных
    to_embed-задач при частичной деградации пула."""
    class _MixedStore(_FakeStore):
        def existing_hash(self, key):
            if key == "ID-1":
                return None  # хэш не совпадёт → задача уходит в to_embed
            raise psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec")

    store, graph, emb = _MixedStore(), _FakeGraph(), _FakeEmbedder()
    tasks = [_brief("ID-1", "PRI-1", title="t1", description="d1", links=[]),
             _brief("ID-2", "PRI-2", title="t2", description="d2", links=[])]
    TaskService(store, graph, emb).index_batch(tasks)

    assert emb.doc_calls == []
