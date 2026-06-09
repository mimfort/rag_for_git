from reviewer.agent.graph import build_graph
from reviewer.agent.nodes import make_verify_node, make_synthesize_node, make_assemble_node
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
              "findings": [], "failed_units": [], "verified": [], "summary": "", "inline_comments": []})
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
                              "findings": [], "failed_units": [], "verified": [], "summary": "",
                              "inline_comments": []})
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
                              "findings": [], "failed_units": [], "verified": [], "summary": "",
                              "inline_comments": []})

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
                              "findings":[],"failed_units":[],"verified":[],"summary":"",
                              "inline_comments":[]})
    summary, comments = vcs.published
    assert comments == [] and "far away" in summary


def _min_deps(**over):
    base = dict(vcs=None, retriever=None, graph=None, policy=None, analyzer=None,
                verifier=None, pr_number=1, head_sha="s", overlay_ref="pr:1",
                changed_paths=[], patches={})
    base.update(over)
    return Deps(**base)


def test_graph_includes_synthesize_when_synthesizer_present():
    g = build_graph(_min_deps(synthesizer=object()))
    assert "synthesize" in g.get_graph().nodes


def test_graph_skips_synthesize_when_no_synthesizer():
    g = build_graph(_min_deps(synthesizer=None))
    assert "synthesize" not in g.get_graph().nodes


# ---------------------------------------------------------------------------
# Тесты порядка gate → verify и фильтрации синтезатора
# ---------------------------------------------------------------------------

def _make_finding(category="correctness", severity="high", confidence=0.9, file="a.py"):
    return Finding(category, severity, file, 1, "RIGHT", "msg", None, confidence)


def _node_deps(policy, verifier, synthesizer=None):
    """Минимальный Deps для изолированного теста узлов verify/synthesize."""
    return Deps(
        vcs=None, retriever=None, graph=None,
        policy=policy,
        analyzer=None,
        verifier=verifier,
        pr_number=1, head_sha="s", overlay_ref="pr:1",
        changed_paths=[], patches={},
        synthesizer=synthesizer,
    )


class _RecordingVerifier:
    """Запоминает, что передали в verify; пропускает все находки (pass-through)."""
    def __init__(self):
        self.received: list = []

    def verify(self, findings, deps):
        self.received = list(findings)
        return findings


def test_verify_node_gate_before_verifier():
    """Gate запускается до verifier: находка, не проходящая gate, в verifier не попадает."""
    # Политика пропускает только severity=high; low-находка должна быть отброшена до verifier
    policy = ReviewPolicy(severity_threshold="high")
    verifier = _RecordingVerifier()
    deps = _node_deps(policy, verifier)

    high_finding = _make_finding(severity="high")
    low_finding = _make_finding(severity="low")

    node = make_verify_node(deps)
    result = node({"findings": [high_finding, low_finding]})

    # verifier получил только high-находку
    assert low_finding not in verifier.received
    assert high_finding in verifier.received
    # в результате — только high-находка
    assert result["verified"] == [high_finding]


def test_verify_node_passes_confirmed_finding():
    """Находка, прошедшая gate и подтверждённая verifier, сохраняется в verified."""
    policy = ReviewPolicy()   # пропускает всё
    verifier = _RecordingVerifier()
    deps = _node_deps(policy, verifier)

    f = _make_finding()
    node = make_verify_node(deps)
    result = node({"findings": [f]})

    assert result["verified"] == [f]


def test_synthesize_node_filters_add_finding_via_gate():
    """add-находка синтезатора, не проходящая gate, отбрасывается."""
    policy = ReviewPolicy(severity_threshold="high")  # low не пройдёт

    good_finding = _make_finding(severity="high")
    bad_finding = _make_finding(severity="low")

    class _Synthesizer:
        def synthesize(self, verified, deps):
            # добавляет новую low-находку к уже подтверждённым
            return list(verified) + [bad_finding]

    deps = _node_deps(policy, verifier=None, synthesizer=_Synthesizer())
    node = make_synthesize_node(deps)
    result = node({"verified": [good_finding]})

    assert good_finding in result["verified"]
    assert bad_finding not in result["verified"]


def test_synthesize_node_keeps_passing_add_finding():
    """add-находка синтезатора, прошедшая gate, остаётся в verified."""
    policy = ReviewPolicy()   # пропускает всё

    existing = _make_finding(severity="high")
    new_finding = _make_finding(severity="high", file="b.py")

    class _Synthesizer:
        def synthesize(self, verified, deps):
            return list(verified) + [new_finding]

    deps = _node_deps(policy, verifier=None, synthesizer=_Synthesizer())
    node = make_synthesize_node(deps)
    result = node({"verified": [existing]})

    assert existing in result["verified"]
    assert new_finding in result["verified"]


# ---------------------------------------------------------------------------
# Тесты dedup в verify-узле и логирования публикаций
# ---------------------------------------------------------------------------

def test_verify_node_dedup_before_verifier():
    """Два одинаковых finding (file/line/category/message) → verifier получает один."""
    policy = ReviewPolicy()   # пропускает всё
    verifier = _RecordingVerifier()
    deps = _node_deps(policy, verifier)

    # Два идентичных finding с одинаковым file/line/category/message
    f1 = Finding("correctness", "high", "a.py", 5, "RIGHT", "ошибка X", None, 0.9)
    f2 = Finding("correctness", "high", "a.py", 5, "RIGHT", "ошибка X", None, 0.8)

    node = make_verify_node(deps)
    node({"findings": [f1, f2]})

    # После dedup verifier должен получить ровно одну находку
    assert len(verifier.received) == 1


class _FakeVerdictLog:
    """Фейковый VerdictLog: записывает вызовы log_published."""
    def __init__(self):
        self.calls: list[tuple] = []   # [(finding, inline), ...]

    def log_published(self, finding, inline: bool) -> None:
        self.calls.append((finding, inline))


def _assemble_deps(vcs, policy=None, verdicts=None):
    """Deps для изолированного теста make_assemble_node."""
    if policy is None:
        policy = ReviewPolicy()
    return Deps(
        vcs=vcs, retriever=None, graph=None,
        policy=policy,
        analyzer=None,
        verifier=None,
        pr_number=1, head_sha="s", overlay_ref="pr:1",
        changed_paths=["a.py"],
        patches={"a.py": "@@ -1,2 +1,2 @@\n x\n+y\n"},
        verdicts=verdicts,
    )


def test_log_published_inline_finding():
    """Inline-находка логируется с inline=True."""
    vcs = FakeVCS()
    vlog = _FakeVerdictLog()
    deps = _assemble_deps(vcs, verdicts=vlog)

    # Строка 2 есть в диффе (RIGHT)
    f = Finding("correctness", "high", "a.py", 2, "RIGHT", "bug inline", None, 0.9)
    node = make_assemble_node(deps)
    node({"verified": [f]})

    assert len(vlog.calls) == 1
    assert vlog.calls[0] == (f, True)


def test_log_published_summary_finding():
    """Находка вне диффа уходит в сводку и логируется с inline=False."""
    vcs = FakeVCS()
    vlog = _FakeVerdictLog()
    deps = _assemble_deps(vcs, verdicts=vlog)

    # Строка 999 вне диффа → попадёт в сводку
    f = Finding("correctness", "high", "a.py", 999, "RIGHT", "bug in summary", None, 0.8)
    node = make_assemble_node(deps)
    node({"verified": [f]})

    assert len(vlog.calls) == 1
    assert vlog.calls[0] == (f, False)


def test_log_published_skips_existing_fingerprint():
    """Находка с фингерпринтом из предыдущего прогона не логируется."""
    f = Finding("correctness", "high", "a.py", 2, "RIGHT", "already there", None, 0.9)

    class _VCSWithExisting:
        def list_existing_fingerprints(self, n):
            return {f.fingerprint()}
        def publish_review(self, *args, **kwargs):
            pass

    vcs = _VCSWithExisting()
    vlog = _FakeVerdictLog()
    deps = _assemble_deps(vcs, verdicts=vlog)

    node = make_assemble_node(deps)
    node({"verified": [f]})

    assert vlog.calls == []


def test_assemble_without_verdicts_does_not_raise():
    """deps.verdicts = None → ничего не падает."""
    vcs = FakeVCS()
    deps = _assemble_deps(vcs, verdicts=None)

    f = Finding("correctness", "high", "a.py", 2, "RIGHT", "тест без лога", None, 0.9)
    node = make_assemble_node(deps)
    # Не должно бросать исключений
    node({"verified": [f]})


# ---------------------------------------------------------------------------
# Тесты fail-soft analyze, skipped_paths и чистая сводка
# ---------------------------------------------------------------------------

class _ErrorAnalyzer:
    """Анализатор, бросающий исключение для файла с именем 'bad.py', иначе возвращает одну находку."""
    def analyze(self, unit, deps):
        if unit.path == "bad.py":
            raise RuntimeError("намеренная ошибка")
        return [Finding("correctness", "high", unit.path, 2, "RIGHT", f"bug in {unit.path}", None, 0.9)]


def _two_file_deps(vcs):
    """Deps с двумя файлами: a.py (успех) и bad.py (ошибка)."""
    return Deps(
        vcs=vcs, retriever=None, graph=None, policy=ReviewPolicy(),
        analyzer=_ErrorAnalyzer(), verifier=PassVerifier(),
        pr_number=1, head_sha="sha", overlay_ref="pr:1",
        changed_paths=["a.py", "bad.py"],
        patches={
            "a.py": "@@ -1,2 +1,2 @@\n x\n+y\n",
            "bad.py": "@@ -1,2 +1,2 @@\n x\n+y\n",
        },
    )


def test_analyze_fail_soft_one_file_does_not_crash_graph():
    """Ошибка анализа одного файла не валит граф; находки второго публикуются,
    в summary присутствует секция «Не проанализировано» с путём и типом ошибки."""
    vcs = FakeVCS()
    deps = _two_file_deps(vcs)
    g = build_graph(deps)
    g.invoke({
        "review_units": [
            ReviewUnit("a.py", ["a.py#f"], "y"),
            ReviewUnit("bad.py", ["bad.py#f"], "y"),
        ],
        "findings": [], "failed_units": [], "verified": [], "summary": "", "inline_comments": [],
    })
    summary, comments = vcs.published
    # Находка из a.py опубликована
    assert any(c.path == "a.py" for c in comments)
    # Секция ошибок присутствует в сводке
    assert "Не проанализировано" in summary
    assert "bad.py" in summary
    assert "RuntimeError" in summary


def test_skipped_paths_appear_in_summary():
    """deps.skipped_paths=[...] → в summary секция про лимит с обоими путями."""
    vcs = FakeVCS()
    deps = _deps(vcs)
    deps.skipped_paths = ["a.py", "b.py"]
    build_graph(deps).invoke({
        "review_units": [ReviewUnit("a.py", ["a.py#f"], "y")],
        "findings": [], "failed_units": [], "verified": [], "summary": "", "inline_comments": [],
    })
    summary, _ = vcs.published
    assert "Пропущено по лимиту файлов" in summary
    assert "a.py" in summary
    assert "b.py" in summary


def test_clean_summary_has_no_error_or_skipped_sections():
    """Без ошибок и без skipped_paths — секции «Не проанализировано» и «Пропущено» отсутствуют."""
    vcs = FakeVCS()
    deps = _deps(vcs)
    build_graph(deps).invoke({
        "review_units": [ReviewUnit("a.py", ["a.py#f"], "y")],
        "findings": [], "failed_units": [], "verified": [], "summary": "", "inline_comments": [],
    })
    summary, _ = vcs.published
    assert "Не проанализировано" not in summary
    assert "Пропущено по лимиту файлов" not in summary
