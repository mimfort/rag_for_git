from reviewer.agent.graph import build_graph
from reviewer.agent.state import Deps, ReviewUnit
from reviewer.vcs.base import Finding
from reviewer.policy.policy import ReviewPolicy

class FakeAnalyzer:
    def analyze(self, unit, deps):
        return [Finding("correctness", "high", unit.path, 2, "RIGHT", f"bug in {unit.path}", None, 0.9)]
class PassVerifier:
    def verify(self, findings, deps): return findings
class FakeVCS:
    def __init__(self): self.published = None
    def list_existing_fingerprints(self, n): return set()
    def publish_review(self, n, sha, summary, comments):
        self.published = (summary, comments)

def _deps(vcs):
    return Deps(vcs=vcs, retriever=None, graph=None, policy=ReviewPolicy(),
               analyzer=FakeAnalyzer(), verifier=PassVerifier(),
               pr_number=1, head_sha="sha", overlay_ref="pr:1",
               changed_paths=["a.py"], patches={"a.py": "@@ -1,2 +1,2 @@\n x\n+y\n"})

def test_end_to_end_inline_on_diff_line():
    vcs = FakeVCS()
    g = build_graph(_deps(vcs))
    g.invoke({"review_units": [ReviewUnit("a.py", ["a.py#f"], "y")],
              "findings": [], "verified": [], "summary": "", "inline_comments": []})
    summary, comments = vcs.published
    assert len(comments) == 1 and comments[0].path == "a.py" and comments[0].line == 2
    assert "ai-review:" in comments[0].body

def test_suggestion_rendered_as_text_not_github_suggestion_block():
    vcs = FakeVCS()
    deps = _deps(vcs)
    class WithSuggestion:
        def analyze(self, unit, deps):
            return [Finding("correctness", "high", "a.py", 2, "RIGHT",
                            "bug", "Добавить guard перед делением", 0.9)]
    deps.analyzer = WithSuggestion()
    build_graph(deps).invoke({"review_units": [ReviewUnit("a.py", ["a.py#f"], "y")],
                              "findings": [], "verified": [], "summary": "", "inline_comments": []})
    _, comments = vcs.published
    body = comments[0].body
    assert "Добавить guard" in body            # совет присутствует как текст
    assert "```suggestion" not in body          # но НЕ как applyable-блок GitHub


def test_finding_off_diff_goes_to_summary():
    vcs = FakeVCS()
    deps = _deps(vcs)
    class OffDiff:
        def analyze(self, unit, deps):
            return [Finding("correctness","high","a.py",999,"RIGHT","far away",None,0.8)]
    deps.analyzer = OffDiff()
    build_graph(deps).invoke({"review_units":[ReviewUnit("a.py",["a.py#f"],"y")],
                              "findings":[],"verified":[],"summary":"","inline_comments":[]})
    summary, comments = vcs.published
    assert comments == [] and "far away" in summary
