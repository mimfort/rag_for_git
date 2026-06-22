from reviewer.tools.code_tools import ToolContext, make_tools


class FakeRetriever:
    def __init__(self):
        self.branch = None

    def retrieve(self, repo, query, changed_node_ids, overlay_ref, changed_paths,
                 top_k=8, *, branch=""):
        self.branch = branch

        class P:
            def as_context(self_):
                return "ctx"

        return P()


class FakeGraph:
    def __init__(self):
        self.branch = None

    def expand_detailed(self, repo, node_ids, hops=2, *, branch=""):
        self.branch = branch
        return []


def test_search_code_passes_branch():
    r = FakeRetriever()
    ctx = ToolContext(retriever=r, graph=FakeGraph(), overlay_ref="pr:1",
                     changed_paths=["a.py"], repo="a/x", branch="master")
    tools = {t.name: t for t in make_tools(ctx)}
    tools["search_code"].invoke({"query": "x"})
    assert r.branch == "master"


def test_get_related_symbols_passes_branch():
    g = FakeGraph()
    ctx = ToolContext(retriever=FakeRetriever(), graph=g, overlay_ref="pr:1",
                     changed_paths=[], repo="a/x", branch="master")
    tools = {t.name: t for t in make_tools(ctx)}
    tools["get_related_symbols"].invoke({"node_id": "a.py#f"})
    assert g.branch == "master"
