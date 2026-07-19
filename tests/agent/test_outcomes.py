"""account_outcomes — учёт терминального исхода каждого кандидата."""
from __future__ import annotations

from types import SimpleNamespace

from reviewer.agent.outcomes import account_outcomes
from reviewer.policy.policy import ReviewPolicy
from reviewer.vcs.base import Finding


def F(msg, *, cat="correctness", sev="high", file="a.py", line=1, conf=0.9):
    return Finding(cat, sev, file, line, "RIGHT", msg, None, conf)


def _asm_row(f: Finding, *, published: bool, inline: bool) -> dict:
    """Строка как её строит assemble._row."""
    return {
        "file": f.file, "line": f.line, "category": f.category,
        "severity": f.severity, "confidence": f.confidence,
        "fingerprint": f.fingerprint(), "message": (f.message or "")[:500],
        "is_real": True, "published": published, "inline": inline,
    }


def test_all_published_inline():
    f = F("bug")
    candidates = {"f1": f}
    asm = SimpleNamespace(findings_rows=[_asm_row(f, published=True, inline=True)])
    rows = account_outcomes(
        candidates, {}, {}, parsed=[f], kept=[f], deduped=[f],
        asm=asm, policy=ReviewPolicy(),
    )
    assert len(rows) == len(candidates)
    assert rows[0]["outcome"] == "published_inline"
    assert rows[0]["reject_reason"] is None


def test_verify_rejected_carries_reason():
    f = F("hallucinated")
    candidates = {"f1": f}
    asm = SimpleNamespace(findings_rows=[])
    rows = account_outcomes(
        candidates, {"f1": False}, {"f1": "line does not exist"},
        parsed=[], kept=[], deduped=[], asm=asm, policy=ReviewPolicy(),
    )
    assert len(rows) == 1
    assert rows[0]["outcome"] == "verify_rejected"
    assert rows[0]["reject_reason"] == "line does not exist"
    assert rows[0]["is_real"] is False
    assert rows[0]["published"] is False


def test_gate_dropped_carries_gate_reason():
    f = F("style nit", sev="low")                    # ниже medium-порога
    candidates = {"f1": f}
    asm = SimpleNamespace(findings_rows=[])
    policy = ReviewPolicy(severity_threshold="medium")
    rows = account_outcomes(
        candidates, {}, {}, parsed=[f], kept=[], deduped=[],
        asm=asm, policy=policy,
    )
    assert rows[0]["outcome"] == "gate_dropped"
    assert rows[0]["reject_reason"].startswith("severity")
    assert rows[0]["is_real"] is True


def test_deduped_dropped_no_reason():
    winner = F("same bug")
    dup = F("same bug")                              # точный дубль → тот же fingerprint
    candidates = {"f1": winner, "f2": dup}
    asm = SimpleNamespace(findings_rows=[_asm_row(winner, published=True, inline=True)])
    rows = account_outcomes(
        candidates, {}, {}, parsed=[winner, dup], kept=[winner, dup],
        deduped=[winner], asm=asm, policy=ReviewPolicy(),
    )
    outcomes = sorted(r["outcome"] for r in rows)
    assert outcomes == ["deduped", "published_inline"]
    deduped_row = next(r for r in rows if r["outcome"] == "deduped")
    assert deduped_row["reject_reason"] is None


def test_already_posted_from_unpublished_asm_row():
    f = F("dup of prior run")
    candidates = {"f1": f}
    asm = SimpleNamespace(findings_rows=[_asm_row(f, published=False, inline=False)])
    rows = account_outcomes(
        candidates, {}, {}, parsed=[f], kept=[f], deduped=[f],
        asm=asm, policy=ReviewPolicy(),
    )
    assert rows[0]["outcome"] == "already_posted"


def test_published_summary():
    f = F("out of diff", line=None)
    candidates = {"f1": f}
    asm = SimpleNamespace(findings_rows=[_asm_row(f, published=True, inline=False)])
    rows = account_outcomes(
        candidates, {}, {}, parsed=[f], kept=[f], deduped=[f],
        asm=asm, policy=ReviewPolicy(),
    )
    assert rows[0]["outcome"] == "published_summary"


def test_zero_candidates():
    asm = SimpleNamespace(findings_rows=[])
    rows = account_outcomes({}, {}, {}, parsed=[], kept=[], deduped=[],
                            asm=asm, policy=ReviewPolicy())
    assert rows == []


def test_full_funnel_sums_to_candidates():
    """Все 6 исходов присутствуют, сумма = числу кандидатов."""
    inline = F("real inline bug")
    summary = F("real out-of-diff bug", line=None)
    already = F("seen before")
    rejected = F("false positive")
    gated = F("low sev", sev="low")
    d_winner = F("dupe")
    d_loser = F("dupe")
    candidates = {
        "f1": inline, "f2": summary, "f3": already,
        "f4": rejected, "f5": gated, "f6": d_winner, "f7": d_loser,
    }
    parsed = [inline, summary, already, gated, d_winner, d_loser]   # rejected исключён
    kept = [inline, summary, already, d_winner, d_loser]            # gated отсеян
    deduped = [inline, summary, already, d_winner]                  # d_loser схлопнут
    asm = SimpleNamespace(findings_rows=[
        _asm_row(inline, published=True, inline=True),
        _asm_row(summary, published=True, inline=False),
        _asm_row(already, published=False, inline=False),
        _asm_row(d_winner, published=True, inline=True),
    ])
    policy = ReviewPolicy(severity_threshold="medium")
    rows = account_outcomes(
        candidates, {"f4": False}, {"f4": "not a bug"},
        parsed, kept, deduped, asm, policy,
    )
    assert len(rows) == len(candidates)
    counts = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    assert counts == {
        "published_inline": 2, "published_summary": 1, "already_posted": 1,
        "verify_rejected": 1, "gate_dropped": 1, "deduped": 1,
    }


def test_all_rows_share_identical_key_set():
    """Каждая строка account_outcomes несёт одинаковый набор ключей.

    Guard против дрейфа двух билдеров строк (outcomes._row vs assemble._row):
    history.record_run дефолтит лишь run_id/outcome/reject_reason, а остальные
    ключи (file/line/category/severity/confidence/is_real/published/inline/
    fingerprint/message) обязаны присутствовать. Отсутствие любого → KeyError
    в executemany → проглатывается fail-soft → тихая потеря истории всего прогона.
    """
    inline = F("real inline bug")
    rejected = F("false positive")
    gated = F("low sev", sev="low")
    d_winner = F("dupe")
    d_loser = F("dupe")
    candidates = {"f1": inline, "f2": rejected, "f3": gated, "f4": d_winner, "f5": d_loser}
    parsed = [inline, gated, d_winner, d_loser]        # rejected исключён
    kept = [inline, d_winner, d_loser]                 # gated отсеян
    deduped = [inline, d_winner]                       # d_loser схлопнут
    asm = SimpleNamespace(findings_rows=[               # по строке на каждый deduped (1:1)
        _asm_row(inline, published=True, inline=True),
        _asm_row(d_winner, published=True, inline=True),
    ])
    rows = account_outcomes(
        candidates, {"f2": False}, {"f2": "not a bug"},
        parsed, kept, deduped, asm, ReviewPolicy(severity_threshold="medium"),
    )
    # все 5 кандидатов → 5 строк, покрыты verify_rejected/gate_dropped/deduped/published_inline
    assert len(rows) == len(candidates)
    reference = set(rows[0].keys())
    mandatory = {
        "file", "line", "category", "severity", "confidence", "is_real",
        "published", "inline", "fingerprint", "message", "outcome", "reject_reason",
    }
    assert reference == mandatory
    for r in rows:
        assert set(r.keys()) == reference
