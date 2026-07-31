"""Нормализация board-конфига и migration legacy аргументов на target/options."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from reviewer.tasks.boards.base import JsonValue


@dataclass(frozen=True)
class TaskSyncFilter:
    """Общие ограничения набора задач для синхронизации."""

    max_age_days: int | None = None
    include_archived: bool = True

    def as_dict(self) -> dict[str, JsonValue]:
        """Вернуть только значения, отличающиеся от значений по умолчанию."""
        result: dict[str, JsonValue] = {}
        if self.max_age_days is not None:
            result["max_age_days"] = self.max_age_days
        if not self.include_archived:
            result["include_archived"] = False
        return result

    def canonical_dict(self) -> dict[str, JsonValue]:
        """Вернуть полную каноническую форму с явными значениями по умолчанию."""
        return {
            "max_age_days": self.max_age_days,
            "include_archived": self.include_archived,
        }


@dataclass(frozen=True)
class TaskBoardConfig:
    """Нормализованный, не содержащий credential-ов конфиг task board."""

    board_type: str | tuple[str, ...] | None
    mcp: str | None
    project: str | None
    key_pattern: str | None
    url_template: str | None
    create_target: str | None
    done_target: str | None
    options: Mapping[str, JsonValue]
    warnings: tuple[str, ...] = ()
    sync_filter: TaskSyncFilter | None = None

    def as_dict(self) -> dict[str, object]:
        """Совместимая для callers плоская форма блока ``task_board``."""
        result: dict[str, object] = {}
        for field, value in (
            ("type", self.board_type),
            ("mcp", self.mcp),
            ("project", self.project),
            ("key_pattern", self.key_pattern),
            ("url_template", self.url_template),
            ("create_target", self.create_target),
            ("done_target", self.done_target),
        ):
            if value is not None:
                result[field] = value
        if self.options:
            result["options"] = dict(self.options)
        if self.sync_filter is not None:
            result["sync_filter"] = self.sync_filter.as_dict()
        return result


def _migrate_legacy_values(
    *,
    target: str | None,
    options: Mapping[str, JsonValue] | None,
    done_state: str | None,
    status_field: str | None,
    done_column: str | None,
    legacy_prefix: str,
    target_name: str,
    option_name: str,
    targets_first: bool,
) -> tuple[str | None, dict[str, JsonValue], list[str]]:
    """Общие правила приоритета generic формы над legacy полями."""
    normalized_options = dict(options or {})
    warnings: list[str] = []

    def migrate_status_field() -> None:
        if status_field is None:
            return
        if "status_field" in normalized_options:
            warnings.append(
                f"{legacy_prefix}status_field ignored because {option_name} is set"
            )
        else:
            normalized_options["status_field"] = status_field
            warnings.append(
                f"{legacy_prefix}status_field migrated to {option_name}"
            )

    def migrate_targets() -> None:
        nonlocal target
        legacy_targets = (
            ("done_column", done_column),
            ("done_state", done_state),
        )
        if target is not None:
            warnings.extend(
                f"{legacy_prefix}{name} ignored because {target_name} is set"
                for name, value in legacy_targets
                if value is not None
            )
            return
        for name, value in legacy_targets:
            if value is None:
                continue
            if target is None:
                target = value
                warnings.append(f"{legacy_prefix}{name} migrated to {target_name}")
            else:
                warnings.append(
                    f"{legacy_prefix}{name} ignored because {target_name} is already migrated"
                )

    if targets_first:
        migrate_targets()
        migrate_status_field()
    else:
        migrate_status_field()
        migrate_targets()
    return target, normalized_options, warnings


def migrate_legacy_board_args(
    *,
    target: str | None,
    provider_options: Mapping[str, JsonValue] | None,
    done_state: str | None = None,
    status_field: str | None = None,
    done_column: str | None = None,
) -> tuple[str | None, dict[str, JsonValue], list[str]]:
    """Новая форма имеет приоритет; legacy преобразуется с явными warnings."""
    return _migrate_legacy_values(
        target=target,
        options=provider_options,
        done_state=done_state,
        status_field=status_field,
        done_column=done_column,
        legacy_prefix="legacy ",
        target_name="target",
        option_name="provider_options.status_field",
        targets_first=False,
    )


def _optional_string(raw: Mapping[str, object], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"task_board.{key} must be a string")
    return value


def _board_type(raw: Mapping[str, object]) -> str | tuple[str, ...] | None:
    value = raw.get("type")
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ValueError("task_board.type must be a string or list of strings")


def _json_mapping(value: object, *, field: str) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping")

    def normalize(item: object) -> JsonValue:
        if item is None or isinstance(item, (bool, int, float, str)):
            return item
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, Mapping):
            if not all(isinstance(key, str) for key in item):
                raise ValueError(f"{field} must contain string keys")
            return {key: normalize(child) for key, child in item.items()}
        raise ValueError(f"{field} must contain JSON values")

    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field} must contain string keys")
    return {key: normalize(item) for key, item in value.items()}


def normalize_task_sync_filter(raw: object) -> TaskSyncFilter:
    """Проверить и нормализовать общий фильтр синхронизации задач."""
    values = _json_mapping(raw, field="task_board.sync_filter")
    if set(values) - {"max_age_days", "include_archived"}:
        raise ValueError("task_board.sync_filter contains unknown fields")

    max_age_days = values.get("max_age_days")
    if max_age_days is not None and (
        not isinstance(max_age_days, int)
        or isinstance(max_age_days, bool)
        or max_age_days < 1
    ):
        raise ValueError("task_board.sync_filter.max_age_days must be an integer >= 1 or null")

    include_archived = values.get("include_archived", True)
    if not isinstance(include_archived, bool):
        raise ValueError("task_board.sync_filter.include_archived must be a boolean")

    return TaskSyncFilter(
        max_age_days=max_age_days,
        include_archived=include_archived,
    )


def _secret_env_names() -> frozenset[str]:
    """Credential names come only from registry metadata, never from config values."""
    from reviewer.tasks.boards.registry import default_board_registry

    registry = default_board_registry()
    return frozenset(
        env
        for board_name in registry.registered_types()
        for spec in (registry.get(board_name),)
        for credential in spec.credential_fields
        if credential.secret
        for env in (credential.env, *credential.aliases)
    )


def _contains_secret_key(value: JsonValue, secret_names: frozenset[str]) -> bool:
    if isinstance(value, dict):
        return any(
            key in secret_names or _contains_secret_key(item, secret_names)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret_key(item, secret_names) for item in value)
    return False


def normalize_task_board_config(raw: Mapping[str, object] | None) -> TaskBoardConfig | None:
    """Normalize `.review.yml` task board config and migrate one-release legacy keys.

    The returned config deliberately excludes credentials.  Its warnings are metadata
    for callers, not provider options, so they can never reach board adapters.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("task_board must be a mapping")
    if not raw:
        return None

    board_type = _board_type(raw)
    raw_options = raw.get("options", {})
    options = _json_mapping(raw_options, field="task_board.options")
    if _contains_secret_key(_json_mapping(raw, field="task_board"), _secret_env_names()):
        raise ValueError("task_board must not contain credentials")
    sync_filter = (
        normalize_task_sync_filter(raw["sync_filter"])
        if "sync_filter" in raw
        else None
    )
    done_target, options, warnings = _migrate_legacy_values(
        target=_optional_string(raw, "done_target"),
        options=options,
        done_state=_optional_string(raw, "done_state"),
        status_field=_optional_string(raw, "status_field"),
        done_column=_optional_string(raw, "done_column"),
        legacy_prefix="task_board.",
        target_name="done_target",
        option_name="options.status_field",
        targets_first=True,
    )
    return TaskBoardConfig(
        board_type=board_type,
        mcp=_optional_string(raw, "mcp"),
        project=_optional_string(raw, "project"),
        key_pattern=_optional_string(raw, "key_pattern"),
        url_template=_optional_string(raw, "url_template"),
        create_target=_optional_string(raw, "create_target"),
        done_target=done_target,
        options=MappingProxyType(options),
        warnings=tuple(warnings),
        sync_filter=sync_filter,
    )
