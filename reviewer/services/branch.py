"""Резолв ветки для ветка-агностичных операций (CLI search, solve-task).

Правило: явный запрос (если в allowlist) → текущая git-ветка (если в allowlist)
→ первичная ветка. Граф задач остаётся branch-agnostic — ветвится только код-ретрив.
"""
from __future__ import annotations

import subprocess

from reviewer.config.branches import RepoBranches


def resolve_branch(requested: str | None, current: str | None,
                   branches: RepoBranches) -> str:
    """Выбрать ветку: явный запрос → текущая git-ветка → первичная."""
    if requested:
        if requested not in branches.index:
            raise ValueError(
                f"ветка {requested!r} не отслеживается для этого репозитория "
                f"({list(branches.index)}; источник: {branches.source})"
            )
        return requested
    if current and current in branches.index:
        return current
    return branches.primary


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
