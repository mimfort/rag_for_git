from langchain_core.messages import AIMessage, HumanMessage

from reviewer.agent.analyzer import (
    LLMAnalyzer, LLMVerifier, LLMSynthesizer,
    _pr_context, _to_findings, _FindingModel, _window,
)
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
    """Фейковый провайдер с счётчиком вызовов и записью аргументов model."""

    def __init__(self, scripted, final_content):
        self.scripted = scripted
        self.final_content = final_content
        self.chat_model_calls = 0
        self.chat_model_with_tools_calls = 0
        self.last_model_arg: str | None = None
        self.last_tools_model_arg: str | None = None

    def chat_model_with_tools(self, tools, model=None):
        self.chat_model_with_tools_calls += 1
        self.last_tools_model_arg = model
        return ScriptedToolsLLM(list(self.scripted))

    def chat_model(self, model=None):
        self.chat_model_calls += 1
        self.last_model_arg = model
        return FinalLLM(self.final_content)


def _deps(**kwargs):
    base = dict(vcs=None, retriever=FakeRetriever(), graph=FakeGraph(), policy=None,
                analyzer=None, verifier=None, pr_number=1, head_sha="s",
                overlay_ref="pr:1", changed_paths=["a.py"], patches={})
    base.update(kwargs)
    return Deps(**base)


# ---------------------------------------------------------------------------
# Базовые тесты analyze (сохранены из предыдущего набора)
# ---------------------------------------------------------------------------

def test_analyze_survives_tool_error_and_parses_fenced_json():
    turn1 = AIMessage(content="", tool_calls=[
        {"name": "search_code", "args": {"query": "q"}, "id": "t1", "type": "tool_call"}])
    # turn2 — без tool_calls, но без валидного JSON -> fallback на final
    turn2 = AIMessage(content="ok")
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


# ---------------------------------------------------------------------------
# Инлайн-JSON: последний AI-ответ без доп. вызова
# ---------------------------------------------------------------------------

def test_analyze_inline_json_no_extra_call():
    """Если последний AI-ответ уже содержит валидный findings-JSON — доп. invoke не нужен."""
    inline_json = ('{"findings":[{"category":"correctness","severity":"high",'
                   '"line":5,"message":"inline bug"}]}')
    # Единственный ответ tool-loop содержит JSON и НЕ имеет tool_calls -> inline-путь
    prov = FakeProvider([AIMessage(content=inline_json)], "SHOULD NOT BE CALLED")
    out = LLMAnalyzer(prov, max_iterations=10).analyze(
        ReviewUnit("a.py", [], "code"), _deps())
    assert len(out) == 1
    assert out[0].message == "inline bug"
    # chat_model() НЕ должен вызываться
    assert prov.chat_model_calls == 0


def test_analyze_fallback_when_last_response_has_no_json():
    """Fallback: последний ответ не содержит JSON -> делаем доп. вызов."""
    final = '{"findings":[{"category":"security","severity":"low","line":1,"message":"sec"}]}'
    prov = FakeProvider([AIMessage(content="мысли вслух, без json")], final)
    out = LLMAnalyzer(prov, max_iterations=10).analyze(
        ReviewUnit("a.py", [], "code"), _deps())
    assert len(out) == 1
    assert out[0].message == "sec"
    # chat_model() вызывается для fallback
    assert prov.chat_model_calls == 1


def test_synthesize_inline_json_no_extra_call():
    """Синтезатор: inline-JSON из последнего AI-ответа без доп. вызова."""
    inline = ('{"keep": [0], "add": [{"category":"correctness","severity":"high",'
              '"line":5,"message":"cross-file","file":"b.py"}]}')
    prov = FakeProvider([AIMessage(content=inline)], "SHOULD NOT BE CALLED")
    s = LLMSynthesizer(prov, max_iterations=2)
    inp = [_finding(severity="high", msg="orig")]
    out = s.synthesize(inp, _deps())
    msgs = {f.message for f in out}
    assert msgs == {"orig", "cross-file"}
    assert prov.chat_model_calls == 0


def test_synthesize_fallback_when_last_response_bad():
    """Синтезатор: fallback-вызов, когда последний ответ не парсится."""
    final = '{"keep": [0], "add": []}'
    prov = FakeProvider([AIMessage(content="мусор")], final)
    s = LLMSynthesizer(prov, max_iterations=2)
    inp = [_finding(severity="high", msg="orig")]
    out = s.synthesize(inp, _deps())
    assert [f.message for f in out] == ["orig"]
    assert prov.chat_model_calls == 1


# ---------------------------------------------------------------------------
# Тесты verify (базовые, сохранены)
# ---------------------------------------------------------------------------

def _F(i):
    return Finding("correctness", "high", "a.py", i, "RIGHT", f"bug{i}", None, 0.9)


class VerifyProvider:
    def __init__(self, content):
        self.content = content
        self.last_model_arg = None

    def chat_model(self, model=None):
        self.last_model_arg = model
        return FinalLLM(self.content)


def test_verify_drops_false_keeps_true_and_unmentioned():
    findings = [_F(1), _F(2), _F(3)]
    prov = VerifyProvider('{"verdicts":[{"index":0,"is_real":true},'
                          '{"index":1,"is_real":false}]}')
    out = LLMVerifier(prov).verify(findings, _deps())
    assert {f.line for f in out} == {1, 3}


def test_verify_fail_open_on_unparseable():
    prov = VerifyProvider("не смог разобрать вердикт")
    out = LLMVerifier(prov).verify([_F(1)], _deps())
    assert len(out) == 1


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
    assert out[0].file == "other.py"
    assert out[1].file == "a.py"


def _finding(severity="high", confidence=0.9, msg="bug"):
    return Finding(category="correctness", severity=severity, file="a.py", line=2,
                   side="RIGHT", message=msg, suggestion=None, confidence=confidence)


def test_agentic_verify_low_severity_passes_without_llm():
    class BoomProvider:
        def chat_model_with_tools(self, tools, model=None): raise AssertionError("не должно вызываться")
        def chat_model(self, model=None): raise AssertionError("не должно вызываться")
    v = LLMVerifier(BoomProvider(), agentic=True, max_iterations=2, min_severity="high")
    out = v.verify([_finding(severity="low", confidence=0.9)], _deps())
    assert len(out) == 1


def test_agentic_verify_drops_false_positive():
    # Единственный AI-ответ содержит JSON is_real: false -> inline-путь
    inline = '{"is_real": false}'
    prov = FakeProvider([AIMessage(content=inline)], "SHOULD NOT BE CALLED")
    v = LLMVerifier(prov, agentic=True, max_iterations=2, min_severity="low")
    out = v.verify([_finding(severity="high")], _deps())
    assert out == []
    assert prov.chat_model_calls == 0


def test_agentic_verify_fail_open_on_unparseable():
    prov = FakeProvider([AIMessage(content="done")], "мусор без json")
    v = LLMVerifier(prov, agentic=True, max_iterations=2, min_severity="low")
    out = v.verify([_finding(severity="high")], _deps())
    assert len(out) == 1   # fail-open


def test_oneshot_verify_still_works_when_not_agentic():
    prov = FakeProvider([], '{"verdicts":[{"index":0,"is_real":false}]}')
    v = LLMVerifier(prov, agentic=False)
    out = v.verify([_finding(severity="high")], _deps())
    assert out == []


def test_agentic_verify_low_severity_but_uncertain_is_checked():
    prov = FakeProvider([AIMessage(content="done")], '{"is_real": false}')
    v = LLMVerifier(prov, agentic=True, max_iterations=2, min_severity="high")
    out = v.verify([_finding(severity="low", confidence=0.3)], _deps())
    assert out == []


# ---------------------------------------------------------------------------
# Тесты synthesize (базовые, сохранены)
# ---------------------------------------------------------------------------

def test_synthesize_keeps_originals_and_adds_cross_file():
    final = ('{"keep": [0], "add": [{"category":"correctness","severity":"high",'
             '"line":5,"message":"caller mismatch","file":"b.py"}]}')
    prov = FakeProvider([AIMessage(content="done")], final)
    s = LLMSynthesizer(prov, max_iterations=2)
    out = s.synthesize([_finding(severity="high", msg="orig")], _deps())
    msgs = {f.message for f in out}
    assert msgs == {"orig", "caller mismatch"}
    assert any(f.file == "b.py" for f in out)


def test_synthesize_preserves_fix_of_kept_finding():
    kept = Finding(category="correctness", severity="high", file="a.py", line=2,
                   side="RIGHT", message="bug", suggestion=None, confidence=0.9,
                   fix_start=2, fix_end=2, replacement="x = 1")
    prov = FakeProvider([AIMessage(content="done")], '{"keep": [0], "add": []}')
    s = LLMSynthesizer(prov, max_iterations=2)
    out = s.synthesize([kept], _deps())
    assert len(out) == 1
    assert out[0].replacement == "x = 1" and out[0].fix_start == 2 and out[0].fix_end == 2


def test_synthesize_drops_via_keep_omission():
    prov = FakeProvider([AIMessage(content="done")], '{"keep": [1], "add": []}')
    s = LLMSynthesizer(prov, max_iterations=2)
    a = _finding(severity="high", msg="dupe-a")
    b = _finding(severity="high", msg="keep-b")
    out = s.synthesize([a, b], _deps())
    assert [f.message for f in out] == ["keep-b"]


def test_synthesize_fail_open_on_unparseable():
    prov = FakeProvider([AIMessage(content="done")], "не json")
    s = LLMSynthesizer(prov, max_iterations=2)
    inp = [_finding(severity="high", msg="keep me")]
    out = s.synthesize(inp, _deps())
    assert out == inp


def test_synthesize_fail_open_on_empty_result():
    prov = FakeProvider([AIMessage(content="done")], '{"keep": [], "add": []}')
    s = LLMSynthesizer(prov, max_iterations=2)
    inp = [_finding(severity="high", msg="keep me")]
    out = s.synthesize(inp, _deps())
    assert out == inp


# ---------------------------------------------------------------------------
# Новые тесты
# ---------------------------------------------------------------------------

# --- _window ---

def test_window_basic():
    source = "\n".join(f"line{i}" for i in range(1, 11))  # 10 строк
    result = _window(source, line=5, radius=2)
    lines = result.splitlines()
    # строки 3..7 (1-based) -> 5 строк
    assert len(lines) == 5
    assert lines[0].startswith("3|")
    assert lines[-1].startswith("7|")


def test_window_clips_at_start():
    source = "\n".join(f"line{i}" for i in range(1, 11))
    result = _window(source, line=1, radius=5)
    lines = result.splitlines()
    assert lines[0].startswith("1|")


def test_window_clips_at_end():
    source = "\n".join(f"line{i}" for i in range(1, 6))  # 5 строк
    result = _window(source, line=5, radius=10)
    lines = result.splitlines()
    assert lines[-1].startswith("5|")


def test_window_empty_source():
    assert _window("", line=1) == ""


# --- prompt_cache ---

def test_prompt_cache_true_system_is_list():
    """При prompt_cache=True контент SystemMessage — список блоков с cache_control."""
    prov = FakeProvider(
        [AIMessage(content='{"findings":[]}')],
        "SHOULD NOT BE CALLED",
    )
    analyzer = LLMAnalyzer(prov, max_iterations=1, prompt_cache=True)

    class CaptureLLM:
        """Перехватывает сообщения, переданные в invoke."""
        def __init__(self):
            self.captured = None

        def invoke(self, messages):
            self.captured = list(messages)
            return AIMessage(content='{"findings":[]}')

        def bind_tools(self, tools):
            return self

    cap = CaptureLLM()

    class CapProvider:
        def chat_model_with_tools(self, tools, model=None):
            return cap
        def chat_model(self, model=None):
            return FinalLLM("FALLBACK")

    analyzer.provider = CapProvider()
    analyzer.analyze(ReviewUnit("a.py", [], "code"), _deps())

    system_content = cap.captured[0].content
    assert isinstance(system_content, list), "При prompt_cache=True контент должен быть списком"
    assert system_content[0].get("cache_control") == {"type": "ephemeral"}


def test_prompt_cache_false_system_is_string():
    """При prompt_cache=False контент SystemMessage — строка."""

    class CaptureLLM:
        def __init__(self):
            self.captured = None

        def invoke(self, messages):
            self.captured = list(messages)
            return AIMessage(content='{"findings":[]}')

        def bind_tools(self, tools):
            return self

    cap = CaptureLLM()

    class CapProvider:
        def chat_model_with_tools(self, tools, model=None):
            return cap
        def chat_model(self, model=None):
            return FinalLLM("FALLBACK")

    analyzer = LLMAnalyzer(CapProvider(), max_iterations=1, prompt_cache=False)
    analyzer.analyze(ReviewUnit("a.py", [], "code"), _deps())

    system_content = cap.captured[0].content
    assert isinstance(system_content, str), "При prompt_cache=False контент должен быть строкой"


# --- usage ---

class FakeUsageLog:
    """Фейковый UsageLog: записывает все вызовы add()."""

    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def add(self, stage: str, message) -> None:
        self.calls.append((stage, message))

    @property
    def count(self):
        return len(self.calls)

    def stages(self):
        return [c[0] for c in self.calls]


def test_usage_add_called_on_each_llm_call_analyze():
    """usage.add вызывается для каждого LLM-ответа в analyze."""
    inline_json = '{"findings":[]}'
    prov = FakeProvider([AIMessage(content=inline_json)], "FALLBACK")
    usage = FakeUsageLog()
    deps = _deps(usage=usage)
    LLMAnalyzer(prov, max_iterations=5).analyze(ReviewUnit("a.py", [], "code"), deps)
    assert usage.count >= 1
    assert all(s == "analyze" for s in usage.stages())


def test_usage_add_called_on_verify():
    """usage.add вызывается при verify (agentic=True)."""
    inline = '{"is_real": true}'
    prov = FakeProvider([AIMessage(content=inline)], "FALLBACK")
    usage = FakeUsageLog()
    deps = _deps(usage=usage)
    v = LLMVerifier(prov, agentic=True, max_iterations=2, min_severity="low")
    v.verify([_finding()], deps)
    assert usage.count >= 1
    assert all(s == "verify" for s in usage.stages())


def test_usage_add_called_on_synthesize():
    """usage.add вызывается при synthesize."""
    inline = '{"keep": [0], "add": []}'
    prov = FakeProvider([AIMessage(content=inline)], "FALLBACK")
    usage = FakeUsageLog()
    deps = _deps(usage=usage)
    s = LLMSynthesizer(prov, max_iterations=2)
    s.synthesize([_finding()], deps)
    assert usage.count >= 1
    assert all(s == "synthesize" for s in usage.stages())


def test_usage_fallback_also_reported():
    """При fallback-вызове usage.add тоже вызывается."""
    prov = FakeProvider([AIMessage(content="мусор без json")], '{"findings":[]}')
    usage = FakeUsageLog()
    deps = _deps(usage=usage)
    LLMAnalyzer(prov, max_iterations=5).analyze(ReviewUnit("a.py", [], "code"), deps)
    # tool-loop (1 вызов) + fallback (1 вызов) = 2
    assert usage.count == 2


# --- verify использует model-override ---

def test_verifier_model_override_passed_to_provider():
    """LLMVerifier с model='cheap' передаёт model в chat_model_with_tools и chat_model."""
    inline = '{"is_real": true}'
    prov = FakeProvider([AIMessage(content=inline)], "FALLBACK")
    v = LLMVerifier(prov, agentic=True, max_iterations=2, min_severity="low", model="cheap-model")
    v.verify([_finding()], _deps())
    assert prov.last_tools_model_arg == "cheap-model"


def test_verifier_oneshot_model_override():
    """oneshot-режим тоже передаёт model-override."""
    prov = FakeProvider([], '{"verdicts":[{"index":0,"is_real":true}]}')
    v = LLMVerifier(prov, agentic=False, model="cheap-oneshot")
    v.verify([_finding()], _deps())
    assert prov.last_model_arg == "cheap-oneshot"


# --- _verify_one включает окно кода и дифф ---

def test_verify_one_includes_code_window_and_diff():
    """_verify_one добавляет окно кода и дифф в human-промпт при наличии deps.sources."""
    source_code = "\n".join(f"line {i}" for i in range(1, 50))
    patch_text = "@@ -1,3 +1,3 @@\n-old\n+new"
    captured_messages: list = []

    class CaptureLLM:
        def invoke(self, messages):
            captured_messages.extend(messages)
            return AIMessage(content='{"is_real": true}')

        def bind_tools(self, tools):
            return self

    class CapProvider:
        def chat_model_with_tools(self, tools, model=None):
            return CaptureLLM()
        def chat_model(self, model=None):
            return FinalLLM("FALLBACK")

    v = LLMVerifier(CapProvider(), agentic=True, max_iterations=2, min_severity="low")
    deps = _deps(
        sources={"a.py": source_code},
        patches={"a.py": patch_text},
    )
    v.verify([_finding(severity="high")], deps)

    # Собираем весь текст human-сообщений
    human_texts = []
    for msg in captured_messages:
        if isinstance(msg, HumanMessage):
            content = msg.content
            if isinstance(content, list):
                human_texts.append("".join(p.get("text", "") for p in content if isinstance(p, dict)))
            else:
                human_texts.append(str(content))
    combined = "\n".join(human_texts)

    assert "Контекст кода" in combined, "Должно быть окно кода"
    assert "Дифф файла" in combined, "Должен быть дифф файла"
    assert "@@ -1,3 +1,3 @@" in combined


def test_verify_one_no_window_without_sources():
    """Если deps.sources не задан — окно кода НЕ добавляется."""
    captured_messages: list = []

    class CaptureLLM:
        def invoke(self, messages):
            captured_messages.extend(messages)
            return AIMessage(content='{"is_real": true}')

        def bind_tools(self, tools):
            return self

    class CapProvider:
        def chat_model_with_tools(self, tools, model=None):
            return CaptureLLM()
        def chat_model(self, model=None):
            return FinalLLM("FALLBACK")

    v = LLMVerifier(CapProvider(), agentic=True, max_iterations=2, min_severity="low")
    v.verify([_finding(severity="high")], _deps())   # sources=None по умолчанию

    human_texts = []
    for msg in captured_messages:
        if isinstance(msg, HumanMessage):
            content = msg.content
            if isinstance(content, list):
                human_texts.append("".join(p.get("text", "") for p in content if isinstance(p, dict)))
            else:
                human_texts.append(str(content))
    combined = "\n".join(human_texts)

    assert "Контекст кода" not in combined
