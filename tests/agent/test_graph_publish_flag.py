from reviewer.agent.graph import build_graph
from reviewer.agent.state import Deps, ReviewUnit
from reviewer.vcs.base import Finding
from reviewer.policy.policy import ReviewPolicy


class FakeAnalyzer:
    def analyze(self, unit, deps):
        return [Finding("correctness", "high", unit.path, 2, "RIGHT",
                        f"bug in {unit.path}", None, 0.9)]


class PassVerifier:
    def verify(self, findings, deps):
        return findings


class FakeVCS:
    def __init__(self):
        self.published = None

    def list_existing_fingerprints(self, n):
        return set()

    def publish_review(self, n, sha, summary, comments):
        self.published = (summary, comments)


def _deps(vcs):
    return Deps(vcs=vcs, retriever=None, graph=None, policy=ReviewPolicy(),
               analyzer=FakeAnalyzer(), verifier=PassVerifier(),
               pr_number=1, head_sha="sha", overlay_ref="pr:1",
               changed_paths=["a.py"], patches={"a.py": "@@ -1,2 +1,2 @@\n x\n+y\n"})


def test_publish_false_skips_publish_node():
    """publish=False — узла publish нет в графе."""
    g = build_graph(_deps(FakeVCS()), publish=False)
    assert "publish" not in g.get_graph().nodes


def test_publish_true_includes_publish_node():
    """По умолчанию (publish=True) узел publish присутствует."""
    g = build_graph(_deps(FakeVCS()), publish=True)
    assert "publish" in g.get_graph().nodes


def test_publish_false_does_not_call_vcs_but_assembles_state():
    """publish=False — publish_review не вызывается, но summary/inline считаются."""
    vcs = FakeVCS()
    g = build_graph(_deps(vcs), publish=False)
    state = g.invoke({"review_units": [ReviewUnit("a.py", ["a.py#f"], "y")],
                      "findings": [], "failed_units": [], "verified": [],
                      "summary": "", "inline_comments": []})
    # publish_review не дёрнут
    assert vcs.published is None
    # но финальное состояние посчитано: inline на строку 2 диффа
    assert len(state["inline_comments"]) == 1
    assert state["inline_comments"][0].path == "a.py"
    assert state["inline_comments"][0].line == 2
    assert state["summary"]
