"""Лимиты контекста retrieval-тулов (PRI-202). Per-repo, читаются из .review.yml.
Env-слоя нет: отсутствие ключа → дефолт-константа из этого модуля."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CodebaseLimits:
    floor: int = 4               # минимум чанков всегда (даже при обрыве на 1-м)
    ceiling: int = 15            # потолок (токены/Voyage)
    ratio: float = 0.5           # брать пока score >= ratio*top
    abs_floor: float = 0.3       # и score >= abs_floor по абсолюту
    candidate_pool: int = 30     # верхний предел кандидатов до реранка
    ann_distance_max: float = 0.65  # ANN-префильтр: отбросить не-BM25 с cosine-дистанцией > порога


@dataclass(frozen=True)
class TasksLimits:
    floor: int = 3
    ceiling: int = 8


@dataclass(frozen=True)
class GraphLimits:
    hops: int = 1
    callers_topk: int = 25


@dataclass(frozen=True)
class ContextLimits:
    search_codebase: CodebaseLimits = field(default_factory=CodebaseLimits)
    search_tasks: TasksLimits = field(default_factory=TasksLimits)
    graph: GraphLimits = field(default_factory=GraphLimits)

    @classmethod
    def from_review_yaml(cls, data: dict | None) -> "ContextLimits":
        """Собрать лимиты из распарсенного .review.yml. Заданные ключи поверх дефолтов."""
        block = (data or {}).get("context_limits") or {}
        cb = block.get("search_codebase") or {}
        st = block.get("search_tasks") or {}
        gr = block.get("graph") or {}
        return cls(
            search_codebase=CodebaseLimits(
                floor=int(cb.get("floor", CodebaseLimits.floor)),
                ceiling=int(cb.get("ceiling", CodebaseLimits.ceiling)),
                ratio=float(cb.get("ratio", CodebaseLimits.ratio)),
                abs_floor=float(cb.get("abs_floor", CodebaseLimits.abs_floor)),
                candidate_pool=int(cb.get("candidate_pool", CodebaseLimits.candidate_pool)),
                ann_distance_max=float(
                    cb.get("ann_distance_max", CodebaseLimits.ann_distance_max)),
            ),
            search_tasks=TasksLimits(
                floor=int(st.get("floor", TasksLimits.floor)),
                ceiling=int(st.get("ceiling", TasksLimits.ceiling)),
            ),
            graph=GraphLimits(
                hops=int(gr.get("hops", GraphLimits.hops)),
                callers_topk=int(gr.get("callers_topk", GraphLimits.callers_topk)),
            ),
        )
