"""Провайдеры досок задач: фабрика по типу + реэкспорт интерфейса."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress

from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.tasks.boards.base import RawTask, TaskBoardProvider, project_prefix
from reviewer.tasks.boards.base import JsonValue
from reviewer.tasks.boards.registry import BoardProviderRegistry, default_board_registry

__all__ = ["RawTask", "TaskBoardProvider", "project_prefix", "make_board_provider",
           "make_board_providers"]


def make_board_provider(
    settings,
    type_: str,
    *,
    provider_options: Mapping[str, JsonValue] | None = None,
    registry: BoardProviderRegistry | None = None,
    credential_source: ProviderCredentialSource | None = None,
) -> TaskBoardProvider | None:
    """Создать настроенный provider через registry; unknown/unconfigured → None."""
    registry = registry or default_board_registry()
    source = credential_source or ProviderCredentialSource.from_settings(settings)
    try:
        spec = registry.get(type_)
    except KeyError:
        return None
    resolved = source.resolve(spec)
    if not resolved.safe_metadata["configured"]:
        return None
    return registry.create(
        type_,
        credentials=resolved.values,
        options=provider_options or {},
        build_defaults={
            "key_pattern": settings.task_board_key_pattern,
            "url_template": settings.task_board_url_template,
            "attachment_max_bytes": settings.task_attachment_max_bytes,
            "attachment_timeout": settings.task_attachment_timeout,
            "attachment_store_chars": settings.task_attachment_store_chars,
        },
    )


def make_board_providers(
    settings,
    *,
    registry: BoardProviderRegistry | None = None,
    credential_source: ProviderCredentialSource | None = None,
) -> list[TaskBoardProvider]:
    """Все настроенные доски (по configured_board_types) — для мульти-синка."""
    registry = registry or default_board_registry()
    source = credential_source or ProviderCredentialSource.from_settings(settings)
    out: list[TaskBoardProvider] = []
    try:
        for type_ in registry.configured_types(source):
            provider = make_board_provider(
                settings,
                type_,
                registry=registry,
                credential_source=source,
            )
            if provider is not None:
                out.append(provider)
    except Exception:
        for provider in out:
            with suppress(Exception):
                provider.close()
        raise
    return out
