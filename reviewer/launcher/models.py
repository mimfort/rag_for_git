"""Модели результата интерактивного launcher."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LauncherResult:
    """Результат выбора команды в интерактивном launcher."""

    argv: tuple[str, ...] | None = field(repr=False)
    exit_code: int
