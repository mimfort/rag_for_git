"""Классификация путей: что входит в ядро и как называется промах."""
from __future__ import annotations

NEW_FILE_CATEGORY = "новый файл (не существовал до PR)"


def is_core_production_path(path: str) -> bool:
    """Уже существовавший продакшн-код, по которому ретрив может и должен попадать.

    Ядро: reviewer/**/*.py, plugin/** кроме *.md, корневые *.py. Всё остальное
    (тесты, доки, конфиги, манифесты, eval/) — вне ядра: бриф структурно не
    обязан их предсказывать, и включение их в знаменатель делает recall
    метрикой размера diff'а, а не качества ретрива.
    """
    if path.startswith("eval/"):
        return False
    if path.startswith("reviewer/") and path.endswith(".py"):
        return True
    if path.startswith("plugin/") and not path.endswith(".md"):
        return True
    if "/" not in path and path.endswith(".py"):
        return True
    return False


def categorize_miss(path: str, existed_before: bool) -> str:
    """Категория непредсказанного файла. Новый файл — отдельная категория:
    бриф не мог сослаться на файл, которого ещё не существовало."""
    if not existed_before:
        return NEW_FILE_CATEGORY
    if path.startswith("tests/"):
        return "tests/"
    if path.startswith("docs/"):
        return "docs/"
    if path == ".review.yml" or path.endswith(".review.yml"):
        return ".review.yml/конфиги"
    if path.startswith("plugin/skills/") and path.endswith(".md"):
        return "plugin/skills/*.md"
    if path.startswith("plugin/"):
        return "plugin/ (прочее)"
    if path.startswith("reviewer/"):
        parts = path.split("/")
        module = parts[1] if len(parts) > 1 else ""
        return f"reviewer/{module}" if module else "reviewer/"
    if path.startswith("eval/"):
        return "eval/"
    return "прочее"
