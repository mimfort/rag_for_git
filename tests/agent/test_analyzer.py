from langchain_core.messages import AIMessage

from reviewer.agent.analyzer import LLMAnalyzer, LLMVerifier, _pr_context, _to_findings, _FindingModel
from reviewer.agent.state import ReviewUnit, Deps
from reviewer.vcs.base import Finding


class FakeRetriever:
    def retrieve(self, **kw):
        raise RuntimeError("boom")   # инструмент search_code упадёт


class FakeGraph:
    def expand(self, ids, hops=2):
        return set()


class ScriptedToolsLLM:
    def __init__(self, msgs):
        self.msgs = msgs
        self.i = 0

    def invoke(self, messages):
        m = self.msgs[self.i]
        self.i += 1
        return m


class FinalLLM:
    def __init__(self, content):
        self.content = content

    def invoke(self, messages):
        return AIMessage(content=self.content)


class FakeProvider:
    def __init__(self, scripted, final_content):
        self.scripted = scripted
        self.final_content = final_content

    def chat_model_with_tools(self, tools):
        return ScriptedToolsLLM(self.scripted)

    def chat_model(self):
        return FinalLLM(self.final_content)


def _deps():
    return Deps(vcs=None, retriever=FakeRetriever(), graph=FakeGraph(), policy=None,
                analyzer=None, verifier=None, pr_number=1, head_sha="s",
                overlay_ref="pr:1", changed_paths=["a.py"], patches={})


def test_analyze_survives_tool_error_and_parses_fenced_json():
    turn1 = AIMessage(content="", tool_calls=[
        {"name": "search_code", "args": {"query": "q"}, "id": "t1", "type": "tool_call"}])
    turn2 = AIMessage(content="ok")   # без tool_calls -> выходим из цикла, затем JSON-вывод
    final = ('```json\n{"findings":[{"category":"correctness","severity":"high",'
             '"line":2,"message":"bug"}]}\n```')
    prov = FakeProvider([turn1, turn2], final)
    out = LLMAnalyzer(prov, max_iterations=10).analyze(
        ReviewUnit("a.py", ["a.py#f"], "code"), _deps())
    assert len(out) == 1
    assert out[0].file == "a.py" and out[0].line == 2 and out[0].severity == "high"


def test_analyze_normalizes_unknown_severity_plain_json():
    final = '{"findings":[{"category":"x","severity":"info","line":null,"message":"m"}]}'
    prov = FakeProvider([AIMessage(content="done")], final)
    out = LLMAnalyzer(prov, max_iterations=10).analyze(
        ReviewUnit("a.py", [], "code"), _deps())
    assert out[0].severity == "medium"   # 'info' -> medium


def test_analyze_parses_fix_into_finding():
    final = ('{"findings":[{"category":"correctness","severity":"high","line":2,'
             '"message":"m","fix":{"start_line":1,"end_line":2,"replacement":"A\\nB"}}]}')
    prov = FakeProvider([AIMessage(content="done")], final)
    out = LLMAnalyzer(prov, max_iterations=10).analyze(ReviewUnit("a.py", [], "code"), _deps())
    assert out[0].fix_start == 1 and out[0].fix_end == 2 and out[0].replacement == "A\nB"


def test_analyze_empty_on_unparseable_output():
    prov = FakeProvider([AIMessage(content="done")], "извините, JSON не дам")
    out = LLMAnalyzer(prov, max_iterations=10).analyze(
        ReviewUnit("a.py", [], "code"), _deps())
    assert out == []


def _F(i):
    return Finding("correctness", "high", "a.py", i, "RIGHT", f"bug{i}", None, 0.9)


class VerifyProvider:
    def __init__(self, content):
        self.content = content

    def chat_model(self):
        return FinalLLM(self.content)


def test_verify_drops_false_keeps_true_and_unmentioned():
    findings = [_F(1), _F(2), _F(3)]   # индексы 0,1,2 -> line 1,2,3
    prov = VerifyProvider('{"verdicts":[{"index":0,"is_real":true},'
                          '{"index":1,"is_real":false}]}')
    out = LLMVerifier(prov).verify(findings, _deps())
    assert {f.line for f in out} == {1, 3}   # index1 (line2) отброшен, index2 не упомянут -> оставлен


def test_verify_fail_open_on_unparseable():
    prov = VerifyProvider("не смог разобрать вердикт")
    out = LLMVerifier(prov).verify([_F(1)], _deps())
    assert len(out) == 1   # fail-open: реальный баг не теряем


def test_pr_context_includes_title_and_manifest():
    deps = _deps()
    deps.pr_title = "Fix auth"
    deps.pr_body = "body text"
    deps.changed_status = {"a.py": "modified"}
    out = _pr_context(deps, ["a.py"])
    assert "Fix auth" in out and "body text" in out and "a.py (modified)" in out


def test_to_findings_respects_model_file_then_default():
    models = [
        _FindingModel(category="correctness", severity="high", message="m1", file="other.py"),
        _FindingModel(category="security", severity="low", message="m2"),
    ]
    out = _to_findings(models, default_file="a.py")
    assert out[0].file == "other.py"     # из модели
    assert out[1].file == "a.py"         # фолбэк на default
