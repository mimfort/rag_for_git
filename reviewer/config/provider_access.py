"""Общие несекретные подсказки по доступу внешнего provider."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderAccessSpec:
    """Минимальные права и фактические операции reviewer."""

    minimum_permissions: str
    read_operations: tuple[str, ...]
    write_operations: tuple[str, ...]
    validation: str

    def __post_init__(self) -> None:
        for name in ("minimum_permissions", "validation"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        for name in ("read_operations", "write_operations"):
            values = getattr(self, name)
            if not values or any(not value.strip() for value in values):
                raise ValueError(f"{name} must not be empty")


def render_provider_access(
    *,
    label: str,
    help_text: str,
    help_url: str,
    access: ProviderAccessSpec,
) -> str:
    """Сформировать одинаковую provider setup-подсказку без credentials."""
    return "\n".join(
        (
            f"[{label}]",
            help_text,
            f"Минимальные права: {access.minimum_permissions}",
            f"Чтение: {'; '.join(access.read_operations)}",
            f"Запись: {'; '.join(access.write_operations)}",
            f"Проверка: {access.validation}",
            f"Официальная инструкция: {help_url}",
        )
    )
