from langchain_core.messages import AIMessage
from reviewer.agent.analyzer import LLMAnalyzer, _Findings, _FindingModel
from reviewer.agent.state import ReviewUnit, Deps

class FakeRetriever:
    def retrieve(self, **kw):
        raise RuntimeError("boom")   # инструмент search_code упадёт

class FakeGraph:
    def expand(self, ids, hops=2):
        return set()

class ScriptedToolsLLM:
    def __init__(self, msgs): self.msgs = msgs; self.i = 0
    def invoke(self, messages):
        m = self.msgs[self.i]; self.i += 1; return m

class StructuredLLM:
    def __init__(self, result): self.result = result
    def invoke(self, messages): return self.result

class FakeChatModel:
    def __init__(self, result): self.result = result
    def with_structured_output(self, schema): return StructuredLLM(self.result)

class FakeProvider:
    def __init__(self, scripted, findings):
        self.scripted = scripted; self.findings = findings
    def chat_model_with_tools(self, tools): return ScriptedToolsLLM(self.scripted)
    def chat_model(self): return FakeChatModel(self.findings)

def _deps():
    return Deps(vcs=None, retriever=FakeRetriever(), graph=FakeGraph(), policy=None,
                analyzer=None, verifier=None, pr_number=1, head_sha="s",
                overlay_ref="pr:1", changed_paths=["a.py"], patches={})

def test_analyze_survives_tool_error_and_returns_findings():
    turn1 = AIMessage(content="", tool_calls=[
        {"name": "search_code", "args": {"query": "q"}, "id": "t1", "type": "tool_call"}])
    turn2 = AIMessage(content="done")   # без tool_calls -> цикл завершается
    findings = _Findings(findings=[
        _FindingModel(category="correctness", severity="high", line=2, message="bug")])
    prov = FakeProvider([turn1, turn2], findings)
    out = LLMAnalyzer(prov, max_iterations=10).analyze(
        ReviewUnit("a.py", ["a.py#f"], "code"), _deps())
    assert len(out) == 1 and out[0].file == "a.py" and out[0].line == 2

def test_analyze_normalizes_unknown_severity():
    turn1 = AIMessage(content="done")   # сразу без tool_calls
    findings = _Findings(findings=[
        _FindingModel(category="x", severity="info", line=None, message="m")])
    prov = FakeProvider([turn1], findings)
    out = LLMAnalyzer(prov, max_iterations=10).analyze(
        ReviewUnit("a.py", [], "code"), _deps())
    assert out[0].severity == "medium"   # 'info' нормализовано
