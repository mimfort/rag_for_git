"""Конфигурация метрики качества брифа: ядро репозитория, ключ задачи, каталог брифов.

Модуль ЧИСТЫЙ, как и весь пакет: на вход — уже разобранный mapping `.review.yml`,
ни файлов, ни git, ни БД. Три хардкода, которые он заменяет (regex ключа, предикат
ядра, каталог брифов), были тремя независимыми каналами настройки; одного объекта
достаточно, и рассинхронизировать их между собой больше нечем.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping

from reviewer.services.task_keys import DEFAULT_KEY_PATTERN

# Дефолт воспроизводит прежний предикат rag_for_git один в один: ядро — это
# reviewer/**/*.py, plugin/** кроме *.md и корневые *.py; eval/ вне ядра.
DEFAULT_CORE_PATHS: tuple[str, ...] = (
    "reviewer/**/*.py",
    "plugin/**",
    "!plugin/**/*.md",
    "!eval/**",
    "*.py",
)
DEFAULT_BRIEFS_DIR = "docs/superpowers/briefs"


def _glob_to_regex(pattern: str) -> str:
    """glob → regex, где `**` пересекает `/`, а `*` и `?` — нет.

    fnmatch здесь непригоден: он не знает про `/`, поэтому `fnmatch(
    "reviewer/x.py", "*.py")` истинно, и правило «только корневые *.py»
    на нём невыразимо. Последовательность `**/` переводится в `(?:.*/)?`,
    иначе `reviewer/**/*.py` не совпал бы с `reviewer/app.py`.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return f"^{''.join(out)}$"


@lru_cache(maxsize=64)
def _compiled(patterns: tuple[str, ...]) -> tuple[tuple[re.Pattern, ...], tuple[re.Pattern, ...]]:
    """(позитивные, исключающие) скомпилированные паттерны набора."""
    positive: list[re.Pattern] = []
    negative: list[re.Pattern] = []
    for raw in patterns:
        if not raw:
            continue
        target = negative if raw.startswith("!") else positive
        target.append(re.compile(_glob_to_regex(raw.lstrip("!"))))
    return tuple(positive), tuple(negative)


@dataclass(frozen=True)
class BriefQualityConfig:
    """Настройка метрики для конкретного репозитория.

    `configured` — был ли `core_paths` задан явно. Без него «у репозитория
    в диффе нет файлов ядра» неотличимо от «репозиторий не настроен, и ядро
    посчитано чужой линейкой»: именно на этом различии стоит статус
    `unconfigured_core_denominator`.
    """

    core_paths: tuple[str, ...] = DEFAULT_CORE_PATHS
    key_pattern: str = DEFAULT_KEY_PATTERN
    briefs_dir: str = DEFAULT_BRIEFS_DIR
    configured: bool = False

    def matches_core(self, path: str) -> bool:
        """Путь принадлежит ядру: совпал с позитивным паттерном и ни с одним `!`."""
        positive, negative = _compiled(self.core_paths)
        if any(rx.match(path) for rx in negative):
            return False
        return any(rx.match(path) for rx in positive)

    @classmethod
    def from_review_yaml(
        cls, data: Mapping[str, object] | None, *, briefs_dir: str | None = None
    ) -> "BriefQualityConfig":
        """Собрать конфиг из данных `.review.yml` (уже разобранных в mapping)."""
        data = data or {}
        raw = data.get("metrics")
        section = raw.get("brief_quality") if isinstance(raw, Mapping) else None
        core_raw = section.get("core_paths") if isinstance(section, Mapping) else None
        # Tri-state как у ReviewPolicy._summary_paths_ignore: ключа нет или
        # значение None → дефолт; явный список, в том числе пустой → настроено.
        if core_raw is None:
            core_paths, configured = DEFAULT_CORE_PATHS, False
        else:
            core_paths, configured = tuple(str(item) for item in core_raw), True

        board = data.get("task_board")
        pattern = board.get("key_pattern") if isinstance(board, Mapping) else None
        return cls(
            core_paths=core_paths,
            key_pattern=str(pattern) if pattern else DEFAULT_KEY_PATTERN,
            briefs_dir=briefs_dir or DEFAULT_BRIEFS_DIR,
            configured=configured,
        )


DEFAULT = BriefQualityConfig()
