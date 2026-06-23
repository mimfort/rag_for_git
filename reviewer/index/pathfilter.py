from __future__ import annotations

from fnmatch import fnmatch

_GLOB_CHARS = ("*", "?", "[")


def is_ignored(path: str, patterns: list[str]) -> bool:
    """Путь под одним из ignore-паттернов (fnmatch).

    Нормализация: голый паттерн без glob-метасимволов (``* ? [``) — ``"dir"`` или
    ``"a/b"`` — матчит и сам путь, и его поддерево (эквивалент fnmatch против ``pat``
    и ``pat + "/*"``), чтобы «папка и всё внутри» работало без явного ``/*``.
    Паттерны с glob (``"vendor/*"``, ``"*.gen.py"``) матчатся fnmatch как есть.
    """
    for pat in patterns:
        if not pat:
            continue
        if fnmatch(path, pat):
            return True
        if not any(ch in pat for ch in _GLOB_CHARS):
            prefix = pat.rstrip("/")
            if fnmatch(path, f"{prefix}/*"):
                return True
    return False
