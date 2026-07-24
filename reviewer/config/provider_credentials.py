"""Безопасное разрешение server-side credential-полей провайдеров досок."""
from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from dotenv import dotenv_values

from reviewer.config.settings import _resolve_env_file

if TYPE_CHECKING:
    from reviewer.tasks.boards.registry import BoardProviderRegistry, BoardProviderSpec


@dataclass(frozen=True)
class ResolvedProviderCredentials:
    """Значения для factory и метаданные, безопасные для внешнего boundary."""

    values: Mapping[str, str]
    safe_metadata: dict[str, bool | list[str]]


class ProviderCredentialSource:
    """Process env поверх один раз прочитанного resolved ``.env`` файла."""

    def __init__(
        self,
        *,
        env_file: str | Path | None = None,
        values: Mapping[str, str] | None = None,
    ) -> None:
        # ``values`` — уже разрешённый snapshot (например, из Settings): повторно
        # читать process-wide .env нельзя, иначе тестовый/внедрённый source загрязняется.
        path = Path(env_file) if env_file is not None else Path(_resolve_env_file())
        self._file_values = (
            {
                key: value
                for key, value in dotenv_values(path).items()
                if value is not None
            }
            if values is None or env_file is not None
            else {}
        )
        self._process_values = dict(os.environ if values is None else values)

    @classmethod
    def from_settings(cls, settings: object) -> "ProviderCredentialSource":
        """Registry credentials из общего env/.env с приоритетом Settings."""
        dump = getattr(settings, "model_dump", None)
        if not callable(dump):
            return cls()
        env_file = getattr(settings, "_provider_env_file", _resolve_env_file())
        source = cls(env_file=env_file) if env_file is not None else cls(values=os.environ)
        settings_values = {
            key.upper(): "" if value is None else str(value)
            for key, value in dump().items()
        }
        source._process_values.update(settings_values)
        # Settings уже разрешил свои поля с учётом init/env/.env. Даже явное
        # пустое значение — override; не даём нижнему file-layer вернуть его назад.
        for key in settings_values:
            source._file_values.pop(key, None)
        return source

    def resolve(self, spec: BoardProviderSpec) -> ResolvedProviderCredentials:
        values: dict[str, str] = {}
        missing: list[str] = []
        for field in spec.credential_fields:
            value = self._lookup(field.env, field.aliases)
            if not value:
                value = field.default
            values[field.env] = value
            if field.required and not value:
                missing.append(field.env)
        return ResolvedProviderCredentials(
            values=MappingProxyType(values),
            safe_metadata={"configured": not missing, "missing": missing},
        )

    def is_configured(self, spec: BoardProviderSpec) -> bool:
        return bool(self.resolve(spec).safe_metadata["configured"])

    def configured_types(
        self, registry_or_specs: BoardProviderRegistry | Iterable[BoardProviderSpec]
    ) -> tuple[str, ...]:
        specs = self._specs(registry_or_specs)
        return tuple(spec.board_type for spec in specs if self.is_configured(spec))

    def secret_values(
        self, spec_or_specs: BoardProviderSpec | Iterable[BoardProviderSpec]
    ) -> frozenset[str]:
        specs = self._specs(spec_or_specs)
        return frozenset(
            resolved.values[field.env]
            for spec in specs
            for resolved in (self.resolve(spec),)
            for field in spec.credential_fields
            if field.secret and resolved.values[field.env]
        )

    def _lookup(self, env: str, aliases: tuple[str, ...]) -> str:
        for key in (env, *aliases):
            value = self._process_values.get(key)
            if value:
                return value
        for key in (env, *aliases):
            value = self._file_values.get(key)
            if value:
                return value
        return ""

    @staticmethod
    def _specs(
        registry_or_specs: BoardProviderRegistry | BoardProviderSpec | Iterable[BoardProviderSpec],
    ) -> tuple[BoardProviderSpec, ...]:
        if hasattr(registry_or_specs, "registered_types") and hasattr(registry_or_specs, "get"):
            return tuple(
                registry_or_specs.get(board_type)
                for board_type in registry_or_specs.registered_types()
            )
        if hasattr(registry_or_specs, "board_type"):
            return (registry_or_specs,)
        return tuple(registry_or_specs)
