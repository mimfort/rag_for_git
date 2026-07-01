"""Адаптивная отсечка контекста по «обрыву» скоров реранкера (PRI-202).

Чистая, без БД и сети. На вход — список (item, score), отсортированный по score
убыванием; на выход — оставленные items + метаданные хвоста для ленивой заметки.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


def _prefix(path: str) -> str:
    """Первый сегмент пути — грубая «подсистема»."""
    return path.replace("\\", "/").split("/", 1)[0]


@dataclass
class TailMeta:
    kept_n: int = 0
    total_n: int = 0
    top_score: float | None = None
    cut_score: float | None = None        # скор последнего взятого
    drop_score: float | None = None       # скор первого отброшенного
    beyond_relevant: int = 0              # за обрезом со score >= abs_floor
    groups: list[tuple[str, int, float]] = field(default_factory=list)  # (prefix, count, top)


def select_by_cliff(scored, *, floor_n, ceiling_n, ratio, abs_floor):
    """Отсечь хвост по гибридному правилу (ratio ∧ abs_floor) в рельсах [floor, ceiling].

    floor побеждает оба правила (минимум всегда); ceiling — жёсткий максимум.
    Возвращает (kept_items, TailMeta).
    """
    if not scored:
        return [], TailMeta()
    ceiling_n = max(ceiling_n, floor_n)        # ceiling приоритетнее, но не ниже floor
    top = scored[0][1]
    kept: list = []
    for i, (item, score) in enumerate(scored):
        if i < floor_n:
            kept.append((item, score))
            continue
        if len(kept) >= ceiling_n:
            break
        if score >= top * ratio and score >= abs_floor:
            kept.append((item, score))
        else:
            break
    tail = scored[len(kept):]
    return [it for it, _ in kept], _build_tail_meta(kept, tail, top, abs_floor, len(scored))


def _build_tail_meta(kept, tail, top, abs_floor, total):
    relevant = [(it, s) for it, s in tail if s >= abs_floor]
    grouped: dict[str, list[float]] = defaultdict(list)
    for it, s in relevant:
        grouped[_prefix(it.path)].append(s)
    groups = sorted(
        ((p, len(ss), max(ss)) for p, ss in grouped.items()),
        key=lambda g: g[2], reverse=True)
    return TailMeta(
        kept_n=len(kept), total_n=total, top_score=top,
        cut_score=kept[-1][1] if kept else None,
        drop_score=tail[0][1] if tail else None,
        beyond_relevant=len(relevant), groups=groups[:3])


def format_tail_note(meta: TailMeta) -> str | None:
    """Ленивая заметка о высокоскоровом хвосте за обрезом. None, если хвост нерелевантен."""
    if meta.beyond_relevant <= 0:
        return None
    grp = ", ".join(f"{p} ({top:.2f})" for p, _cnt, top in meta.groups)
    drop = f", обрыв на {meta.drop_score:.2f}" if meta.drop_score is not None else ""
    return (f"— контекст обрезан по cliff: {meta.kept_n} из {meta.total_n} "
            f"(скор {meta.top_score:.2f}→{meta.cut_score:.2f}{drop}). За обрезом ещё "
            f"{meta.beyond_relevant} релевантных: {grp}. Перевызови с большим ceiling, "
            f"чтобы включить.")
