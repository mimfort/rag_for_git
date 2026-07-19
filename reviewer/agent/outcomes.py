"""Учёт терминального исхода каждого кандидата-находки для наблюдаемости (PRI).

Одна ответственность: сопоставить кандидату из состояния MCP-сессии на момент
publish его терминальный исход воронки и построить строку для review_findings.
Чистая функция — без сети/БД, детерминирована, тестируется изолированно.

Воронка (6 исходов, сумма = числу кандидатов):
  verify_rejected  — verdicts[fid] is False; reject_reason = verdict_reasons[fid]
  gate_dropped     — survived (parsed), но not policy.gate(f); reject_reason = gate_reason(f)
  deduped          — прошли gate, но схлопнуты dedup_findings (kept ∖ deduped по identity)
  already_posted   — из asm.findings_rows со скрытым fingerprint прошлого прогона (published=False)
  published_inline / published_summary — из asm.findings_rows по флагу inline

Замечание про строки: grounding применяется только к survived; у verify_rejected
кандидатов строка исходная (не грунтованная) — допустимо (запись, не публикация).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reviewer.vcs.base import Finding


def _row(f: "Finding", outcome: str, reject_reason: str | None, *, is_real: bool) -> dict:
    """Строка review_findings для отклонённого кандидата (поля как в assemble._row)."""
    return {
        "file": f.file,
        "line": f.line,
        "category": f.category,
        "severity": f.severity,
        "confidence": f.confidence,
        "fingerprint": f.fingerprint(),
        "message": (f.message or "")[:500],
        "is_real": is_real,
        "published": False,
        "inline": False,
        "outcome": outcome,
        "reject_reason": reject_reason,
    }


def _asm_outcome(row: dict) -> str:
    """Исход строки assemble.findings_rows: published=False ⟺ already_posted."""
    if not row["published"]:
        return "already_posted"
    return "published_inline" if row["inline"] else "published_summary"


def account_outcomes(
    candidates: dict,
    verdicts: dict,
    verdict_reasons: dict,
    parsed: list,
    kept: list,
    deduped: list,
    asm,
    policy,
) -> list[dict]:
    """Построить полный список строк review_findings по терминальным исходам.

    См. модульный докстринг. Инвариант: ``len(result) == len(candidates)``.
    """
    rows: list[dict] = []

    # 1) verify_rejected — кандидаты с явным is_real=false.
    for fid, f in candidates.items():
        if verdicts.get(fid) is False:
            rows.append(_row(f, "verify_rejected", verdict_reasons.get(fid), is_real=False))

    # 2) gate_dropped — survived (parsed), не прошедшие gate. Identity-разность:
    #    kept ⊆ parsed по тем же объектам (kept = [f for f in parsed if gate(f)]).
    kept_ids = {id(f) for f in kept}
    for f in parsed:
        if id(f) not in kept_ids:
            rows.append(_row(f, "gate_dropped", policy.gate_reason(f), is_real=True))

    # 3) deduped — прошли gate, но схлопнуты dedup. deduped ⊆ kept по identity
    #    (dedup_findings возвращает те же объекты). Fingerprint-разность здесь
    #    неверна: точный дубль имеет ТОТ ЖЕ fingerprint, что и выживший.
    deduped_ids = {id(f) for f in deduped}
    for f in kept:
        if id(f) not in deduped_ids:
            rows.append(_row(f, "deduped", None, is_real=True))

    # 4) published_* / already_posted — готовые строки assemble (по одной на deduped).
    for row in asm.findings_rows:
        rows.append({**row, "outcome": _asm_outcome(row), "reject_reason": None})

    return rows
