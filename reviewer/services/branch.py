"""Резолв ветки для ветка-агностичных операций (CLI search, solve-task).

Правило: явный запрос (если в allowlist) → текущая git-ветка (если в allowlist)
→ первичная ветка. Граф задач остаётся branch-agnostic — ветвится только код-ретрив.
"""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reviewer.config.settings import Settings


def resolve_branch(requested: str | None, current: str | None, settings: Settings) -> str:
    allow = settings.review_branches_list()
    if requested:
        if requested not in allow:
            raise ValueError(
                f"ветка {requested!r} не в REVIEW_BRANCHES ({allow})"
            )
        return requested
    if current and current in allow:
        return current
    return settings.primary_branch()


def current_git_branch(path: str = ".") -> str | None:
    """Имя текущей git-ветки клона, или None (detached HEAD / не git / ошибка)."""
    try:
        out = subprocess.run(
            ["git", "-C", path, "branch", "--show-current"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    name = out.stdout.strip()
    return name or None
