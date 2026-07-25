"""Модели результата интерактивного launcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import click


@dataclass(frozen=True)
class LauncherResult:
    """Результат выбора команды в интерактивном launcher."""

    argv: tuple[str, ...] | None = field(repr=False)
    exit_code: int


class Effect(StrEnum):
    READ = "read"
    WRITE = "write"
    NETWORK = "network"
    DESTRUCTIVE = "destructive"


class ParamSection(StrEnum):
    BASIC = "basic"
    ADVANCED = "advanced"


@dataclass(frozen=True)
class CommandPresentation:
    summary: str
    details: str
    effects: tuple[Effect, ...]
    scenarios: tuple[str, ...]
    keywords: tuple[str, ...]
    special_action: str | None = None


@dataclass(frozen=True)
class ParameterPresentation:
    section: ParamSection = ParamSection.BASIC
    sensitive: bool = False
    description: str | None = None


@dataclass(frozen=True)
class ParameterSpec:
    source: click.Parameter = field(repr=False, compare=False)
    name: str
    kind: str
    option_strings: tuple[str, ...]
    secondary_strings: tuple[str, ...]
    required: bool
    nargs: int
    multiple: bool
    count: bool
    is_flag: bool
    default: object = field(repr=False)
    choices: tuple[str, ...]
    section: ParamSection
    sensitive: bool
    label: str = ""
    description: str | None = None


@dataclass(frozen=True)
class CommandSpec:
    path: tuple[str, ...]
    command: click.Command = field(repr=False, compare=False)
    summary: str
    details: str
    effects: tuple[Effect, ...]
    scenarios: tuple[str, ...]
    keywords: tuple[str, ...]
    params: tuple[ParameterSpec, ...]
    special_action: str | None = None
