"""Безопасное построение argv и его отображаемого preview."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import platform
import shlex
import subprocess
from typing import AbstractSet

from reviewer.launcher.models import CommandSpec, ParameterSpec


_MASK = "••••••"


@dataclass(frozen=True)
class PreparedCommand:
    argv: tuple[str, ...] = field(repr=False)
    preview: str


def prepare_command(
    spec: CommandSpec,
    values: Mapping[str, object],
    changed: AbstractSet[str],
    *,
    platform_name: str | None = None,
) -> PreparedCommand:
    """Собрать аргументы процесса и безопасный preview без shell-интерпретации."""
    argv = list(spec.path)
    masked = list(spec.path)
    option_argv: list[str] = []
    masked_options: list[str] = []
    positional_argv: list[str] = []
    masked_positionals: list[str] = []
    for parameter in spec.params:
        target_argv, target_masked = (
            (positional_argv, masked_positionals)
            if parameter.kind == "argument"
            else (option_argv, masked_options)
        )
        _append_parameter(target_argv, target_masked, parameter, values, changed)
    argv.extend(option_argv)
    masked.extend(masked_options)
    if any(token.startswith("-") for token in positional_argv):
        argv.append("--")
        masked.append("--")
    argv.extend(positional_argv)
    masked.extend(masked_positionals)
    return PreparedCommand(
        argv=tuple(argv),
        preview=_format_preview(("reviewer", *masked), platform_name),
    )


def _append_parameter(
    argv: list[str],
    masked: list[str],
    parameter: ParameterSpec,
    values: Mapping[str, object],
    changed: AbstractSet[str],
) -> None:
    if parameter.name not in values:
        return
    value = values[parameter.name]
    if parameter.kind == "argument":
        if value is None or (not parameter.required and parameter.name not in changed):
            return
        _append_values(argv, masked, _values(value, parameter), parameter.sensitive)
        return
    if parameter.name not in changed:
        return
    if parameter.count:
        _append_repeated_option(argv, masked, parameter, value)
        return
    if parameter.is_flag:
        _append_flag(argv, masked, parameter, value)
        return
    if value is None:
        return
    if parameter.multiple:
        for item in _values(value, parameter, split_multiple=True):
            _append_option(argv, masked, parameter, (item,))
        return
    _append_option(argv, masked, parameter, _values(value, parameter))


def _append_repeated_option(
    argv: list[str], masked: list[str], parameter: ParameterSpec, value: object
) -> None:
    if not isinstance(value, int) or value < 1:
        return
    option = parameter.option_strings[0]
    argv.extend([option] * value)
    masked.extend([option] * value)


def _append_flag(
    argv: list[str], masked: list[str], parameter: ParameterSpec, value: object
) -> None:
    if value:
        option = parameter.option_strings[0]
    elif parameter.secondary_strings:
        option = parameter.secondary_strings[0]
    else:
        return
    argv.append(option)
    masked.append(option)


def _append_option(
    argv: list[str], masked: list[str], parameter: ParameterSpec, values: tuple[object, ...]
) -> None:
    if not parameter.option_strings:
        return
    argv.append(parameter.option_strings[0])
    masked.append(parameter.option_strings[0])
    _append_values(argv, masked, values, parameter.sensitive)


def _append_values(
    argv: list[str], masked: list[str], values: tuple[object, ...], sensitive: bool
) -> None:
    for value in values:
        argv.append(str(value))
        masked.append(_MASK if sensitive else str(value))


def _values(
    value: object, parameter: ParameterSpec, *, split_multiple: bool = False
) -> tuple[object, ...]:
    if split_multiple:
        return tuple(value) if isinstance(value, (tuple, list)) else (value,)
    if parameter.nargs != 1:
        return tuple(value) if isinstance(value, (tuple, list)) else (value,)
    return (value,)


def _format_preview(argv: tuple[str, ...], platform_name: str | None) -> str:
    target_platform = platform_name or platform.system()
    if target_platform.lower() == "windows":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)
