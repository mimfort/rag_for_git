"""Пути-кандидаты из отрендеренного вывода ретрива (PRI-254).

Парсится ИМЕННО отрендеренный текст, а не объект ContextPack: as_context
обрезает вывод по max_context_chars, и часть найденных путей до сборщика
брифа не доезжает. Чтение items напрямую приписало бы ретриву кандидатов,
которых сборщик брифа не видел, и завысило бы метрику.
"""
from __future__ import annotations

import re

# Заголовок блока: '// <node_id> (<path>:<start>-<end>)'. Диапазон строк
# обязателен: обрезанный по max_context_chars заголовок его не имеет, и
# такой блок отбрасывается, а не достраивается догадкой.
_HEADER_RE = re.compile(r"^//\s+\S+\s+\(([^()]+):\d+-\d+\)$")


def extract_context_paths(text: str) -> set:
    """Множество путей из заголовков блоков вывода ретрива."""
    paths: set = set()
    for line in (text or "").splitlines():
        match = _HEADER_RE.match(line.strip())
        if match:
            paths.add(match.group(1).strip())
    return paths
