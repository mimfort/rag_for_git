from __future__ import annotations
from dataclasses import dataclass


@dataclass
class ReviewUnit:
    path: str
    node_ids: list[str]
    changed_text: str
    new_source: str = ""        # полная новая версия файла (для точных fix-диапазонов)
    structural_summary: str = ""  # компактная сводка структурных изменений символов
