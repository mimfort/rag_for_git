#!/usr/bin/env python3
"""Извлечение секции одной версии из CHANGELOG.md.

Используется джобой `release` в CI: тело GitHub Release — это ровно та секция
`CHANGELOG.md`, что относится к публикуемой версии. Отдельный модуль (а не
inline-скрипт в workflow) — чтобы разбор был покрыт unit-тестами: молчаливо
выданная чужая секция хуже отсутствующих нот.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Заголовок версии: "## 0.4.10 — 2026-08-12" или "## 0.4.10". Дата не обязательна.
_HEADING = re.compile(r"^##\s+v?(?P<version>\d+\.\d+\.\d+)\s*(?:[—\-–].*)?$")


class SectionNotFoundError(LookupError):
    """В CHANGELOG нет непустой секции для запрошенной версии."""


def extract_section(changelog: str, version: str) -> str:
    """Вернуть тело секции `version` без её заголовка.

    Сравнение идёт по разобранной версии из заголовка, а не по подстроке:
    иначе `0.4.1` совпала бы с заголовком `0.4.10`.
    """
    wanted = version.lstrip("vV").strip()
    lines = changelog.splitlines()

    start: int | None = None
    end = len(lines)
    for index, line in enumerate(lines):
        match = _HEADING.match(line.strip())
        if match is None:
            continue
        if start is None and match.group("version") == wanted:
            start = index + 1
        elif start is not None:
            end = index
            break

    if start is None:
        raise SectionNotFoundError(f"нет секции для версии {wanted}")

    body = "\n".join(lines[start:end]).strip()
    if not body:
        raise SectionNotFoundError(f"секция версии {wanted} пуста")
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="версия релиза, например 0.4.11")
    parser.add_argument(
        "--changelog",
        type=Path,
        default=Path("CHANGELOG.md"),
        help="путь к CHANGELOG.md (по умолчанию — в текущем каталоге)",
    )
    args = parser.parse_args(argv)

    try:
        text = args.changelog.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"не удалось прочитать {args.changelog}: {exc}", file=sys.stderr)
        return 1

    try:
        print(extract_section(text, args.version))
    except SectionNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
