from __future__ import annotations
from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage

from reviewer.agent.state import ReviewUnit, Deps
from reviewer.agent.prompts import ANALYZE_SYSTEM, VERIFY_SYSTEM
from reviewer.tools.code_tools import make_tools, ToolContext
from reviewer.vcs.base import Finding
from reviewer.llm.budget import BudgetTracker, BudgetExceeded

_VALID_SEVERITY = {"low", "medium", "high", "critical"}

class _FindingModel(BaseModel):
    category: str
    severity: str = Field(description="low|medium|high|critical")
    line: int | None = None
    message: str
    suggestion: str | None = None
    confidence: float = 0.7

class _Findings(BaseModel):
    findings: list[_FindingModel] = Field(default_factory=list)

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
        ctx = ToolContext(retriever=deps.retriever, graph=deps.graph,
                          overlay_ref=deps.overlay_ref, changed_paths=deps.changed_paths,
                          changed_node_ids=unit.node_ids)
        tools = make_tools(ctx)
        llm = self.provider.chat_model_with_tools(tools)
        tools_by_name = {t.name: t for t in tools}
        budget = BudgetTracker(self.max_iterations)
        messages = [SystemMessage(ANALYZE_SYSTEM),
                    HumanMessage(f"Файл: {unit.path}\nИзменения:\n{unit.changed_text}")]
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
                        if tool is None:
                            result = f"(неизвестный инструмент: {call['name']})"
                        else:
                            result = tool.invoke(call["args"])
                    except Exception as e:  # инструмент упал — не рвём диалог
                        result = f"(ошибка инструмента {call['name']}: {e})"
                    messages.append(ToolMessage(str(result), tool_call_id=call["id"]))
        except BudgetExceeded:
            pass
        structured = self.provider.chat_model().with_structured_output(_Findings)
        parsed: _Findings = structured.invoke(
            messages + [HumanMessage("Выведи итоговые findings структурой.")])
        return [Finding(category=f.category,
                        severity=(f.severity if f.severity in _VALID_SEVERITY else "medium"),
                        file=unit.path,
                        line=f.line, side="RIGHT", message=f.message,
                        suggestion=f.suggestion, confidence=f.confidence)
                for f in parsed.findings]

class LLMVerifier:
    def __init__(self, llm_provider):
        self.provider = llm_provider

    def verify(self, findings: list[Finding], deps: Deps) -> list[Finding]:
        if not findings:
            return []
        llm = self.provider.chat_model().with_structured_output(_VerdictBatch)
        listing = "\n".join(f"{i}. [{f.category}/{f.severity}] {f.file}:{f.line} {f.message}"
                            for i, f in enumerate(findings))
        verdicts: _VerdictBatch = llm.invoke([SystemMessage(VERIFY_SYSTEM),
                                              HumanMessage(listing)])
        keep_idx = {v.index for v in verdicts.verdicts if v.is_real}
        return [f for i, f in enumerate(findings) if i in keep_idx]
