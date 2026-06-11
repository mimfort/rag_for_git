from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from reviewer.agent.analyzer import (
    LLMAnalyzer, LLMVerifier, LLMSynthesizer,
    _pr_context, _to_findings, _FindingModel, _window,
    _file_context, _signature_changes, _pr_bundle, _pr_bundle_static,
    _run_tool_loop,
)
from reviewer.agent.state import ReviewUnit, Deps
from reviewer.config.settings import Settings
from reviewer.llm.budget import BudgetTracker
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

    def __init__(self, total_tokens: int = 0):
        self.calls: list[tuple[str, object]] = []
        self._total_tokens = total_tokens

    def add(self, stage: str, message) -> None:
        self.calls.append((stage, message))

    @property
    def count(self):
        return len(self.calls)

    def stages(self):
        return [c[0] for c in self.calls]

    @property
    def total_tokens(self) -> int:
        return self._total_tokens


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


# ---------------------------------------------------------------------------
# _file_context — адаптивный контекст файла в analyze
# ---------------------------------------------------------------------------


def _gen_module(n_funcs: int, body_lines: int) -> str:
    """Синтезирует валидный python-модуль из n_funcs функций по body_lines строк тела."""
    out: list[str] = []
    for k in range(n_funcs):
        out.append(f"def func_{k}():")
        for j in range(body_lines):
            out.append(f"    x_{k}_{j} = {j}")
        out.append("")
    return "\n".join(out)


def test_file_context_small_file_is_full():
    """Файл ≤ 400 строк показывается целиком с нумерацией N|код."""
    source = "\n".join(f"line{i}" for i in range(1, 51))  # 50 строк
    unit = ReviewUnit("a.py", [], "@@ -1,1 +1,1 @@\n+x", new_source=source)
    ctx = _file_context(unit)
    lines = ctx.splitlines()
    assert len(lines) == 50
    assert lines[0] == "1|line1"
    assert lines[-1] == "50|line50"


def test_file_context_empty_source():
    unit = ReviewUnit("a.py", [], "", new_source="")
    assert _file_context(unit) == ""


def test_file_context_large_file_windows_real_line_numbers():
    """Большой файл (>400) -> окна вокруг хунков с реальной нумерацией + сигнатуры + маркер пропуска."""
    source = _gen_module(n_funcs=120, body_lines=4)  # ~720 строк
    total = len(source.splitlines())
    assert total > 400
    # два далёких хунка (200 и 500) -> два отдельных окна с маркером пропуска между ними
    changed = ("@@ -200,3 +200,3 @@\n-old\n+new\n context\n"
               "@@ -500,3 +500,3 @@\n-old\n+new\n context\n")
    unit = ReviewUnit("big.py", [], changed, new_source=source)
    ctx = _file_context(unit)
    # реальные номера строк вокруг хунков (radius=50)
    assert "200|" in ctx
    assert "500|" in ctx
    assert "Структура модуля (сигнатуры):" in ctx
    assert "def func_" in ctx          # сигнатура из chunk_python
    # между двумя окнами должен быть маркер пропуска
    assert "пропущены" in ctx
    # окно не охватывает весь файл -> строка 1 не показана как код окна
    assert "1|def func_0" not in ctx


def test_file_context_large_file_merges_overlapping_hunks():
    """Пересекающиеся/смежные хунки сливаются в одно окно (без дубля маркеров между ними)."""
    source = _gen_module(n_funcs=120, body_lines=4)  # ~720 строк
    # два близких хунка: 300 и 320 — после ±50 окна пересекаются -> 1 окно
    changed = ("@@ -300,2 +300,2 @@\n-a\n+b\n"
               "@@ -320,2 +320,2 @@\n-c\n+d\n")
    unit = ReviewUnit("big.py", [], changed, new_source=source)
    ctx = _file_context(unit)
    # между 300 и 320 НЕ должно быть маркера пропуска (слиты)
    body = ctx.split("Структура модуля (сигнатуры):")[-1]
    # отрезаем блок сигнатур, ищем маркеры в окнах
    skips = [ln for ln in body.splitlines() if "пропущены" in ln]
    # допустим максимум 1 ведущий маркер (до первого окна), но не между 300 и 320
    assert "300|" in ctx and "320|" in ctx
    # обе строки в одном непрерывном окне -> между ними реальные номера 301..319
    assert "310|" in ctx
    assert len(skips) <= 1


def test_file_context_large_file_empty_diff_falls_back_to_head():
    """Большой файл с пустым/кривым диффом -> первые 400 строк целиком."""
    source = "\n".join(f"row{i}" for i in range(1, 601))  # 600 строк, не парсится как модуль
    unit = ReviewUnit("a.py", [], "", new_source=source)
    ctx = _file_context(unit)
    lines = ctx.splitlines()
    assert len(lines) == 400
    assert lines[0] == "1|row1"
    assert lines[-1] == "400|row400"
    assert "Структура модуля" not in ctx   # fallback без сигнатур


def test_file_context_1300_lines_now_windows_not_empty():
    """Файл 1300+ строк (раньше _numbered давал пустоту) -> теперь непустые окна."""
    source = _gen_module(n_funcs=220, body_lines=4)  # ~1320 строк
    total = len(source.splitlines())
    assert total > 1300
    changed = "@@ -600,3 +600,3 @@\n-old\n+new\n context"
    unit = ReviewUnit("huge.py", [], changed, new_source=source)
    ctx = _file_context(unit)
    assert ctx != ""
    assert "600|" in ctx


# ---------------------------------------------------------------------------
# _signature_changes — подсказка об изменённых сигнатурах в synthesize
# ---------------------------------------------------------------------------


def test_signature_changes_captures_both_def_lines():
    patch = ("@@ -1,1 +1,1 @@\n"
             "-def connect(host, port):\n"
             "+def connect(host, port, timeout):\n")
    out = _signature_changes({"a.py": patch})
    assert "a.py:" in out
    assert "- def connect(host, port):" in out
    assert "+ def connect(host, port, timeout):" in out


def test_signature_changes_handles_class_and_async_def():
    patch = ("@@ -1,1 +1,1 @@\n"
             "+class Foo:\n"
             "+async def run(self):\n"
             "+    x = 1\n")
    out = _signature_changes({"b.py": patch})
    assert "+ class Foo:" in out
    assert "+ async def run(self):" in out
    assert "x = 1" not in out   # тела не попадают


def test_signature_changes_empty_when_no_signatures():
    patch = "@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n"
    assert _signature_changes({"a.py": patch}) == ""


def test_signature_changes_ignores_file_headers():
    """Строки ---/+++ не считаются изменениями сигнатур даже при совпадении."""
    patch = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-x=1\n+x=2\n"
    assert _signature_changes({"x.py": patch}) == ""


def test_synthesize_prompt_includes_signature_changes():
    """Блок изменённых сигнатур попадает в human-промпт синтезатора."""
    captured: list = []

    class CaptureLLM:
        def invoke(self, messages):
            captured.extend(messages)
            return AIMessage(content='{"keep": [0], "add": []}')

        def bind_tools(self, tools):
            return self

    class CapProvider:
        def chat_model_with_tools(self, tools, model=None):
            return CaptureLLM()
        def chat_model(self, model=None):
            return FinalLLM("FALLBACK")

    patch = ("@@ -1,1 +1,1 @@\n"
             "-def connect(host, port):\n"
             "+def connect(host, port, timeout):\n")
    deps = _deps(patches={"a.py": patch})
    s = LLMSynthesizer(CapProvider(), max_iterations=2)
    s.synthesize([_finding(severity="high")], deps)

    human_texts = []
    for msg in captured:
        if isinstance(msg, HumanMessage):
            c = msg.content
            human_texts.append("".join(p.get("text", "") for p in c if isinstance(p, dict))
                               if isinstance(c, list) else str(c))
    combined = "\n".join(human_texts)
    assert "Изменённые сигнатуры в PR" in combined
    assert "+ def connect(host, port, timeout):" in combined


# ---------------------------------------------------------------------------
# VerdictLog-хук в верификаторе
# ---------------------------------------------------------------------------


class FakeVerdictLog:
    """Фейковый VerdictLog: записывает вызовы log_verdict."""

    def __init__(self):
        self.verdicts: list[tuple[object, bool, str]] = []

    def log_verdict(self, finding, is_real: bool, source: str = "agentic") -> None:
        self.verdicts.append((finding, is_real, source))

    def log_published(self, finding, inline: bool) -> None:  # для полноты интерфейса
        pass


def test_verdict_logged_agentic_true():
    prov = FakeProvider([AIMessage(content='{"is_real": true}')], "FALLBACK")
    vlog = FakeVerdictLog()
    deps = _deps(verdicts=vlog)
    v = LLMVerifier(prov, agentic=True, max_iterations=2, min_severity="low")
    v.verify([_finding(severity="high")], deps)
    assert len(vlog.verdicts) == 1
    _, is_real, source = vlog.verdicts[0]
    assert is_real is True and source == "agentic"


def test_verdict_logged_agentic_false():
    prov = FakeProvider([AIMessage(content='{"is_real": false}')], "FALLBACK")
    vlog = FakeVerdictLog()
    deps = _deps(verdicts=vlog)
    v = LLMVerifier(prov, agentic=True, max_iterations=2, min_severity="low")
    out = v.verify([_finding(severity="high")], deps)
    assert out == []
    assert vlog.verdicts[0][1] is False and vlog.verdicts[0][2] == "agentic"


def test_verdict_logged_on_fallback_path():
    """Fallback-инвок (последний ответ без JSON) тоже логирует вердикт."""
    prov = FakeProvider([AIMessage(content="мысли вслух")], '{"is_real": false}')
    vlog = FakeVerdictLog()
    deps = _deps(verdicts=vlog)
    v = LLMVerifier(prov, agentic=True, max_iterations=2, min_severity="low")
    out = v.verify([_finding(severity="high")], deps)
    assert out == []
    assert len(vlog.verdicts) == 1
    assert vlog.verdicts[0][1] is False and vlog.verdicts[0][2] == "agentic"


def test_verdict_logged_on_fail_open():
    """Fail-open (неразборный вердикт -> оставляем) логирует is_real=True."""
    prov = FakeProvider([AIMessage(content="done")], "мусор без json")
    vlog = FakeVerdictLog()
    deps = _deps(verdicts=vlog)
    v = LLMVerifier(prov, agentic=True, max_iterations=2, min_severity="low")
    out = v.verify([_finding(severity="high")], deps)
    assert len(out) == 1
    assert len(vlog.verdicts) == 1
    assert vlog.verdicts[0][1] is True and vlog.verdicts[0][2] == "agentic"


def test_verdict_not_logged_on_needs_check_skip():
    """Дёшево пропущенная находка (_needs_check=False) НЕ логируется — это не вердикт."""
    class BoomProvider:
        def chat_model_with_tools(self, tools, model=None):
            raise AssertionError("не должно вызываться")
        def chat_model(self, model=None):
            raise AssertionError("не должно вызываться")

    vlog = FakeVerdictLog()
    deps = _deps(verdicts=vlog)
    v = LLMVerifier(BoomProvider(), agentic=True, max_iterations=2, min_severity="high")
    out = v.verify([_finding(severity="low", confidence=0.9)], deps)
    assert len(out) == 1
    assert vlog.verdicts == []


def test_verdict_oneshot_logs_all_with_source_oneshot():
    prov = FakeProvider([], '{"verdicts":[{"index":0,"is_real":true},'
                            '{"index":1,"is_real":false}]}')
    vlog = FakeVerdictLog()
    deps = _deps(verdicts=vlog)
    v = LLMVerifier(prov, agentic=False)
    out = v.verify([_finding(msg="a"), _finding(msg="b")], deps)
    assert [f.message for f in out] == ["a"]
    assert len(vlog.verdicts) == 2
    assert all(src == "oneshot" for _, _, src in vlog.verdicts)
    assert vlog.verdicts[0][1] is True and vlog.verdicts[1][1] is False


class CountingRetrieverA:
    """Считает retrieve — проверка, что deps.tool_cache доходит до ToolContext."""
    def __init__(self):
        self.calls = 0
    def retrieve(self, **kw):
        from reviewer.retrieval.retriever import ContextPack
        from reviewer.index.store import Retrieved
        self.calls += 1
        return ContextPack([Retrieved("a.py#f", "a.py", "f", "function", 1, 2, "x", 1.0)])


def test_analyze_shares_tool_cache_across_units():
    """Общий deps.tool_cache шарится между юнитами при совпадающем ctx_sig.

    Здесь оба юнита имеют changed_node_ids=[] (как стадии verify/synthesize) -> ctx_sig
    одинаков -> второй analyze берёт результат search_code из общего кэша (1 вызов retrieve).
    Обратный случай (разные node_ids -> кэш-промах) покрыт на уровне тулов тестом
    test_run_cache_respects_changed_node_ids."""
    tool_call = AIMessage(content="", tool_calls=[
        {"name": "search_code", "args": {"query": "q"}, "id": "t1", "type": "tool_call"}])
    final_json = AIMessage(content='{"findings":[]}')
    ret = CountingRetrieverA()
    deps = _deps(retriever=ret, tool_cache={})
    LLMAnalyzer(FakeProvider([tool_call, final_json], "FB"), max_iterations=10).analyze(
        ReviewUnit("a.py", [], "code"), deps)
    LLMAnalyzer(FakeProvider([tool_call, final_json], "FB"), max_iterations=10).analyze(
        ReviewUnit("b.py", [], "code"), deps)
    assert ret.calls == 1


# ---------------------------------------------------------------------------
# _pr_bundle — предзагрузка диффов чужих файлов + сигнатур в промпт
# ---------------------------------------------------------------------------


def _combine_human(messages):
    """Склеить текст всех HumanMessage (учитывая cacheable-блоки списком)."""
    out = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            c = msg.content
            out.append("".join(p.get("text", "") for p in c if isinstance(p, dict))
                       if isinstance(c, list) else str(c))
    return "\n".join(out)


def test_pr_bundle_excludes_current_and_includes_signatures():
    patches = {"a.py": "@@ -1 +1 @@\n-def f(x):\n+def f(x, y):",
               "b.py": "@@ -1 +1 @@\n+z = 1"}
    sources = {"a.py": "def f(x, y):\n    return x"}
    deps = _deps(changed_paths=["a.py", "b.py"], patches=patches, sources=sources)
    out = _pr_bundle(deps, ["a.py", "b.py"], current_path="a.py")
    assert "--- b.py ---" in out
    assert "--- a.py ---" not in out                 # текущий файл исключён
    assert "Изменённые сигнатуры в PR" in out
    assert "+ def f(x, y):" in out
    assert "Структура изменённых модулей" in out     # из sources a.py


def test_pr_bundle_caps_total_diff_lines():
    big_patch = "\n".join(f"+line{i}" for i in range(1, 1001))   # 1000 строк
    patches = {f"f{k}.py": big_patch for k in range(5)}          # 5 файлов × 1000
    deps = _deps(changed_paths=list(patches), patches=patches)
    out = _pr_bundle(deps, list(patches))
    assert "опущены" in out                          # часть файлов не влезла в кап


def test_pr_bundle_omitted_hint_when_all_diffs_exceed_cap():
    """Если ни один дифф не влез в кап — diff-секции нет, но подсказка про get_changed_file_diff есть."""
    huge = "\n".join(f"+l{i}" for i in range(1, 2001))   # 2000 строк > кап
    patches = {"a.py": huge, "b.py": huge}
    deps = _deps(changed_paths=["a.py", "b.py"], patches=patches)
    out = _pr_bundle(deps, ["a.py", "b.py"])
    assert "--- a.py ---" not in out and "--- b.py ---" not in out
    assert "опущены" in out


def test_analyze_prompt_includes_other_file_diffs_bundle():
    captured: list = []

    class CaptureLLM:
        def invoke(self, messages):
            captured.extend(messages)
            return AIMessage(content='{"findings":[]}')
        def bind_tools(self, tools):
            return self

    class CapProvider:
        def chat_model_with_tools(self, tools, model=None):
            return CaptureLLM()
        def chat_model(self, model=None):
            return FinalLLM("FB")

    deps = _deps(changed_paths=["a.py", "b.py"],
                 patches={"a.py": "@@ -1 +1 @@\n+a", "b.py": "@@ -1 +1 @@\n-old\n+newbie"})
    LLMAnalyzer(CapProvider(), max_iterations=2).analyze(ReviewUnit("a.py", [], "code"), deps)
    human = _combine_human(captured)
    assert "Диффы других изменённых файлов PR" in human
    assert "--- b.py ---" in human and "+newbie" in human
    assert "--- a.py ---" not in human               # текущий файл не дублируется в bundle


# ---------------------------------------------------------------------------
# Verify budget + oneshot threshold (Task 2.4)
# ---------------------------------------------------------------------------


def test_verify_oneshot_threshold_forces_oneshot():
    """При >N findings agentic игнорируется и используется oneshot."""
    prov = FakeProvider(
        [],
        '{"verdicts":[{"index":0,"is_real":false},{"index":1,"is_real":true}]}',
    )
    v = LLMVerifier(
        prov, agentic=True, max_iterations=2, min_severity="low",
        oneshot_threshold=1,
    )
    out = v.verify([_finding(msg="a"), _finding(msg="b")], _deps())
    assert [f.message for f in out] == ["b"]
    # _verify_oneshot использует chat_model, а не chat_model_with_tools
    assert prov.chat_model_calls == 1
    assert prov.chat_model_with_tools_calls == 0


def test_verify_threshold_default_10_agentic_for_10():
    """Дефолт threshold=10: ровно 10 findings ещё можно agentic."""
    inline = '{"is_real": true}'
    prov = FakeProvider([AIMessage(content=inline)], "SHOULD NOT BE CALLED")
    v = LLMVerifier(prov, agentic=True, max_iterations=2, min_severity="low")
    findings = [_finding(severity="high", msg=f"bug{i}") for i in range(10)]
    out = v.verify(findings, _deps())
    assert len(out) == 10
    # chat_model_with_tools должен был вызываться 10 раз
    assert prov.chat_model_with_tools_calls == 10


def test_verify_threshold_default_10_oneshot_for_11():
    """Дефолт threshold=10: 11 findings переключают в oneshot."""
    verdicts = (
        '{"verdicts":['
        + ",".join(f'{{"index":{i},"is_real":true}}' for i in range(11))
        + ']}'
    )
    prov = FakeProvider([], verdicts)
    v = LLMVerifier(prov, agentic=True, max_iterations=2, min_severity="low")
    findings = [_finding(severity="high", msg=f"bug{i}") for i in range(11)]
    out = v.verify(findings, _deps())
    assert len(out) == 11
    # oneshot использует chat_model
    assert prov.chat_model_calls == 1
    assert prov.chat_model_with_tools_calls == 0


def test_verify_budget_exceeded_skips_verify():
    """При превышении бюджета токенов verify пропускается (fail-open)."""
    class BoomProvider:
        def chat_model_with_tools(self, tools, model=None):
            raise AssertionError("не должно вызываться")
        def chat_model(self, model=None):
            raise AssertionError("не должно вызываться")

    v = LLMVerifier(
        BoomProvider(), agentic=True, max_iterations=2, min_severity="low",
        max_verify_tokens=1,
    )
    usage = FakeUsageLog(total_tokens=1000)
    deps = _deps(usage=usage)
    out = v.verify([_finding(severity="high")], deps)
    assert len(out) == 1


def test_verify_budget_exceeded_gates_findings():
    """При превышении бюджета findings фильтруются через policy gate."""
    class FakePolicy:
        def gate(self, f):
            return f.severity == "high"

    class BoomProvider:
        def chat_model_with_tools(self, tools, model=None):
            raise AssertionError("не должно вызываться")
        def chat_model(self, model=None):
            raise AssertionError("не должно вызываться")

    v = LLMVerifier(
        BoomProvider(), agentic=True, max_iterations=2, min_severity="low",
        max_verify_tokens=1,
    )
    usage = FakeUsageLog(total_tokens=1000)
    deps = _deps(usage=usage, policy=FakePolicy())
    out = v.verify(
        [_finding(severity="high"), _finding(severity="low")], deps,
    )
    assert len(out) == 1
    assert out[0].severity == "high"


def test_verify_budget_ok_allows_agentic():
    """При достаточном бюджете agentic работает как обычно."""
    inline = '{"is_real": false}'
    prov = FakeProvider([AIMessage(content=inline)], "SHOULD NOT BE CALLED")
    v = LLMVerifier(
        prov, agentic=True, max_iterations=2, min_severity="low",
        max_verify_tokens=100000,
    )
    out = v.verify([_finding(severity="high")], _deps())
    assert out == []


def test_verify_budget_ok_allows_oneshot():
    """При достаточном бюджете oneshot работает как обычно."""
    prov = FakeProvider([], '{"verdicts":[{"index":0,"is_real":true}]}')
    v = LLMVerifier(
        prov, agentic=False, max_verify_tokens=100000,
    )
    out = v.verify([_finding(severity="high")], _deps())
    assert len(out) == 1


def test_verify_budget_zero_means_unlimited():
    """max_verify_tokens=0 означает отсутствие ограничения."""
    inline = '{"is_real": false}'
    prov = FakeProvider([AIMessage(content=inline)], "SHOULD NOT BE CALLED")
    v = LLMVerifier(
        prov, agentic=True, max_iterations=2, min_severity="low",
        max_verify_tokens=0,
    )
    # Даже при огромном usage total_tokens verify должен выполниться
    usage = FakeUsageLog(total_tokens=1_000_000)
    deps = _deps(usage=usage)
    out = v.verify([_finding(severity="high")], deps)
    assert out == []


# ---------------------------------------------------------------------------
# PR-bundle limits and prioritization (Task 2.3)
# ---------------------------------------------------------------------------


def test_pr_bundle_caps_files_and_prioritizes_signature_changes():
    """При превышении review_bundle_max_files остаются файлы с изменёнными сигнатурами."""
    patches = {
        "a.py": "@@ -1 +1 @@\n-x\n+y",
        "b.py": "@@ -1 +1 @@\n+def foo():",
        "c.py": "@@ -1 +1 @@\n-z\n+w",
    }
    sources = {p: "def foo():\n    pass" for p in patches}
    deps = _deps(
        changed_paths=list(patches),
        patches=patches,
        sources=sources,
        settings=Settings(review_bundle_max_files=1),
    )
    out = _pr_bundle(deps, list(patches))
    assert "--- b.py ---" in out
    assert "--- a.py ---" not in out
    assert "--- c.py ---" not in out
    assert "опущены" in out


def test_pr_bundle_caps_signature_lines():
    """При превышении review_bundle_max_lines карты сигнатур обрезаются,
    приоритет отдаётся файлам с изменёнными сигнатурами."""
    src_a = "\n".join(f"def func_{i}():\n    pass" for i in range(5))
    src_b = "def small():\n    pass"
    patches = {
        "a.py": "@@ -1 +1 @@\n-x\n+y",
        "b.py": "@@ -1 +1 @@\n+def bar():",
    }
    sources = {"a.py": src_a, "b.py": src_b}
    deps = _deps(
        changed_paths=["a.py", "b.py"],
        patches=patches,
        sources=sources,
        settings=Settings(review_bundle_max_lines=5),
    )
    static = _pr_bundle_static(deps, ["a.py", "b.py"])
    assert "Структура изменённых модулей:" in static
    maps_part = static.split("Структура изменённых модулей:")[1]
    assert "b.py:" in maps_part
    assert "a.py:" not in maps_part


def test_pr_bundle_defaults_when_no_settings():
    """Без settings используются дефолтные лимиты — все файлы помещаются."""
    patches = {
        "a.py": "@@ -1 +1 @@\n-x\n+y",
        "b.py": "@@ -1 +1 @@\n+def foo():",
    }
    sources = {p: "def foo():\n    pass" for p in patches}
    deps = _deps(changed_paths=list(patches), patches=patches, sources=sources)
    out = _pr_bundle(deps, list(patches))
    assert "--- a.py ---" in out
    assert "--- b.py ---" in out


# ---------------------------------------------------------------------------
# Truncation tool results
# ---------------------------------------------------------------------------

class _LongTool:
    def invoke(self, args):
        return "x" * 10000


class _FakeToolLLM:
    def __init__(self):
        self._called = False

    def invoke(self, messages):
        if not self._called:
            self._called = True
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "long_tool", "args": {}, "id": "t1", "type": "tool_call"}
                ],
            )
        return AIMessage(content="done")


def test_run_tool_loop_truncates_long_result():
    """Результат tool-вызова обрезается до max_tool_result_chars с маркером."""
    messages = []
    budget = BudgetTracker(max_iterations=3)
    _run_tool_loop(
        messages,
        _FakeToolLLM(),
        {"long_tool": _LongTool()},
        budget,
        max_tool_result_chars=100,
    )
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    content = tool_msgs[0].content
    assert len(content) <= 120
    assert "[...truncated]" in content


def test_run_tool_loop_does_not_truncate_when_under_limit():
    """Короткий результат не обрезается."""
    messages = []
    budget = BudgetTracker(max_iterations=3)
    _run_tool_loop(
        messages,
        _FakeToolLLM(),
        {"long_tool": _LongTool()},
        budget,
        max_tool_result_chars=20000,
    )
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert "[...truncated]" not in tool_msgs[0].content
    assert len(tool_msgs[0].content) == 10000


def test_synthesize_drops_add_finding_without_file():
    """add-находка без поля file отбрасывается, а не приписывается к findings[0].file
    (иначе кросс-файловая находка получает ложный file:line)."""
    final = ('{"keep": [0], "add": [{"category":"correctness","severity":"high",'
             '"line":42,"message":"fileless cross-file"}]}')
    prov = FakeProvider([AIMessage(content="done")], final)
    s = LLMSynthesizer(prov, max_iterations=2)
    out = s.synthesize([_finding(severity="high", msg="orig")], _deps())
    assert [f.message for f in out] == ["orig"]


def test_estimate_verify_tokens_agentic_scales_with_findings():
    """agentic-оценка растёт с числом проверяемых находок и выше oneshot для тех же
    находок — раньше всегда моделировался один oneshot-промпт (систематический недоучёт)."""
    v = LLMVerifier(object(), agentic=True, oneshot_threshold=100, min_severity="low")
    five = [_F(i) for i in range(5)]
    ten = [_F(i) for i in range(10)]
    assert v._estimate_verify_tokens(ten) > v._estimate_verify_tokens(five)

    v_oneshot = LLMVerifier(object(), agentic=False, oneshot_threshold=100)
    assert v._estimate_verify_tokens(five) > v_oneshot._estimate_verify_tokens(five)


# ---------------------------------------------------------------------------
# Task A1: тесты _resolve_line
# ---------------------------------------------------------------------------

def test_resolve_line_unique_exact_match():
    from reviewer.agent.analyzer import _resolve_line
    source = "def a():\n    x = 1\n    return compute(x)\n"
    assert _resolve_line("return compute(x)", source) == 3
    assert _resolve_line("    return compute(x)", source) == 3   # ведущие пробелы игнорируются


def test_resolve_line_ambiguous_returns_none():
    from reviewer.agent.analyzer import _resolve_line
    source = "x = 1\nx = 1\n"
    assert _resolve_line("x = 1", source) is None   # 2 совпадения -> не угадываем


def test_resolve_line_substring_fallback_unique():
    from reviewer.agent.analyzer import _resolve_line
    source = "alpha\n    result = compute(x) + 1\nbeta\n"
    assert _resolve_line("compute(x)", source) == 2   # уникальная подстрока


def test_resolve_line_empty_inputs():
    from reviewer.agent.analyzer import _resolve_line
    assert _resolve_line(None, "x = 1") is None
    assert _resolve_line("x = 1", None) is None
    assert _resolve_line("   ", "x = 1") is None


# ---------------------------------------------------------------------------
# Task A2: тесты code_quote + грунтовка в _to_findings
# ---------------------------------------------------------------------------

def test_to_findings_grounds_line_from_code_quote():
    from reviewer.agent.analyzer import _to_findings, _FindingModel
    source = "def a():\n    x = 1\n    return compute(x)\n"
    models = [_FindingModel(category="correctness", severity="high", message="m",
                            file="a.py", line=999, code_quote="return compute(x)")]
    out = _to_findings(models, default_file="a.py", sources={"a.py": source})
    assert out[0].line == 3   # реальный номер вместо выдуманного 999


def test_to_findings_keeps_model_line_when_quote_absent():
    from reviewer.agent.analyzer import _to_findings, _FindingModel
    models = [_FindingModel(category="correctness", severity="high", message="m",
                            file="a.py", line=7)]
    out = _to_findings(models, default_file="a.py", sources={"a.py": "x\ny\n"})
    assert out[0].line == 7   # нет code_quote -> номер модели не трогаем


def test_to_findings_grounding_is_optional_without_sources():
    from reviewer.agent.analyzer import _to_findings, _FindingModel
    models = [_FindingModel(category="correctness", severity="high", message="m",
                            file="a.py", line=42, code_quote="whatever")]
    out = _to_findings(models, default_file="a.py")   # sources не передан
    assert out[0].line == 42   # обратная совместимость


# ---------------------------------------------------------------------------
# Task A3: тест прокидывания sources через analyze
# ---------------------------------------------------------------------------

def test_analyze_grounds_line_via_code_quote(monkeypatch):
    from reviewer.agent.analyzer import LLMAnalyzer
    src = "def a():\n    x = 1\n    return compute(x)\n"
    unit = ReviewUnit("a.py", ["a.py#a"], "@@ -1,3 +1,3 @@", new_source=src)
    out_json = ('{"findings": [{"category":"correctness","severity":"high",'
                '"line":999,"code_quote":"return compute(x)","message":"bug"}]}')
    prov = FakeProvider([AIMessage(content=out_json)], "SHOULD NOT BE CALLED")
    deps = _deps()
    deps.sources = {"a.py": src}
    a = LLMAnalyzer(prov, max_iterations=2)
    out = a.analyze(unit, deps)
    assert len(out) == 1
    assert out[0].line == 3   # грунтовка сработала, а не 999


# ---------------------------------------------------------------------------
# Task A4: тест наличия code_quote в схемах
# ---------------------------------------------------------------------------

def test_findings_schema_mentions_code_quote():
    from reviewer.agent.analyzer import _FINDINGS_SCHEMA, _SYNTH_SCHEMA
    assert "code_quote" in _FINDINGS_SCHEMA
    assert "code_quote" in _SYNTH_SCHEMA


def test_to_findings_grounds_line_when_model_gave_null():
    from reviewer.agent.analyzer import _to_findings, _FindingModel
    source = "def a():\n    x = 1\n    return compute(x)\n"
    models = [_FindingModel(category="correctness", severity="high", message="m",
                            file="a.py", line=None, code_quote="return compute(x)")]
    out = _to_findings(models, default_file="a.py", sources={"a.py": source})
    assert out[0].line == 3   # грунтовка по цитате дала номер там где модель отдала null
