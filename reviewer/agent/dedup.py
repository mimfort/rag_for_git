"""Дедупликация находок (Finding) без LLM/эмбеддингов.

Алгоритм:
1. Точные дубли по ключу (file, line, category, message) — оставляем первый.
   message нормализуется перед сравнением (регистр, пробелы, пунктуация), поэтому
   near-duplicate с дрейфом формулировки LLM схлопываются на этом шаге.
2. Fuzzy-дубли внутри группы (file, category): строки совпадают или отличаются ≤2,
   и SequenceMatcher ratio по нормализованным message ≥ similarity — это дубль.
3. Выживает находка с максимальным severity; при равенстве — с максимальным confidence.
   Если у выжившей нет fix/replacement, а у дубля есть — fix-поля переносятся с дубля.
"""
from __future__ import annotations

import difflib
import logging
from typing import TYPE_CHECKING

from reviewer.vcs.base import normalize_message

if TYPE_CHECKING:
    from reviewer.vcs.base import Finding

logger = logging.getLogger(__name__)

# Порядок severity: чем выше индекс, тем «тяжелее».
_SEVERITY_RANK: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}


def _has_fix(f: "Finding") -> bool:
    return f.replacement is not None and f.fix_start is not None and f.fix_end is not None


def _merge(winner: "Finding", loser: "Finding") -> "Finding":
    """Перенести fix-поля с loser на winner, если у winner их нет."""
    if not _has_fix(winner) and _has_fix(loser):
        winner.fix_start = loser.fix_start
        winner.fix_end = loser.fix_end
        winner.replacement = loser.replacement
    return winner


def _better(a: "Finding", b: "Finding") -> "Finding":
    """Вернуть 'лучшую' из двух: сначала по severity, затем по confidence."""
    rank_a = _SEVERITY_RANK.get(a.severity, 0)
    rank_b = _SEVERITY_RANK.get(b.severity, 0)
    if rank_a != rank_b:
        return a if rank_a > rank_b else b
    return a if a.confidence >= b.confidence else b


def _fuzzy_match(m1: str, m2: str, similarity: float) -> bool:
    return difflib.SequenceMatcher(None, normalize_message(m1), normalize_message(m2)).ratio() >= similarity


def dedup_findings(findings: list, similarity: float = 0.9) -> list:
    """Схлопнуть дубли находок без LLM/эмбеддингов. Порядок выживших — как во входе.

    message нормализуется (регистр, пробелы, пунктуация) перед точным и fuzzy-сравнением,
    поэтому мелкий дрейф формулировки LLM между прогонами не плодит дубли.

    Parameters
    ----------
    findings:
        Список объектов :class:`~reviewer.vcs.base.Finding`.
    similarity:
        Порог SequenceMatcher ratio для fuzzy-совпадения нормализованных сообщений (0..1).

    Returns
    -------
    list
        Находки без дублей; порядок соответствует входному списку.
    """
    if len(findings) <= 1:
        return list(findings)

    # --- Шаг 1: точные дубли по (file, line, category, message) ---
    exact_seen: dict[tuple, int] = {}  # ключ → индекс в списке
    after_exact: list = []             # уже без точных дублей, индексы исходного списка

    for idx, f in enumerate(findings):
        key = (f.file, f.line, f.category, normalize_message(f.message))
        if key in exact_seen:
            survivor_idx = exact_seen[key]
            survivor = after_exact[survivor_idx]
            logger.info(
                "dedup: %s:%s [%s] «%s» схлопнута с %s:%s [%s] «%s» (точный дубль)",
                f.file, f.line, f.category, f.message,
                survivor.file, survivor.line, survivor.category, survivor.message,
            )
            # Выбираем лучшую из двух по severity/confidence, затем переносим fix.
            winner = _better(survivor, f)
            loser = f if winner is survivor else survivor
            after_exact[survivor_idx] = _merge(winner, loser)
        else:
            exact_seen[key] = len(after_exact)
            after_exact.append(f)

    if len(after_exact) <= 1:
        return after_exact

    # --- Шаг 2–3: fuzzy-дубли внутри группы (file, category) ---
    # Сохраняем индекс в after_exact, чтобы сохранить порядок входа.
    groups: dict[tuple[str, str], list[int]] = {}
    for i, f in enumerate(after_exact):
        gk = (f.file, f.category)
        groups.setdefault(gk, []).append(i)

    # alive[i] = True/False: жива ли находка с индексом i в after_exact
    alive = [True] * len(after_exact)

    for gk, indices in groups.items():
        # Сортируем по line (None → очень большой номер, на хвост)
        indices_sorted = sorted(indices, key=lambda i: (after_exact[i].line is None, after_exact[i].line or 0))

        for pos_a in range(len(indices_sorted)):
            ia = indices_sorted[pos_a]
            if not alive[ia]:
                continue
            fa = after_exact[ia]

            for pos_b in range(pos_a + 1, len(indices_sorted)):
                ib = indices_sorted[pos_b]
                if not alive[ib]:
                    continue
                fb = after_exact[ib]

                # Строки совпадают или отличаются не больше чем на 2.
                line_a = fa.line if fa.line is not None else -1000
                line_b = fb.line if fb.line is not None else -1000
                if abs(line_a - line_b) > 2:
                    # Список отсортирован по line, дальше будут только бо́льшие отступы.
                    break

                if not _fuzzy_match(fa.message, fb.message, similarity):
                    continue

                # Это дубль — определяем победителя.
                winner = _better(fa, fb)
                loser = fa if winner is fb else fb
                iw = ia if winner is fa else ib
                il = ia if winner is fb else ib

                logger.info(
                    "dedup: %s:%s [%s] «%s» схлопнута с %s:%s [%s] «%s» (fuzzy ratio≥%.2f)",
                    loser.file, loser.line, loser.category, loser.message,
                    winner.file, winner.line, winner.category, winner.message,
                    similarity,
                )

                # Переносим fix с проигравшего на победителя, если нужно.
                after_exact[iw] = _merge(winner, loser)
                alive[il] = False

                # Обновляем fa, если победил fb (iw = ib).
                if iw == ib:
                    fa = after_exact[iw]
                    # ia теперь «мёртв» — выходим из внутреннего цикла.
                    break

    return [after_exact[i] for i in range(len(after_exact)) if alive[i]]
