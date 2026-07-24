"""Безопасные ошибки и редактирование секретов для board providers."""
from __future__ import annotations

from collections.abc import Collection

_CATEGORIES = frozenset(
    {
        "configuration",
        "authentication",
        "permission",
        "not_found",
        "rate_limit",
        "transient",
        "unsupported",
    }
)


def sanitize_provider_text(value: object, secrets: Collection[str] = ()) -> str:
    """Скрыть literal-секреты в любом тексте до log/MCP boundary.

    Длинные значения обрабатываются первыми, чтобы секрет-префикс не оставлял
    хвост более длинного секрета в query/header/исключении.
    """
    rendered = str(value)
    for secret in sorted({str(item) for item in secrets if item}, key=len, reverse=True):
        rendered = rendered.replace(secret, "[REDACTED]")
    return rendered


def sanitize_provider_payload(value: object, secrets: Collection[str] = ()) -> object:
    """Рекурсивно скрыть секреты в provider result до внешней границы."""
    if isinstance(value, str):
        return sanitize_provider_text(value, secrets)
    if isinstance(value, list):
        return [sanitize_provider_payload(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_provider_payload(item, secrets) for item in value)
    if isinstance(value, dict):
        return {
            sanitize_provider_text(key, secrets): sanitize_provider_payload(item, secrets)
            for key, item in value.items()
        }
    return value


class BoardProviderError(RuntimeError):
    """Ошибка провайдера, пригодная для безопасного показа пользователю."""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        hint: str = "",
        retryable: bool = False,
        secrets: Collection[str] = (),
    ) -> None:
        if category not in _CATEGORIES:
            raise ValueError(f"unknown board provider error category: {category}")
        self.category = category
        self.message = sanitize_provider_text(message, secrets)
        self.hint = sanitize_provider_text(hint, secrets)
        self.retryable = retryable
        super().__init__(self.message)

    def __repr__(self) -> str:
        return (
            f"BoardProviderError(category={self.category!r}, message={self.message!r}, "
            f"hint={self.hint!r}, retryable={self.retryable!r})"
        )
