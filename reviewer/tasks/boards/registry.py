"""Явный реестр полных провайдеров досок задач."""
from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from reviewer.tasks.boards.base import JsonValue, TaskBoardProvider

if TYPE_CHECKING:
    from reviewer.config.provider_credentials import ProviderCredentialSource
    from reviewer.tasks.boards.setup import SetupIO


@dataclass(frozen=True)
class CredentialFieldSpec:
    env: str
    label: str
    secret: bool = False
    required: bool = True
    default: str = ""
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderOptionSpec:
    key: str
    label: str
    required_for: tuple[Literal["sync", "create", "finish"], ...] = ()


@dataclass(frozen=True)
class ProviderSetupSpec:
    label: str
    help_url: str
    help_text: str
    acquisition: Callable[[SetupIO], dict[str, str]] | None = None
    help_url_builder: Callable[[Mapping[str, str]], str] | None = None


@dataclass(frozen=True)
class ProviderBuildContext:
    credentials: Mapping[str, str]
    options: Mapping[str, JsonValue]
    key_pattern: str
    url_template: str
    attachment_max_bytes: int
    attachment_timeout: float
    attachment_store_chars: int


@dataclass(frozen=True)
class BoardProviderSpec:
    board_type: str
    factory: Callable[[ProviderBuildContext], TaskBoardProvider]
    credential_fields: tuple[CredentialFieldSpec, ...]
    setup: ProviderSetupSpec
    option_fields: tuple[ProviderOptionSpec, ...] = ()
    default_api_base: str = ""
    create_target_label: str = "Create target"
    done_target_label: str = "Done target"


_REQUIRED_PROVIDER_MEMBERS = (
    "validate_connection",
    "iter_raw",
    "normalize",
    "normalize_meta",
    "fetch_one",
    "list_targets",
    "create",
    "finish",
    "close",
)
_MISSING = object()


def _contains_secret(value: JsonValue, secrets: frozenset[str]) -> bool:
    if isinstance(value, str):
        return value in secrets or any(
            len(secret) >= 8 and secret in value
            for secret in secrets
        )
    if isinstance(value, list):
        return any(_contains_secret(item, secrets) for item in value)
    if isinstance(value, dict):
        return any(_contains_secret(key, secrets) or _contains_secret(item, secrets)
                   for key, item in value.items())
    return False


class BoardProviderRegistry:
    """Хранит allowlist типов досок и создаёт только полные провайдеры."""

    def __init__(self, specs: tuple[BoardProviderSpec, ...] | list[BoardProviderSpec] = ()):
        self._specs: dict[str, BoardProviderSpec] = {}
        self._credential_fields: dict[str, CredentialFieldSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: BoardProviderSpec) -> None:
        self._validate_spec(spec)
        if spec.board_type in self._specs:
            raise ValueError(f"board provider already registered: {spec.board_type}")
        for field in spec.credential_fields:
            for env in (field.env, *field.aliases):
                previous = self._credential_fields.get(env)
                if previous is not None and previous != field:
                    raise ValueError(f"incompatible credential declaration: {env}")
        self._specs[spec.board_type] = spec
        for field in spec.credential_fields:
            for env in (field.env, *field.aliases):
                self._credential_fields.setdefault(env, field)

    def get(self, board_type: str) -> BoardProviderSpec:
        try:
            return self._specs[board_type]
        except KeyError:
            raise KeyError(f"unknown board provider: {board_type}") from None

    def registered_types(self) -> tuple[str, ...]:
        """Типы в предсказуемом порядке регистрации."""
        return tuple(self._specs)

    def configured_types(self, credentials: ProviderCredentialSource) -> tuple[str, ...]:
        return credentials.configured_types(self)

    def create(
        self,
        board_type: str,
        *,
        credentials: Mapping[str, str],
        options: Mapping[str, JsonValue],
        build_defaults: Mapping[str, object],
    ) -> TaskBoardProvider:
        spec = self.get(board_type)
        credential_envs = {
            env
            for field in spec.credential_fields
            for env in (field.env, *field.aliases)
        }
        prohibited = sorted(set(options) & credential_envs)
        if prohibited:
            raise ValueError("credentials must not be provider options")
        secret_values = frozenset(
            credentials[field.env]
            for field in spec.credential_fields
            if field.secret and credentials.get(field.env)
        )
        if _contains_secret(dict(options), secret_values):
            raise ValueError("provider options must not contain a secret value")
        known_options = {field.key for field in spec.option_fields}
        unknown = sorted(set(options) - known_options)
        if unknown:
            raise ValueError("unknown provider option")
        context = ProviderBuildContext(
            credentials=MappingProxyType(dict(credentials)),
            options=MappingProxyType(dict(options)),
            key_pattern=str(build_defaults["key_pattern"]),
            url_template=str(build_defaults["url_template"]),
            attachment_max_bytes=int(build_defaults["attachment_max_bytes"]),
            attachment_timeout=float(build_defaults["attachment_timeout"]),
            attachment_store_chars=int(build_defaults["attachment_store_chars"]),
        )
        provider = spec.factory(context)
        try:
            self._validate_runtime_provider(provider, board_type)
        except Exception:
            close = getattr(provider, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
            raise
        return provider

    def _validate_spec(self, spec: BoardProviderSpec) -> None:
        if not spec.board_type or spec.board_type != spec.board_type.strip():
            raise ValueError("board_type must not be empty or surrounded by whitespace")
        if not callable(spec.factory):
            raise TypeError("provider factory must be callable")
        if not spec.setup.label or not spec.setup.help_url or not spec.setup.help_text:
            raise ValueError("provider setup metadata must be complete")
        seen_envs: set[str] = set()
        for field in spec.credential_fields:
            if not field.env or not field.label:
                raise ValueError("credential metadata must include env and label")
            for env in (field.env, *field.aliases):
                if not env or env in seen_envs:
                    raise ValueError(f"duplicate credential environment key: {env}")
                seen_envs.add(env)
        seen_options: set[str] = set()
        for option in spec.option_fields:
            if not option.key or not option.label:
                raise ValueError("provider option metadata must include key and label")
            if option.key in seen_options:
                raise ValueError(f"duplicate provider option: {option.key}")
            if option.key in seen_envs:
                raise ValueError("credentials must not be provider options")
            seen_options.add(option.key)

    @staticmethod
    def _validate_runtime_provider(provider: object, board_type: str) -> None:
        for name in _REQUIRED_PROVIDER_MEMBERS:
            member = inspect.getattr_static(provider, name, _MISSING)
            if member is _MISSING or not callable(member):
                raise TypeError(f"provider {board_type!r} is missing required method: {name}")
        if inspect.getattr_static(provider, "board_type", _MISSING) is _MISSING:
            raise TypeError(f"provider {board_type!r} is missing required attribute: board_type")
        if provider.board_type != board_type:
            raise TypeError(
                f"provider board_type {provider.board_type!r} does not match registered board_type "
                f"{board_type!r}"
            )


@lru_cache(maxsize=1)
def default_board_registry() -> BoardProviderRegistry:
    """Production registry только полностью реализованных адаптеров.

    Локальные явные импорты не создают цикл при инициализации modules.
    """
    from reviewer.tasks.boards.jira import provider_spec as jira_provider_spec
    from reviewer.tasks.boards.yougile import provider_spec as yougile_provider_spec
    from reviewer.tasks.boards.youtrack import provider_spec as youtrack_provider_spec

    return BoardProviderRegistry([
        yougile_provider_spec(),
        youtrack_provider_spec(),
        jira_provider_spec(),
    ])
