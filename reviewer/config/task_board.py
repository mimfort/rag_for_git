"""Узкая миграция legacy board-аргументов на generic target/options."""
from __future__ import annotations

from collections.abc import Mapping

from reviewer.tasks.boards.base import JsonValue


def migrate_legacy_board_args(
    *,
    target: str | None,
    provider_options: Mapping[str, JsonValue] | None,
    done_state: str | None = None,
    status_field: str | None = None,
    done_column: str | None = None,
) -> tuple[str | None, dict[str, JsonValue], list[str]]:
    """Новая форма имеет приоритет; legacy преобразуется с явными warnings."""
    options = dict(provider_options or {})
    warnings: list[str] = []

    if status_field is not None:
        if "status_field" in options:
            warnings.append(
                "legacy status_field ignored because provider_options.status_field is set"
            )
        else:
            options["status_field"] = status_field
            warnings.append(
                "legacy status_field migrated to provider_options.status_field"
            )

    legacy_targets = (
        ("done_column", done_column),
        ("done_state", done_state),
    )
    if target is not None:
        warnings.extend(
            f"legacy {name} ignored because target is set"
            for name, value in legacy_targets
            if value is not None
        )
    else:
        for name, value in legacy_targets:
            if value is None:
                continue
            if target is None:
                target = value
                warnings.append(f"legacy {name} migrated to target")
            else:
                warnings.append(f"legacy {name} ignored because target is already migrated")

    return target, options, warnings
