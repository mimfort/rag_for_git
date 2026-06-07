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


def _finding_with_fix(start, end, replacement):
    return Finding("correctness", "high", "a.py", end, "RIGHT", "bug", "поясн", 0.9,
                   fix_start=start, fix_end=end, replacement=replacement)

class _Analyzer:
    def __init__(self, findings): self._f = findings
    def analyze(self, unit, deps): return list(self._f)

def _run(deps):
    build_graph(deps).invoke({"review_units": [ReviewUnit("a.py", ["a.py#f"], "y")],
                              "findings": [], "verified": [], "summary": "", "inline_comments": []})

def test_applyable_multiline_suggestion_in_diff():
    vcs = FakeVCS(); deps = _deps(vcs)            # diff RIGHT = {1, 2}
    deps.analyzer = _Analyzer([_finding_with_fix(1, 2, "new1\nnew2")])
    _run(deps)
    _, comments = vcs.published
    c = comments[0]
    assert "```suggestion\nnew1\nnew2\n```" in c.body          # applyable-блок с дословной заменой
    assert c.start_line == 1 and c.line == 2                    # привязан к точному диапазону
    assert c.side == "RIGHT" and c.start_side == "RIGHT"

def test_suggestion_skipped_when_range_outside_diff():
    vcs = FakeVCS(); deps = _deps(vcs)
    deps.analyzer = _Analyzer([_finding_with_fix(5, 5, "x")])   # строка 5 вне диффа
    _run(deps)
    summary, comments = vcs.published
    assert comments == [] and "```suggestion" not in summary    # не applyable и не 422

def test_text_mode_disables_suggestion_block():
    vcs = FakeVCS(); deps = _deps(vcs); deps.suggestions_mode = "text"
    deps.analyzer = _Analyzer([_finding_with_fix(1, 2, "new1\nnew2")])
    _run(deps)
    _, comments = vcs.published
    assert "```suggestion" not in comments[0].body              # режим text → только текст

def test_overlapping_suggestions_second_not_applyable():
    vcs = FakeVCS(); deps = _deps(vcs)
    deps.analyzer = _Analyzer([_finding_with_fix(1, 2, "A"), _finding_with_fix(2, 2, "B")])
    _run(deps)
    _, comments = vcs.published
    assert len([c for c in comments if "```suggestion" in c.body]) == 1   # пересечение → второй не applyable


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
