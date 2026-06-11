from __future__ import annotations
import json
import logging
import re
from pydantic import BaseModel, Field
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage, ToolMessage

from reviewer.agent.state import ReviewUnit, Deps
from reviewer.agent.prompts import ANALYZE_SYSTEM, VERIFY_SYSTEM, SYNTHESIZE_SYSTEM
from reviewer.tools.code_tools import make_tools, ToolContext
from reviewer.vcs.base import Finding
from reviewer.llm.budget import BudgetTracker, BudgetExceeded
from reviewer.llm._retry import with_llm_retry
from reviewer.index.chunker import chunk_python

_log = logging.getLogger(__name__)

# Заголовок хунка unified diff: @@ -a,b +c,d @@ (b/d опциональны — тогда =1).
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

_VALID_SEVERITY = {"low", "medium", "high", "critical"}
_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_VERDICT_ONE_SCHEMA = 'Верни СТРОГО один JSON-объект: {"is_real": true|false}'

_SYNTH_SCHEMA = (
    'Верни СТРОГО один JSON-объект без пояснений и markdown:\n'
    '{"keep": [<индексы исходных находок, которые оставить, как в списке выше>], '
    '"add": [{"file": "<путь>", "category": "correctness|security|performance|style", '
    '"severity": "low|medium|high|critical", "line": <int|null>, '
    '"code_quote": "<дословная строка кода проблемы из указанного файла>", "message": "...", '
    '"suggestion": "... или null", "confidence": 0.0}]}\n'
    'keep — индексы исходных находок, которые остаются (исключи дубли/неверные, не переписывай их). '
    'add — ТОЛЬКО новые кросс-файловые проблемы, не входящие в исходный список '
    '(рассогласование сигнатура↔вызовы и т.п.). Поле file обязательно у каждой add-находки. '
    'Если новых проблем нет — "add": []. Если все исходные верны — перечисли все их индексы в keep.'
)

_FINDINGS_SCHEMA = (
    'Верни СТРОГО один JSON-объект без пояснений и без markdown-обёртки:\n'
    '{"findings": [{"category": "correctness|security|performance|style", '
    '"severity": "low|medium|high|critical", '
    '"line": <номер строки в НОВОЙ версии файла или null>, '
    '"code_quote": "<точная строка кода, к которой относится проблема — '
    'скопируй её ДОСЛОВНО из показанной новой версии файла>", '
    '"message": "...", "suggestion": "... или null", '
    '"fix": {"start_line": <int>, "end_line": <int>, '
    '"replacement": "<точный новый код для строк start_line..end_line НОВОЙ версии, '
    'дословно, с правильными отступами, без markdown-обёртки>"}, '
    '"confidence": 0.0}]}\n'
    'fix указывай ТОЛЬКО когда можешь дать точную замену непрерывного диапазона строк '
    'новой версии (start_line ≤ end_line по показанной нумерации), а replacement — '
    'буквальное новое содержимое ИМЕННО этих строк. Если точную замену дать нельзя — '
    'ставь "fix": null и опиши словами в suggestion. Номера строк бери из показанной новой версии.\n'
    'Если реальных проблем нет — верни {"findings": []}.'
)
_VERDICT_SCHEMA = (
    'Для КАЖДОГО пункта реши, реальная ли это проблема. Верни СТРОГО JSON:\n'
    '{"verdicts": [{"index": <номер пункта>, "is_real": true|false}]}'
)


def _cacheable(text: str, enabled: bool):
    """Содержимое сообщения с anthropic cache_control (prompt caching через OpenRouter).
    Кэш-префикс стабилен между итерациями tool-loop — история только дописывается."""
    if not enabled:
        return text
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _text_of(msg) -> str:
    c = getattr(msg, "content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return str(c)


def _numbered(source: str, limit: int = 1200) -> str:
    """Новая версия файла с номерами строк (1-based) для точных fix-диапазонов.
    Слишком большие файлы пропускаем (модель тогда даёт только текстовый совет)."""
    lines = source.splitlines()
    if not lines or len(lines) > limit:
        return ""
    return "\n".join(f"{i}|{ln}" for i, ln in enumerate(lines, 1))


def _window(source: str, line: int, radius: int = 25) -> str:
    """Пронумерованное окно строк вокруг указанной строки (1-based), обрезанное по границам файла."""
    lines = source.splitlines()
    if not lines:
        return ""
    # line — 1-based, перевести в 0-based индекс
    idx = line - 1
    start = max(0, idx - radius)
    end = min(len(lines), idx + radius + 1)
    return "\n".join(f"{i + 1}|{ln}" for i, ln in enumerate(lines[start:end], start))


def _resolve_line(quote: str | None, source: str | None) -> int | None:
    """Настоящий 1-based номер строки, текст которой совпадает с quote.

    Модель часто путает номер строки (особенно для символа из другого модуля),
    но цитирует код верно. Сопоставляем по содержимому, игнорируя ведущие/хвостовые
    пробелы. Возвращаем номер ТОЛЬКО при единственном совпадении (иначе None —
    не привязываем к чужой строке). Фолбэк — уникальная подстрока."""
    if not quote or not source:
        return None
    needle = quote.strip()
    if not needle:
        return None
    lines = source.splitlines()
    exact = [i for i, ln in enumerate(lines, 1) if ln.strip() == needle]
    if len(exact) == 1:
        return exact[0]
    if not exact:
        sub = [i for i, ln in enumerate(lines, 1) if needle in ln]
        if len(sub) == 1:
            return sub[0]
    return None


_FILE_FULL_LIMIT = 400      # ≤ этого числа строк показываем файл целиком
_WINDOWS_LINE_CAP = 1500    # суммарный кап строк во всех окнах


def _hunk_ranges(changed_text: str) -> list[tuple[int, int]]:
    """Диапазоны новой версии [c, c+max(d,1)-1] из заголовков хунков unified diff."""
    ranges: list[tuple[int, int]] = []
    for line in changed_text.splitlines():
        m = _HUNK_HEADER.match(line)
        if not m:
            continue
        c = int(m.group(1))
        d = int(m.group(2)) if m.group(2) is not None else 1
        ranges.append((c, c + max(d, 1) - 1))
    return ranges


def _merge_ranges(ranges: list[tuple[int, int]], total: int,
                  radius: int, gap: int = 10) -> list[tuple[int, int]]:
    """Расширить каждый диапазон на ±radius, обрезать по [1, total], слить
    пересекающиеся/смежные (зазор ≤ gap) в отсортированном порядке."""
    expanded: list[tuple[int, int]] = []
    for lo, hi in ranges:
        s = max(1, lo - radius)
        e = min(total, hi + radius)
        if s <= e:
            expanded.append((s, e))
    expanded.sort()
    merged: list[tuple[int, int]] = []
    for s, e in expanded:
        if merged and s <= merged[-1][1] + gap + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _render_windows(lines: list[str], windows: list[tuple[int, int]]) -> str:
    """Отрисовать окна с реальной нумерацией N|код; между окнами — маркер пропуска."""
    parts: list[str] = []
    prev_end = 0
    for s, e in windows:
        if prev_end and s > prev_end + 1:
            parts.append(f"… (строки {prev_end + 1}–{s - 1} пропущены)")
        parts.append("\n".join(f"{i}|{lines[i - 1]}" for i in range(s, e + 1)))
        prev_end = e
    return "\n".join(parts)


def _module_signatures(path: str, source: str) -> str:
    """Список символов модуля (kind fqn (строки start–end)) без тел — структура файла."""
    try:
        chunks = chunk_python(path, source.encode("utf-8"))
    except Exception:
        return ""
    lines = [f"{c.kind} {c.symbol_fqn} (строки {c.start_line}–{c.end_line})" for c in chunks]
    return "\n".join(lines)


def _file_context(unit: ReviewUnit) -> str:
    """Адаптивный контекст новой версии файла для analyze.

    ≤ 400 строк — весь файл с нумерацией N|код. Больше — «окна вокруг изменений»
    (диапазоны хунков ±50, слитые) плюс сигнатуры модуля; нумерация реальная,
    между окнами — маркеры пропусков. Суммарный кап окон — 1500 строк."""
    source = unit.new_source
    lines = source.splitlines()
    total = len(lines)
    if total == 0:
        return ""
    if total <= _FILE_FULL_LIMIT:
        return _numbered(source)

    ranges = _hunk_ranges(unit.changed_text)
    if not ranges:
        # Нет/кривой дифф — fallback: первые 400 строк целиком.
        head = min(_FILE_FULL_LIMIT, total)
        return "\n".join(f"{i}|{lines[i - 1]}" for i in range(1, head + 1))

    truncated = False
    windows = _merge_ranges(ranges, total, radius=50)
    if sum(e - s + 1 for s, e in windows) > _WINDOWS_LINE_CAP:
        windows = _merge_ranges(ranges, total, radius=20)
        truncated = True
    # Если и при меньшем радиусе перебор — обрезаем самые поздние окна.
    while windows and sum(e - s + 1 for s, e in windows) > _WINDOWS_LINE_CAP:
        windows.pop()
        truncated = True

    parts: list[str] = []
    sigs = _module_signatures(unit.path, source)
    if sigs:
        parts.append("Структура модуля (сигнатуры):\n" + sigs)
    parts.append(_render_windows(lines, windows))
    if truncated:
        parts.append("(часть контекста опущена)")
    return "\n\n".join(parts)


def _signature_changes(patches: dict[str, str | None]) -> str:
    """Изменённые сигнатуры (def/class/async def) в патчах PR, сгруппированные по файлу.

    Возвращает блок вида:
        file.py:
          - def connect(host, port):
          + def connect(host, port, timeout):
    Если изменений сигнатур нет — пустая строка."""
    blocks: list[str] = []
    for path, patch in (patches or {}).items():
        if not patch:
            continue
        sig_lines: list[str] = []
        for line in patch.splitlines():
            if not line or line[0] not in "+-":
                continue
            if line.startswith("---") or line.startswith("+++"):
                continue
            body = line[1:].lstrip()
            if body.startswith(("def ", "class ", "async def ")):
                sig_lines.append(f"{line[0]} {body}")
        if sig_lines:
            blocks.append(f"{path}:\n" + "\n".join(f"  {sl}" for sl in sig_lines))
    return "\n".join(blocks)


def _paths_with_signature_changes(patches: dict[str, str | None]) -> set[str]:
    """Множество путей, в патчах которых есть изменения сигнатур."""
    out: set[str] = set()
    for path, patch in (patches or {}).items():
        if not patch:
            continue
        for line in patch.splitlines():
            if not line or line[0] not in "+-":
                continue
            if line.startswith("---") or line.startswith("+++"):
                continue
            body = line[1:].lstrip()
            if body.startswith(("def ", "class ", "async def ")):
                out.add(path)
                break
    return out


_BUNDLE_LINE_CAP = 1500     # суммарный кап строк диффов в PR-bundle


def _pr_bundle_static(deps, changed_paths: list[str]) -> str:
    """Path-независимая часть PR-bundle: изменённые сигнатуры + карты сигнатур модулей.
    Одинакова для всех файлов прогона -> считается один раз и кэшируется в deps.tool_cache."""
    parts: list[str] = []
    patches = getattr(deps, "patches", None) or {}
    sig_changes = _signature_changes(patches)
    if sig_changes:
        parts.append("Изменённые сигнатуры в PR:\n" + sig_changes)
    sources = getattr(deps, "sources", None) or {}
    settings = getattr(deps, "settings", None)
    max_sig_lines = (
        getattr(settings, "review_bundle_max_lines", 1500) if settings else 1500
    )
    sig_change_paths = _paths_with_signature_changes(patches)
    ordered_paths = sorted(changed_paths, key=lambda p: (p not in sig_change_paths, p))
    sig_maps: list[str] = []
    used = sig_changes.count("\n") + 1 if sig_changes else 0
    for path in ordered_paths:
        src = sources.get(path)
        if not src:
            continue
        sigs = _module_signatures(path, src)
        if not sigs:
            continue
        sig_lines = sigs.count("\n") + 1
        if used + sig_lines > max_sig_lines:
            break
        used += sig_lines
        sig_maps.append(f"{path}:\n{sigs}")
    if sig_maps:
        parts.append("Структура изменённых модулей:\n" + "\n\n".join(sig_maps))
    return "\n\n".join(parts)


def _pr_bundle(deps, changed_paths: list[str], current_path: str | None = None) -> str:
    """Компактный обзор PR для предзагрузки в промпт: диффы изменённых файлов
    (кроме current_path), изменённые сигнатуры и карты сигнатур модулей.

    Диффы режутся по числу файлов и суммарному капу строк;
    остаток помечается (даже если в кап не влез ни один файл).
    При превышении лимитов приоритет отдаётся файлам с изменёнными сигнатурами.
    Path-независимая часть кэшируется в deps.tool_cache."""
    patches = getattr(deps, "patches", None) or {}
    settings = getattr(deps, "settings", None)
    max_files = (
        getattr(settings, "review_bundle_max_files", 50) if settings else 50
    )
    sig_change_paths = _paths_with_signature_changes(patches)
    ordered = sorted(changed_paths, key=lambda p: (p not in sig_change_paths, p))
    parts: list[str] = []

    diff_blocks: list[str] = []
    used = 0
    used_files = 0
    omitted = 0
    for path in ordered:
        if path == current_path:
            continue
        patch = patches.get(path)
        if not patch:
            continue
        plines = patch.splitlines()
        if used_files >= max_files or used + len(plines) > _BUNDLE_LINE_CAP:
            omitted += 1
            continue
        used_files += 1
        used += len(plines)
        diff_blocks.append(f"--- {path} ---\n{patch}")
    if diff_blocks:
        head = ("Диффы других изменённых файлов PR:" if current_path
                else "Диффы изменённых файлов PR:")
        parts.append(head + "\n" + "\n\n".join(diff_blocks))
    if omitted:
        parts.append(f"(ещё {omitted} файлов опущены — "
                     "используй get_changed_file_diff при необходимости)")

    cache = getattr(deps, "tool_cache", None)
    if cache is not None:
        key = ("__pr_bundle_static__",)
        if key not in cache:
            cache[key] = _pr_bundle_static(deps, ordered)
        static = cache[key]
    else:
        static = _pr_bundle_static(deps, ordered)
    if static:
        parts.append(static)

    return "\n\n".join(parts)


def _run_tool_loop(
    messages: list, llm, tools_by_name: dict, budget,
    usage=None, stage: str = "",
    trace=None, unit: str = "",
    max_tool_result_chars: int = 8000,
) -> list:
    """Гоняет tool-loop до отсутствия tool_calls или исчерпания бюджета.
    Мутирует и возвращает messages (добавляет AI- и ToolMessage). При BudgetExceeded —
    мягко выходит, оставляя накопленную историю для финального структурного запроса."""
    try:
        while True:
            budget.tick()
            ai = with_llm_retry(lambda: llm.invoke(messages))
            if usage is not None:
                usage.add(stage, ai)
            if trace is not None:
                trace.record_llm_call(stage, unit, ai)
            messages.append(ai)
            if not ai.tool_calls:
                break
            for call in ai.tool_calls:
                tool = tools_by_name.get(call["name"])
                try:
                    result = (tool.invoke(call["args"]) if tool
                              else f"(неизвестный инструмент: {call['name']})")
                except Exception as e:
                    result = f"(ошибка инструмента {call['name']}: {e})"
                if trace is not None:
                    trace.record_tool_call(stage, unit, call["name"], call["args"], result)
                result_text = str(result)
                if max_tool_result_chars > 0 and len(result_text) > max_tool_result_chars:
                    result_text = result_text[:max_tool_result_chars] + "\n[...truncated]"
                messages.append(ToolMessage(result_text, tool_call_id=call["id"]))
    except BudgetExceeded:
        pass
    return messages


def _extract_json(text: str) -> dict:
    """Достаёт первый JSON-объект из ответа модели (с markdown-фенсами или без)."""
    if not text:
        return {}
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        candidate = m.group(1)
    elif "{" in text and "}" in text:
        candidate = text[text.find("{"):text.rfind("}") + 1]
    else:
        return {}
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _last_ai_json(messages: list) -> dict:
    """Извлечь JSON из последнего AIMessage без tool_calls (иначе вернуть {})."""
    if not messages:
        return {}
    last = messages[-1]
    if not isinstance(last, AIMessage):
        return {}
    if getattr(last, "tool_calls", None):
        return {}
    return _extract_json(_text_of(last))


class _Fix(BaseModel):
    start_line: int | None = None
    end_line: int | None = None
    replacement: str | None = None

class _FindingModel(BaseModel):
    category: str
    file: str | None = None
    severity: str = Field(description="low|medium|high|critical")
    line: int | None = None
    code_quote: str | None = None
    message: str
    suggestion: str | None = None
    fix: _Fix | None = None
    confidence: float = 0.7

class _Findings(BaseModel):
    findings: list[_FindingModel] = Field(default_factory=list)

class _SynthDecision(BaseModel):
    keep: list[int] = Field(default_factory=list)
    add: list[_FindingModel] = Field(default_factory=list)

def _to_findings(models, default_file: str | None,
                 sources: dict[str, str] | None = None) -> list[Finding]:
    """Преобразовать распарсенные модели в Finding. file берётся из модели либо default.

    Если default_file=None (синтез) и модель не указала file — находку пропускаем:
    кросс-файловую находку без файла нельзя достоверно локализовать.
    Если передан sources и модель дала code_quote — номер строки грунтуем по реальному
    коду (модель часто путает номера); иначе оставляем line как есть."""
    out: list[Finding] = []
    for f in models:
        file = f.file or default_file
        if not file:
            continue
        line = f.line
        if sources is not None:
            resolved = _resolve_line(getattr(f, "code_quote", None), sources.get(file))
            if resolved is not None:
                line = resolved
        fs = f.fix.start_line if f.fix else None
        fe = f.fix.end_line if f.fix else None
        rp = f.fix.replacement if f.fix else None
        if rp is not None and (fs is None or fe is None):
            rp = None
        out.append(Finding(
            category=f.category,
            severity=(f.severity if f.severity in _VALID_SEVERITY else "medium"),
            file=file, line=line, side="RIGHT", message=f.message,
            suggestion=f.suggestion, confidence=f.confidence,
            fix_start=fs, fix_end=fe, replacement=rp))
    return out


def _pr_context(deps, changed_paths: list[str]) -> str:
    """Префикс human-промпта: интент PR + манифест изменённых файлов."""
    parts: list[str] = []
    if getattr(deps, "pr_title", ""):
        parts.append(f"Заголовок PR: {deps.pr_title}")
    if getattr(deps, "pr_body", ""):
        parts.append(f"Описание PR: {deps.pr_body[:1500]}")
    status = getattr(deps, "changed_status", None) or {}
    manifest = "\n".join(f"  - {p} ({status.get(p, 'modified')})" for p in changed_paths)
    if manifest:
        parts.append("Изменённые файлы PR:\n" + manifest)
    return "\n".join(parts)


class _Verdict(BaseModel):
    index: int
    is_real: bool

class _VerdictBatch(BaseModel):
    verdicts: list[_Verdict] = Field(default_factory=list)


class _LLMPhase:
    """Базовый класс для LLM-фаз агента (analyze/verify/synthesize).

    Хранит общие параметры и предоставляет фабрику ToolContext из Deps.
    Подклассы переопределяют только промпт + парсинг ответа."""

    def __init__(
        self,
        llm_provider,
        *,
        prompt_cache: bool = False,
        max_tool_result_chars: int = 8000,
    ):
        self.provider = llm_provider
        self.prompt_cache = prompt_cache
        self.max_tool_result_chars = max_tool_result_chars

    def _make_tool_context(self, deps: Deps, changed_node_ids: list[str] | None = None) -> ToolContext:
        return ToolContext(
            retriever=deps.retriever,
            graph=deps.graph,
            overlay_ref=deps.overlay_ref,
            changed_paths=deps.changed_paths,
            changed_node_ids=changed_node_ids or [],
            read_file_fn=(
                (lambda p: deps.vcs.get_file_at_ref(p, deps.head_sha))
                if deps.vcs else None
            ),
            patches=deps.patches,
            store=getattr(deps.retriever, "store", None),
            cache=getattr(deps, "tool_cache", None),
        )

    def _run_loop(self, messages, llm, tools_by_name, budget, deps, *,
                  stage: str, unit: str = ""):
        return _run_tool_loop(
            messages, llm, tools_by_name, budget,
            usage=deps.usage, stage=stage,
            trace=getattr(deps, "trace", None), unit=unit,
            max_tool_result_chars=self.max_tool_result_chars,
        )


class LLMAnalyzer(_LLMPhase):
    """Tool-loop + структурированный вывод findings для одного файла."""
    def __init__(self, llm_provider, max_iterations: int, prompt_cache: bool = False,
                 max_tool_result_chars: int = 8000):
        super().__init__(llm_provider, prompt_cache=prompt_cache,
                         max_tool_result_chars=max_tool_result_chars)
        self.max_iterations = max_iterations

    def analyze(self, unit: ReviewUnit, deps: Deps) -> list[Finding]:
        ctx = self._make_tool_context(deps, changed_node_ids=unit.node_ids)
        tools = make_tools(ctx)
        llm = self.provider.chat_model_with_tools(tools)
        tools_by_name = {t.name: t for t in tools}
        budget = BudgetTracker(self.max_iterations)
        ctx_text = _file_context(unit)
        pr_ctx = _pr_context(deps, deps.changed_paths)
        human = (pr_ctx + "\n\n") if pr_ctx else ""
        bundle = _pr_bundle(deps, deps.changed_paths, current_path=unit.path)
        if bundle:
            human += bundle + "\n\n"
        human += f"Файл: {unit.path}\n"
        if ctx_text:
            total = len(unit.new_source.splitlines())
            if total <= _FILE_FULL_LIMIT:
                human += f"Новая версия файла (с номерами строк N|код):\n{ctx_text}\n\n"
            else:
                human += ("Контекст новой версии файла (нумерация строк реальная; "
                          f"fix указывай только для показанных строк):\n{ctx_text}\n\n")
        human += f"Изменения (дифф):\n{unit.changed_text}"
        human += f"\n\nКогда закончишь работу с инструментами, верни итог в формате:\n{_FINDINGS_SCHEMA}"
        _trace = getattr(deps, "trace", None)
        messages = [
            SystemMessage(_cacheable(ANALYZE_SYSTEM, self.prompt_cache)),
            HumanMessage(_cacheable(human, self.prompt_cache)),
        ]
        if _trace is not None:
            _trace.record_prompt("analyze", unit.path, human)
        self._run_loop(messages, llm, tools_by_name, budget, deps,
                       stage="analyze", unit=unit.path)
        # Пробуем достать JSON из последнего AI-ответа без доп. вызова
        data = _last_ai_json(messages)
        if "findings" in data:
            try:
                parsed = _Findings(**data)
                return _to_findings(parsed.findings, default_file=unit.path,
                                    sources=deps.sources)
            except Exception:
                pass
        # Fallback: отдельный invoke со схемой
        _fallback_msgs = messages + [HumanMessage(_FINDINGS_SCHEMA)]
        resp = with_llm_retry(lambda: self.provider.chat_model().invoke(_fallback_msgs))
        if deps.usage is not None:
            deps.usage.add("analyze", resp)
        if _trace is not None:
            _trace.record_llm_call("analyze", unit.path, resp)
        data = _extract_json(_text_of(resp))
        try:
            parsed = _Findings(**data)
        except Exception:
            parsed = _Findings()
        return _to_findings(parsed.findings, default_file=unit.path, sources=deps.sources)

class LLMVerifier(_LLMPhase):
    """Верификатор находок. agentic=True — поштучная проверка с инструментами;
    agentic=False — прежний one-shot список (обратносовместимо)."""
    def __init__(self, llm_provider, agentic: bool = False,
                 max_iterations: int = 3, min_severity: str = "medium",
                 model: str | None = None, prompt_cache: bool = False,
                 oneshot_threshold: int = 10, max_verify_tokens: int = 0,
                 max_tool_result_chars: int = 8000):
        super().__init__(llm_provider, prompt_cache=prompt_cache,
                         max_tool_result_chars=max_tool_result_chars)
        self.agentic = agentic
        self.max_iterations = max_iterations
        self.min_severity = min_severity
        self.model = model
        self.oneshot_threshold = oneshot_threshold
        self.max_verify_tokens = max_verify_tokens

    def verify(self, findings: list[Finding], deps: Deps) -> list[Finding]:
        if not findings:
            return []
        if self._budget_exceeded(findings, deps):
            _log.warning(
                "verify: превышен бюджет токенов (%s), пропускаем верификацию",
                self.max_verify_tokens,
            )
            policy = getattr(deps, "policy", None)
            if policy is None:
                return list(findings)
            return [f for f in findings if policy.gate(f)]
        if len(findings) > self.oneshot_threshold:
            return self._verify_oneshot(findings, deps)
        if not self.agentic:
            return self._verify_oneshot(findings, deps)
        return [f for f in findings if self._verify_one(f, deps)]

    def _estimate_verify_tokens(self, findings: list[Finding]) -> int:
        """Грубая оценка токенов verify-промпта (1 токен ≈ 4 символа).

        Зависит от ветки, которую выберет verify(): общий oneshot-промпт либо
        agentic — отдельный промпт (со своим системным префиксом) на каждую
        проверяемую находку. Раньше всегда оценивался oneshot, из-за чего в
        agentic-режиме бюджет систематически недооценивался (N промптов вместо 1)."""
        oneshot = len(findings) > self.oneshot_threshold or not self.agentic
        if oneshot:
            listing = "\n".join(
                f"{i}. [{f.category}/{f.severity}] {f.file}:{f.line} {f.message}"
                for i, f in enumerate(findings))
            prompt = VERIFY_SYSTEM + "\n" + listing + "\n\n" + _VERDICT_SCHEMA
            return len(prompt) // 4
        per_finding = sum(
            len(VERIFY_SYSTEM) + len(f.message or "") + len(f.file or "")
            + len(_VERDICT_ONE_SCHEMA)
            for f in findings if self._needs_check(f))
        return per_finding // 4

    def _budget_exceeded(self, findings: list[Finding], deps: Deps) -> bool:
        if self.max_verify_tokens <= 0:
            return False
        usage = getattr(deps, "usage", None)
        current = usage.total_tokens if usage is not None else 0
        estimated = self._estimate_verify_tokens(findings)
        return current + estimated > self.max_verify_tokens

    def _needs_check(self, f: Finding) -> bool:
        sev_ok = (_SEVERITY_ORDER.get(f.severity, 1)
                  >= _SEVERITY_ORDER.get(self.min_severity, 1))
        # Проверяем находку, если она достаточно важная ИЛИ если агент в ней не уверен
        # (низкая важность + высокая уверенность -> дёшево пропускаем; неуверенность -> проверяем).
        return sev_ok or f.confidence < 0.5

    def _verify_one(self, f: Finding, deps: Deps) -> bool:
        if not self._needs_check(f):
            return True   # дёшево пропускаем (не теряем находку)
        ctx = self._make_tool_context(deps)
        tools = make_tools(ctx)
        llm = self.provider.chat_model_with_tools(tools, model=self.model)
        tools_by_name = {t.name: t for t in tools}
        budget = BudgetTracker(self.max_iterations)
        human = (f"Замечание для проверки:\n[{f.category}/{f.severity}] "
                 f"{f.file}:{f.line} {f.message}\n\n")
        # Добавляем окно кода и дифф файла, если доступны — верификатор не тратит итерации на чтение
        sources = getattr(deps, "sources", None)
        if sources and f.file in sources and f.line:
            win = _window(sources[f.file], f.line)
            if win:
                human += f"Контекст кода ({f.file}, строки вокруг {f.line}):\n{win}\n\n"
        patches = getattr(deps, "patches", None)
        if patches and f.file in patches and patches[f.file]:
            human += f"Дифф файла:\n{patches[f.file]}\n\n"
        human += ("Проверь по реальному коду через инструменты (read_file, find_callers, "
                  "get_definition), затем верни вердикт.")
        human += f"\n\nКогда закончишь работу с инструментами, верни итог в формате:\n{_VERDICT_ONE_SCHEMA}"
        _trace = getattr(deps, "trace", None)
        _verify_unit = f"{f.file}:{f.line}"
        messages = [
            SystemMessage(_cacheable(VERIFY_SYSTEM, self.prompt_cache)),
            HumanMessage(_cacheable(human, self.prompt_cache)),
        ]
        if _trace is not None:
            _trace.record_prompt("verify", _verify_unit, human)
        self._run_loop(messages, llm, tools_by_name, budget, deps,
                       stage="verify", unit=_verify_unit)
        # Пробуем достать JSON из последнего AI-ответа без доп. вызова
        data = _last_ai_json(messages)
        if "is_real" in data:
            return self._verdict(f, bool(data.get("is_real", True)), deps)
        # Fallback: отдельный invoke со схемой
        _fallback_msgs = messages + [HumanMessage(_VERDICT_ONE_SCHEMA)]
        resp = with_llm_retry(
            lambda: self.provider.chat_model(model=self.model).invoke(_fallback_msgs))
        if deps.usage is not None:
            deps.usage.add("verify", resp)
        if _trace is not None:
            _trace.record_llm_call("verify", _verify_unit, resp)
        data = _extract_json(_text_of(resp))
        if "is_real" not in data:
            return self._verdict(f, True, deps)   # fail-open: не разобрали -> оставляем
        return self._verdict(f, bool(data.get("is_real", True)), deps)

    def _verdict(self, f: Finding, result: bool, deps: Deps) -> bool:
        """Залогировать agentic-вердикт (если включён VerdictLog) и вернуть его."""
        v = getattr(deps, "verdicts", None)
        if v:
            v.log_verdict(f, is_real=result, source="agentic")
        return result

    def _verify_oneshot(self, findings: list[Finding], deps: Deps) -> list[Finding]:
        listing = "\n".join(
            f"{i}. [{f.category}/{f.severity}] {f.file}:{f.line} {f.message}"
            for i, f in enumerate(findings))
        _oneshot_msgs = [SystemMessage(VERIFY_SYSTEM), HumanMessage(listing + "\n\n" + _VERDICT_SCHEMA)]
        resp = with_llm_retry(
            lambda: self.provider.chat_model(model=self.model).invoke(_oneshot_msgs))
        if deps.usage is not None:
            deps.usage.add("verify", resp)
        _trace = getattr(deps, "trace", None)
        if _trace is not None:
            _trace.record_llm_call("verify", "(oneshot)", resp)
        data = _extract_json(_text_of(resp))
        if "verdicts" not in data:
            return findings
        try:
            vb = _VerdictBatch(**data)
        except Exception:
            return findings
        verdict_by_idx = {v.index: v.is_real for v in vb.verdicts}
        vlog = getattr(deps, "verdicts", None)
        if vlog:
            for i, f in enumerate(findings):
                vlog.log_verdict(f, is_real=bool(verdict_by_idx.get(i, True)), source="oneshot")
        return [f for i, f in enumerate(findings) if verdict_by_idx.get(i, True)]


class LLMSynthesizer(_LLMPhase):
    """Кросс-файловый проход по всем находкам PR: добавляет кросс-файловые проблемы,
    дедуплицирует. Tool-enabled. Fail-open: при неразборе/пустом ответе — возвращает вход."""

    def __init__(self, llm_provider, max_iterations: int = 6, prompt_cache: bool = False,
                 max_tool_result_chars: int = 8000):
        super().__init__(llm_provider, prompt_cache=prompt_cache,
                         max_tool_result_chars=max_tool_result_chars)
        self.max_iterations = max_iterations

    def synthesize(self, findings: list[Finding], deps: Deps) -> list[Finding]:
        if not findings:
            return findings
        ctx = self._make_tool_context(deps)
        tools = make_tools(ctx)
        llm = self.provider.chat_model_with_tools(tools)
        tools_by_name = {t.name: t for t in tools}
        budget = BudgetTracker(self.max_iterations)
        listing = "\n".join(
            f"{i}. [{f.category}/{f.severity}] {f.file}:{f.line} {f.message}"
            for i, f in enumerate(findings))
        pr_ctx = _pr_context(deps, deps.changed_paths)
        human = ((pr_ctx + "\n\n") if pr_ctx else "")
        bundle = _pr_bundle(deps, deps.changed_paths)
        if bundle:
            human += bundle + "\n\n"
        if "Изменённые сигнатуры в PR" in bundle:
            human += ("Проверь согласованность изменённых сигнатур с их вызовами "
                      "через find_callers.\n\n")
        human += (f"Текущие находки по всему PR:\n{listing}\n\n"
                  "Проверь кросс-файловую согласованность инструментами и верни итоговый список.")
        human += f"\n\nКогда закончишь работу с инструментами, верни итог в формате:\n{_SYNTH_SCHEMA}"
        _trace = getattr(deps, "trace", None)
        messages = [
            SystemMessage(_cacheable(SYNTHESIZE_SYSTEM, self.prompt_cache)),
            HumanMessage(_cacheable(human, self.prompt_cache)),
        ]
        if _trace is not None:
            _trace.record_prompt("synthesize", "(синтез)", human)
        self._run_loop(messages, llm, tools_by_name, budget, deps,
                       stage="synthesize", unit="(синтез)")
        # Пробуем достать JSON из последнего AI-ответа без доп. вызова
        data = _last_ai_json(messages)
        valid_inline = "keep" in data or "add" in data
        if valid_inline:
            try:
                decision = _SynthDecision(**data)
                n = len(findings)
                kept = [findings[i] for i in decision.keep if 0 <= i < n]
                added = _to_findings(decision.add, default_file=None, sources=deps.sources)
                if decision.add:
                    missing = sum(1 for m in decision.add if not m.file)
                    if missing:
                        _log.warning("synthesize: %d add-находок без поля file — пропущены "
                                     "(нельзя локализовать)", missing)
                result = kept + added
                return result or findings   # пусто -> не теряем вход (fail-open)
            except Exception:
                pass
        # Fallback: отдельный invoke со схемой
        _fallback_msgs = messages + [HumanMessage(_SYNTH_SCHEMA)]
        resp = with_llm_retry(lambda: self.provider.chat_model().invoke(_fallback_msgs))
        if deps.usage is not None:
            deps.usage.add("synthesize", resp)
        if _trace is not None:
            _trace.record_llm_call("synthesize", "(синтез)", resp)
        data = _extract_json(_text_of(resp))
        try:
            decision = _SynthDecision(**data)
        except Exception:
            return findings   # fail-open: не разобрали -> исходные как есть
        n = len(findings)
        kept = [findings[i] for i in decision.keep if 0 <= i < n]
        added = _to_findings(decision.add, default_file=None, sources=deps.sources)
        if decision.add:
            missing = sum(1 for m in decision.add if not m.file)
            if missing:
                _log.warning("synthesize: %d add-находок без поля file — пропущены "
                             "(нельзя локализовать)", missing)
        result = kept + added
        return result or findings   # пусто -> не теряем вход (fail-open)
