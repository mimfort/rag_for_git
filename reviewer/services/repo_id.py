"""Канонизация и вывод идентификатора репозитория 'owner/name'."""
from __future__ import annotations

import re

_REMOTE_RE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")


def normalize_repo(repo: str) -> str:
    """Привести 'Owner/Repo' к канону 'owner/name' (нижний регистр).

    :raises ValueError: пустая строка или не ровно один '/'.
    """
    s = (repo or "").strip().lower()
    parts = s.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Некорректный repo (ожидается owner/name): {repo!r}")
    return f"{parts[0]}/{parts[1]}"


def derive_repo_from_remote(remote_url: str) -> str | None:
    """Вывести 'owner/name' из git remote URL GitHub. None, если не распознан."""
    m = _REMOTE_RE.search((remote_url or "").strip())
    if not m:
        return None
    return f"{m.group(1).lower()}/{m.group(2).lower()}"
