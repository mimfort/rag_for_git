from __future__ import annotations
import json
import logging
import re
from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage

from reviewer.agent.state import ReviewUnit, Deps
from reviewer.agent.prompts import ANALYZE_SYSTEM, VERIFY_SYSTEM, SYNTHESIZE_SYSTEM
from reviewer.tools.code_tools import make_tools, ToolContext
from reviewer.vcs.base import Finding
from reviewer.llm.budget import BudgetTracker, BudgetExceeded

_log = logging.getLogger(__name__)

_VALID_SEVERITY = {"low", "medium", "high", "critical"}
_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_VERDICT_ONE_SCHEMA = 'Верни СТРОГО один JSON-объект: {"is_real": true|false}'

_SYNTH_SCHEMA = (
    'Верни СТРОГО один JSON-объект без пояснений и markdown:\n'
    '{"findings": [{"file": "<путь>", "category": "correctness|security|performance|style", '
    '"severity": "low|medium|high|critical", "line": <int|null>, "message": "...", '
    '"suggestion": "... или null", "confidence": 0.0}]}\n'
    'Верни ИТОГОВЫЙ список по всему PR: добавь кросс-файловые проблемы '
    '(рассогласование сигнатура↔вызовы), убери дубли. Поле file обязательно у каждой находки.'
)

_FINDINGS_SCHEMA = (
    'Верни СТРОГО один JSON-объект без пояснений и без markdown-обёртки:\n'
    '{"findings": [{"category": "correctness|security|performance|style", '
    '"severity": "low|medium|high|critical", '
    '"line": <номер строки в НОВОЙ версии файла или null>, '
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


def _run_tool_loop(messages: list, llm, tools_by_name: dict, budget) -> list:
    """Гоняет tool-loop до отсутствия tool_calls или исчерпания бюджета.
    Мутирует и возвращает messages (добавляет AI- и ToolMessage). При BudgetExceeded —
    мягко выходит, оставляя накопленную историю для финального структурного запроса."""
    try:
        while True:
            budget.tick()
            ai = llm.invoke(messages)
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
                messages.append(ToolMessage(str(result), tool_call_id=call["id"]))
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

class _Fix(BaseModel):
    start_line: int | None = None
    end_line: int | None = None
    replacement: str | None = None

class _FindingModel(BaseModel):
    category: str
    file: str | None = None
    severity: str = Field(description="low|medium|high|critical")
    line: int | None = None
    message: str
    suggestion: str | None = None
    fix: _Fix | None = None
    confidence: float = 0.7

class _Findings(BaseModel):
    findings: list[_FindingModel] = Field(default_factory=list)

def _to_findings(models, default_file: str) -> list[Finding]:
    """Преобразовать распарсенные модели в Finding. file берётся из модели либо default."""
    out: list[Finding] = []
    for f in models:
        fs = f.fix.start_line if f.fix else None
        fe = f.fix.end_line if f.fix else None
        rp = f.fix.replacement if f.fix else None
        if rp is not None and (fs is None or fe is None):
            rp = None
        out.append(Finding(
            category=f.category,
            severity=(f.severity if f.severity in _VALID_SEVERITY else "medium"),
            file=(f.file or default_file), line=f.line, side="RIGHT", message=f.message,
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

class LLMAnalyzer:
    """Tool-loop + структурированный вывод findings для одного файла."""
    def __init__(self, llm_provider, max_iterations: int):
        self.provider = llm_provider
        self.max_iterations = max_iterations

    def analyze(self, unit: ReviewUnit, deps: Deps) -> list[Finding]:
        ctx = ToolContext(
            retriever=deps.retriever, graph=deps.graph,
            overlay_ref=deps.overlay_ref, changed_paths=deps.changed_paths,
            changed_node_ids=unit.node_ids,
            read_file_fn=((lambda p: deps.vcs.get_file_at_ref(p, deps.head_sha))
                          if deps.vcs else None),
            patches=deps.patches, store=getattr(deps.retriever, "store", None))
        tools = make_tools(ctx)
        llm = self.provider.chat_model_with_tools(tools)
        tools_by_name = {t.name: t for t in tools}
        budget = BudgetTracker(self.max_iterations)
        numbered = _numbered(unit.new_source)
        pr_ctx = _pr_context(deps, deps.changed_paths)
        human = (pr_ctx + "\n\n") if pr_ctx else ""
        human += f"Файл: {unit.path}\n"
        if numbered:
            human += f"Новая версия файла (с номерами строк N|код):\n{numbered}\n\n"
        human += f"Изменения (дифф):\n{unit.changed_text}"
        messages = [SystemMessage(ANALYZE_SYSTEM), HumanMessage(human)]
        _run_tool_loop(messages, llm, tools_by_name, budget)
        resp = self.provider.chat_model().invoke(messages + [HumanMessage(_FINDINGS_SCHEMA)])
        data = _extract_json(_text_of(resp))
        try:
            parsed = _Findings(**data)
        except Exception:
            parsed = _Findings()
        return _to_findings(parsed.findings, default_file=unit.path)

class LLMVerifier:
    """Верификатор находок. agentic=True — поштучная проверка с инструментами;
    agentic=False — прежний one-shot список (обратносовместимо)."""
    def __init__(self, llm_provider, agentic: bool = False,
                 max_iterations: int = 3, min_severity: str = "medium"):
        self.provider = llm_provider
        self.agentic = agentic
        self.max_iterations = max_iterations
        self.min_severity = min_severity

    def verify(self, findings: list[Finding], deps: Deps) -> list[Finding]:
        if not findings:
            return []
        if not self.agentic:
            return self._verify_oneshot(findings, deps)
        return [f for f in findings if self._verify_one(f, deps)]

    def _needs_check(self, f: Finding) -> bool:
        sev_ok = (_SEVERITY_ORDER.get(f.severity, 1)
                  >= _SEVERITY_ORDER.get(self.min_severity, 1))
        # Проверяем находку, если она достаточно важная ИЛИ если агент в ней не уверен
        # (низкая важность + высокая уверенность -> дёшево пропускаем; неуверенность -> проверяем).
        return sev_ok or f.confidence < 0.5

    def _verify_one(self, f: Finding, deps: Deps) -> bool:
        if not self._needs_check(f):
            return True   # дёшево пропускаем (не теряем находку)
        ctx = ToolContext(
            retriever=deps.retriever, graph=deps.graph,
            overlay_ref=deps.overlay_ref, changed_paths=deps.changed_paths,
            changed_node_ids=[],
            read_file_fn=((lambda p: deps.vcs.get_file_at_ref(p, deps.head_sha))
                          if deps.vcs else None),
            patches=deps.patches, store=getattr(deps.retriever, "store", None))
        tools = make_tools(ctx)
        llm = self.provider.chat_model_with_tools(tools)
        tools_by_name = {t.name: t for t in tools}
        budget = BudgetTracker(self.max_iterations)
        human = (f"Замечание для проверки:\n[{f.category}/{f.severity}] "
                 f"{f.file}:{f.line} {f.message}\n\n"
                 "Проверь по реальному коду через инструменты (read_file, find_callers, "
                 "get_definition), затем верни вердикт.")
        messages = [SystemMessage(VERIFY_SYSTEM), HumanMessage(human)]
        _run_tool_loop(messages, llm, tools_by_name, budget)
        resp = self.provider.chat_model().invoke(
            messages + [HumanMessage(_VERDICT_ONE_SCHEMA)])
        data = _extract_json(_text_of(resp))
        if "is_real" not in data:
            return True   # fail-open: не разобрали -> оставляем
        return bool(data.get("is_real", True))

    def _verify_oneshot(self, findings: list[Finding], deps: Deps) -> list[Finding]:
        listing = "\n".join(
            f"{i}. [{f.category}/{f.severity}] {f.file}:{f.line} {f.message}"
            for i, f in enumerate(findings))
        resp = self.provider.chat_model().invoke(
            [SystemMessage(VERIFY_SYSTEM), HumanMessage(listing + "\n\n" + _VERDICT_SCHEMA)])
        data = _extract_json(_text_of(resp))
        if "verdicts" not in data:
            return findings
        try:
            vb = _VerdictBatch(**data)
        except Exception:
            return findings
        verdict_by_idx = {v.index: v.is_real for v in vb.verdicts}
        return [f for i, f in enumerate(findings) if verdict_by_idx.get(i, True)]


class LLMSynthesizer:
    """Кросс-файловый проход по всем находкам PR: добавляет кросс-файловые проблемы,
    дедуплицирует. Tool-enabled. Fail-open: при неразборе/пустом ответе — возвращает вход."""

    def __init__(self, llm_provider, max_iterations: int = 6):
        self.provider = llm_provider
        self.max_iterations = max_iterations

    def synthesize(self, findings: list[Finding], deps: Deps) -> list[Finding]:
        if not findings:
            return findings
        ctx = ToolContext(
            retriever=deps.retriever, graph=deps.graph,
            overlay_ref=deps.overlay_ref, changed_paths=deps.changed_paths,
            changed_node_ids=[],
            read_file_fn=((lambda p: deps.vcs.get_file_at_ref(p, deps.head_sha))
                          if deps.vcs else None),
            patches=deps.patches, store=getattr(deps.retriever, "store", None))
        tools = make_tools(ctx)
        llm = self.provider.chat_model_with_tools(tools)
        tools_by_name = {t.name: t for t in tools}
        budget = BudgetTracker(self.max_iterations)
        listing = "\n".join(
            f"{i}. [{f.category}/{f.severity}] {f.file}:{f.line} {f.message}"
            for i, f in enumerate(findings))
        pr_ctx = _pr_context(deps, deps.changed_paths)
        human = ((pr_ctx + "\n\n") if pr_ctx else "")
        human += (f"Текущие находки по всему PR:\n{listing}\n\n"
                  "Проверь кросс-файловую согласованность инструментами и верни итоговый список.")
        messages = [SystemMessage(SYNTHESIZE_SYSTEM), HumanMessage(human)]
        _run_tool_loop(messages, llm, tools_by_name, budget)
        resp = self.provider.chat_model().invoke(messages + [HumanMessage(_SYNTH_SCHEMA)])
        data = _extract_json(_text_of(resp))
        try:
            parsed = _Findings(**data)
        except Exception:
            return findings   # fail-open
        if not parsed.findings:
            return findings   # пусто -> не теряем вход
        default_file = findings[0].file
        missing = sum(1 for m in parsed.findings if not m.file)
        if missing:
            _log.warning("synthesize: %d находок без поля file — отнесены к %s",
                         missing, default_file)
        return _to_findings(parsed.findings, default_file=default_file)
