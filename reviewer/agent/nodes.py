from __future__ import annotations
import logging
from langgraph.types import Send

from reviewer.agent.assemble import assemble_review
from reviewer.agent.dedup import dedup_findings
from reviewer.agent.state import ReviewState, ReviewUnit, Deps

_log = logging.getLogger(__name__)


def plan_node(state: ReviewState):
    return {}


def fan_out(state: ReviewState):
    return [Send("analyze", {"unit": u}) for u in state["review_units"]]


def make_analyze_node(deps: Deps):
    def analyze(payload: dict):
        unit: ReviewUnit = payload["unit"]
        try:
            found = deps.analyzer.analyze(unit, deps)
        except Exception as e:
            _log.warning("analyze %s: %s", unit.path, e, exc_info=True)
            return {"findings": [], "failed_units": [f"{unit.path}: {type(e).__name__}: {e}"]}
        return {"findings": found, "failed_units": []}
    return analyze


def make_verify_node(deps: Deps):
    def verify(state: ReviewState):
        # Gate первым: не тратим LLM-вызовы верификации на находки,
        # которые всё равно будут отброшены по категории/severity/confidence/путям.
        kept = [f for f in state["findings"] if deps.policy.gate(f)]
        # Dedup до verify: analyze идёт параллельно по файлам и может порождать дубли
        # (особенно на скопированном коде) — не платим за верификацию дублей.
        kept = dedup_findings(kept)
        try:
            kept = deps.verifier.verify(kept, deps)
        except Exception as e:
            _log.warning("verify: %s", e, exc_info=True)
            # Fail-open: при сбое верификатора пропускаем находки как есть.
            return {
                "verified": kept,
                "failed_units": [f"verify: {type(e).__name__}: {e}"],
            }
        return {"verified": kept}
    return verify


def make_assemble_node(deps: Deps):
    def assemble(state: ReviewState):
        # Список уже опубликованных fingerprint'ов — не блокирует публикацию при сбое.
        new_failed: list[str] = []
        try:
            existing = deps.vcs.list_existing_fingerprints(deps.pr_number)
        except Exception as e:
            _log.warning("list_existing_fingerprints: %s", e, exc_info=True)
            existing = set()
            new_failed.append(f"existing fingerprints: {type(e).__name__}: {e}")

        result = assemble_review(
            state["verified"],
            # Только выбранные для ревью файлы (changed_paths): deps.patches содержит
            # ВСЕ файлы PR (не-py, removed, сверх лимита) — inline на них недопустим.
            patches={p: deps.patches.get(p) for p in deps.changed_paths},
            sources=deps.sources or {},
            existing_fps=existing,
            max_comments=deps.policy.max_comments,
            suggestions_mode=deps.suggestions_mode,
        )

        # Подхватываем VerdictLog, если подключён: логируем inline vs summary по findings_rows.
        v = getattr(deps, "verdicts", None)
        if v:
            # Контракт assemble_review: она мутирует f.line входных находок, поэтому
            # f.fingerprint() здесь — пост-мутации и совпадает с row["fingerprint"].
            fp_to_finding = {f.fingerprint(): f for f in state["verified"]}
            for row in result.findings_rows:
                if not row.get("published"):
                    continue
                finding = fp_to_finding.get(row["fingerprint"])
                if finding is not None:
                    v.log_published(finding, inline=row["inline"])

        # Добавляем в сводку ошибки прогона и пропущенные файлы.
        summary_lines = result.summary.split("\n")
        failed = list(state.get("failed_units", [])) + new_failed
        if failed:
            summary_lines.append("\n### Не проанализировано (ошибки)")
            for entry in failed:
                summary_lines.append(f"- {entry}")
        skipped = deps.skipped_paths or []
        if skipped:
            summary_lines.append("\n### Пропущено по лимиту файлов (review_max_files)")
            shown = skipped[:20]
            for p in shown:
                summary_lines.append(f"- {p}")
            if len(skipped) > 20:
                summary_lines.append(f"- … и ещё {len(skipped) - 20}")

        return {
            "inline_comments": result.inline_comments,
            "summary": "\n".join(summary_lines),
            "failed_units": new_failed,
        }
    return assemble


def make_publish_node(deps: Deps):
    def publish(state: ReviewState):
        try:
            deps.vcs.publish_review(deps.pr_number, deps.head_sha,
                                    state["summary"], state["inline_comments"])
        except Exception as e:
            _log.error("Не удалось опубликовать ревью: %s", e, exc_info=True)
            return {
                "published": False,
                "failed_units": [f"publish: {type(e).__name__}: {e}"],
            }
        return {"published": True}
    return publish


def make_synthesize_node(deps: Deps):
    def synthesize(state: ReviewState):
        result = deps.synthesizer.synthesize(state["verified"], deps)
        return {"verified": [f for f in result if deps.policy.gate(f)]}
    return synthesize
