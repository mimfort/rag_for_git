"""Классификация путей: что входит в ядро и как называется промах."""
from __future__ import annotations

from reviewer.metrics.brief_quality.config import BriefQualityConfig

NEW_FILE_CATEGORY = "новый файл (не существовал до PR)"
ROOT_CATEGORY = "корень"


def is_core_production_path(path: str, config: BriefQualityConfig) -> bool:
    """Уже существовавший продакшн-код, по которому ретрив может и должен попадать.

    Ядро задаёт `config.core_paths` (по умолчанию — ядро rag_for_git). Всё
    остальное — тесты, доки, конфиги, манифесты — вне ядра: бриф структурно не
    обязан их предсказывать, и включение их в знаменатель делает recall
    метрикой размера diff'а, а не качества ретрива.

    `config` обязателен намеренно: значение по умолчанию вернуло бы ровно тот
    тихий провал, ради которого задача и делалась — чужой репозиторий молча
    считался бы по ядру rag_for_git.
    """
    return config.matches_core(path)


def categorize_miss(path: str, existed_before: bool, config: BriefQualityConfig) -> str:
    """Категория непредсказанного файла.

    Категории выводятся из тех же `core_paths`, что и само ядро: файл ядра
    называется «верхний сегмент + модуль», прочий — верхним сегментом. Прежний
    захардкоженный список ярлыков (`.review.yml/конфиги`, `plugin/skills/*.md`)
    на чужом репозитории врал, а разъехаться с определением ядра теперь нечему.
    Новый файл — отдельная категория: бриф не мог сослаться на файл, которого
    ещё не существовало.
    """
    if not existed_before:
        return NEW_FILE_CATEGORY
    parts = path.split("/")
    if len(parts) == 1:
        return ROOT_CATEGORY
    if config.matches_core(path) and len(parts) > 2:
        return f"{parts[0]}/{parts[1]}"
    return f"{parts[0]}/"
