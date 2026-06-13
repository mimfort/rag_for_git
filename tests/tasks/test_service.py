from reviewer.tasks.graph import PRRef
from reviewer.tasks.service import TaskService
from reviewer.tasks.store import task_content_hash, build_task_text


class _FakeStore:
    def __init__(self, hashes=None, search_result=None):
        self._hashes = hashes or {}
        self.upserted = []
        self.meta_updates = []
        self._search_result = search_result or []

    def existing_hash(self, key):
        return self._hashes.get(key)

    def upsert_task(self, row):
        self.upserted.append(row)

    def update_meta(self, key, title, status, url, aliases):
        self.meta_updates.append((key, title, status, url, aliases))

    def search(self, q, vec, top_k=5):
        return self._search_result


class _FakeGraph:
    def __init__(self, context=None, raise_on=()):
        self.tasks = []
        self.links = []
        self.pr_links = []
        self._context = context or {}
        self._raise_on = set(raise_on)

    def upsert_task(self, key, aliases, title, status, url):
        if "upsert_task" in self._raise_on:
            raise RuntimeError("neo4j down")
        self.tasks.append((key, aliases, title, status, url))

    def upsert_links(self, key, links):
        self.links.append((key, links))
        return len(links)

    def link_pr(self, task_key, pr, touched):
        self.pr_links.append((task_key, pr, touched))

    def task_context(self, key):
        if "task_context" in self._raise_on:
            raise RuntimeError("neo4j down")
        return self._context


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
