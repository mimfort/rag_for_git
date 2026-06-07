from __future__ import annotations
from langgraph.types import Send

from reviewer.agent.state import ReviewState, ReviewUnit, Deps
from reviewer.vcs.base import InlineComment
from reviewer.vcs.diff import commentable_lines

def plan_node(state: ReviewState):
    return {}

def fan_out(state: ReviewState):
    return [Send("analyze", {"unit": u}) for u in state["review_units"]]

def make_analyze_node(deps: Deps):
    def analyze(payload: dict):
        unit: ReviewUnit = payload["unit"]
        found = deps.analyzer.analyze(unit, deps)
        return {"findings": found}
    return analyze

def make_verify_node(deps: Deps):
    def verify(state: ReviewState):
        kept = deps.verifier.verify(state["findings"], deps)
        kept = [f for f in kept if deps.policy.gate(f)]
        return {"verified": kept}
    return verify

def _range_in_diff(right: set[int], start: int, end: int) -> bool:
    """Все строки [start..end] должны быть в RIGHT-строках диффа (иначе GitHub 422/промах)."""
    return start <= end and all(ln in right for ln in range(start, end + 1))

def _overlaps(used: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(not (end < s or start > e) for (s, e) in used)

def _can_apply(f, right: set[int], used: list[tuple[int, int]], mode: str) -> bool:
    """applyable suggestion разрешён только если: режим apply, есть точная замена,
    весь диапазон в диффе (RIGHT) и не пересекается с уже выставленными правками."""
    return (mode == "apply"
            and f.replacement is not None
            and f.fix_start is not None and f.fix_end is not None
            and _range_in_diff(right, f.fix_start, f.fix_end)
            and not _overlaps(used, f.fix_start, f.fix_end))

def make_assemble_node(deps: Deps):
    def assemble(state: ReviewState):
        existing = deps.vcs.list_existing_fingerprints(deps.pr_number)
        commentable = {p: commentable_lines(deps.patches.get(p)) for p in deps.changed_paths}
        used: dict[str, list[tuple[int, int]]] = {}
        inline: list[InlineComment] = []
        summary_lines: list[str] = ["## Авто-ревью\n"]
        ranked = sorted(state["verified"], key=lambda f: (-f.confidence,))
        for f in ranked:
            if len(inline) >= deps.policy.max_comments:
                break
            fp = f.fingerprint()
            if fp in existing:
                continue
            allowed = commentable.get(f.file, {"RIGHT": set(), "LEFT": set()})
            right = allowed.get("RIGHT", set())
            body = f"**[{f.category}/{f.severity}]** {f.message}"
            if f.suggestion:
                body += f"\n\n💡 _Предложение:_ {f.suggestion}"
            # 1) applyable ```suggestion — только при безопасных инвариантах
            if _can_apply(f, right, used.get(f.file, []), deps.suggestions_mode):
                repl = f.replacement.rstrip("\n")
                body += f"\n\n```suggestion\n{repl}\n```"
                body += f"\n<!-- ai-review:{fp} -->"
                used.setdefault(f.file, []).append((f.fix_start, f.fix_end))
                if f.fix_start < f.fix_end:   # многострочный диапазон
                    inline.append(InlineComment(f.file, f.fix_end, "RIGHT", body,
                                                start_line=f.fix_start, start_side="RIGHT"))
                else:                          # одна строка
                    inline.append(InlineComment(f.file, f.fix_end, "RIGHT", body))
                continue
            # 2) обычный inline (текстовый совет) на строке диффа, иначе — в сводку
            body += f"\n<!-- ai-review:{fp} -->"
            if f.line is not None and f.line in allowed.get(f.side, set()):
                inline.append(InlineComment(f.file, f.line, f.side, body))
            else:
                summary_lines.append(f"- `{f.file}:{f.line}` {body}")
        if inline:
            summary_lines.insert(1, f"Выставлено inline-замечаний на строки диффа: {len(inline)}.\n")
        if len(summary_lines) == 1:
            summary_lines.append("Замечаний не найдено.")
        return {"inline_comments": inline, "summary": "\n".join(summary_lines)}
    return assemble

def make_publish_node(deps: Deps):
    def publish(state: ReviewState):
        deps.vcs.publish_review(deps.pr_number, deps.head_sha,
                                state["summary"], state["inline_comments"])
        return {}
    return publish
