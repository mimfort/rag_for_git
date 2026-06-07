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

def make_assemble_node(deps: Deps):
    def assemble(state: ReviewState):
        existing = deps.vcs.list_existing_fingerprints(deps.pr_number)
        commentable = {p: commentable_lines(deps.patches.get(p)) for p in deps.changed_paths}
        inline: list[InlineComment] = []
        summary_lines: list[str] = ["## Авто-ревью\n"]
        ranked = sorted(state["verified"], key=lambda f: (-f.confidence,))
        for f in ranked:
            if len(inline) >= deps.policy.max_comments:
                break
            fp = f.fingerprint()
            if fp in existing:
                continue
            body = f"**[{f.category}/{f.severity}]** {f.message}"
            if f.suggestion:
                # предложение — обычным текстом, НЕ в ```suggestion: модель часто пишет
                # совет словами, а GitHub-блок suggestion применяется как точная замена кода
                body += f"\n\n💡 _Предложение:_ {f.suggestion}"
            body += f"\n<!-- ai-review:{fp} -->"
            allowed = commentable.get(f.file, {"RIGHT": set(), "LEFT": set()})
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
