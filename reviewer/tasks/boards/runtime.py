"""Единый runtime lifecycle зарегистрированного board provider."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass

from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.config.settings import Settings
from reviewer.tasks.boards.base import JsonValue, TaskBoardProvider
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.registry import BoardProviderRegistry


@dataclass(frozen=True)
class ResolvedProvider:
    board_type: str
    provider: TaskBoardProvider
    secrets: frozenset[str]
    capabilities: frozenset[str]


@contextmanager
def resolved_provider(
    settings: Settings,
    board_type: str | None,
    provider_options: Mapping[str, JsonValue] | None,
    *,
    registry: BoardProviderRegistry,
    credential_source: ProviderCredentialSource,
) -> Iterator[ResolvedProvider]:
    """Выбрать, создать, безопасно выполнить и гарантированно закрыть provider."""
    configured = registry.configured_types(credential_source)
    if board_type is None:
        if len(configured) != 1:
            raise BoardProviderError(
                "configuration",
                "board_type is required unless exactly one provider is configured.",
            )
        board_type = configured[0]
    try:
        spec = registry.get(board_type)
    except KeyError:
        raise BoardProviderError(
            "configuration",
            "Board provider type is not registered.",
        ) from None
    if board_type not in configured:
        raise BoardProviderError(
            "configuration",
            "Board provider is not configured.",
        )

    resolved_credentials = credential_source.resolve(spec)
    secrets = credential_source.secret_values(spec)
    try:
        provider = registry.create(
            board_type,
            credentials=resolved_credentials.values,
            options=provider_options or {},
            build_defaults={
                "key_pattern": settings.task_board_key_pattern,
                "url_template": settings.task_board_url_template,
                "attachment_max_bytes": settings.task_attachment_max_bytes,
                "attachment_timeout": settings.task_attachment_timeout,
                "attachment_store_chars": settings.task_attachment_store_chars,
            },
        )
    except BoardProviderError as error:
        raise BoardProviderError(
            error.category,
            error.message,
            hint=error.hint,
            retryable=error.retryable,
            secrets=secrets,
        ) from None
    except (TypeError, ValueError):
        raise BoardProviderError(
            "configuration",
            "Board provider configuration is invalid.",
            secrets=secrets,
        ) from None
    except Exception:
        raise BoardProviderError(
            "configuration",
            "Board provider initialization failed.",
            secrets=secrets,
        ) from None

    try:
        yield ResolvedProvider(board_type, provider, secrets, spec.capabilities)
    except BoardProviderError as error:
        raise BoardProviderError(
            error.category,
            error.message,
            hint=error.hint,
            retryable=error.retryable,
            secrets=secrets,
        ) from None
    except Exception as error:
        raise BoardProviderError(
            "unsupported",
            str(error),
            hint="Check the board provider operation and configuration.",
            secrets=secrets,
        ) from None
    finally:
        with suppress(Exception):
            provider.close()
