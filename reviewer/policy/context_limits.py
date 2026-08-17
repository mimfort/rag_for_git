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
class CodeSectionLimits:
    """Бюджет секции code контекста задачи (PRI-256). Единица бюджета — файл.

    Отдельный от CodebaseLimits намеренно: тот обслуживает публичный
    search_codebase, /ask и грунтовку, где единица бюджета — чанк, а потолок
    связан с квотой реранкера. Смешение двух шкал в одном dataclass сделало бы
    невыразимым «бюджет секции, независимый от чанкового потолка».

    Резерв источника подмешивания (max_augmented_files) — отдельный ключ, а не
    доля max_files: так вклад источника чисто измерим, а сам рычаг снимается
    одним значением. Верхнего предохранителя у резерва нет: политика доверяет
    оператору, симметрично search_codebase.ceiling.
    """
    max_files: int = 12          # различных файлов в секции
    max_chunks_per_file: int = 1  # чанков на один файл
    chars_per_file: int = 1300   # доля символов на файл (операционный бюджет — на исходный текст блока)
    max_augmented_files: int = 3  # сколько файлов секции может занять подмешанный сигнал (PRI-257)

    @property
    def max_chars(self) -> int:
        """Страховочный потолок ПОСЛЕ рендера, а не операционный бюджет.

        Операционный бюджет секции — max_files × max_chunks_per_file ×
        chars_per_file: именно он делает объём линейным по числу файлов.
        Множитель 3/2 здесь — принятый запас, а не выведенная величина:
        измеренные накладные рендера (префиксы номеров строк ~10 %, заголовки
        блоков ~4 %) лишь показывают, что запаса хватает, в том числе на
        страховочный случай, когда cap_block удерживает первую строку целиком,
        даже если она длиннее лимита. Без запаса срез в as_context вернул бы
        ровно тот дефект, который файловый бюджет убирает.
        """
        return self.max_files * self.max_chunks_per_file * self.chars_per_file * 3 // 2


@dataclass(frozen=True)
class ContextLimits:
    search_codebase: CodebaseLimits = field(default_factory=CodebaseLimits)
    search_tasks: TasksLimits = field(default_factory=TasksLimits)
    graph: GraphLimits = field(default_factory=GraphLimits)
    code_section: CodeSectionLimits = field(default_factory=CodeSectionLimits)

    @classmethod
    def from_review_yaml(cls, data: dict | None) -> "ContextLimits":
        """Собрать лимиты из распарсенного .review.yml. Заданные ключи поверх дефолтов."""
        block = (data or {}).get("context_limits") or {}
        cb = block.get("search_codebase") or {}
        st = block.get("search_tasks") or {}
        gr = block.get("graph") or {}
        cs = block.get("code_section") or {}
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
            code_section=CodeSectionLimits(
                max_files=int(cs.get("max_files", CodeSectionLimits.max_files)),
                max_chunks_per_file=int(
                    cs.get("max_chunks_per_file", CodeSectionLimits.max_chunks_per_file)),
                chars_per_file=int(
                    cs.get("chars_per_file", CodeSectionLimits.chars_per_file)),
                max_augmented_files=int(
                    cs.get("max_augmented_files", CodeSectionLimits.max_augmented_files)),
            ),
        )
