"""Построение каталога команд из деклараций Click."""

from __future__ import annotations

import click

from reviewer.launcher.metadata import COMMAND_PRESENTATION, PARAMETER_PRESENTATION
from reviewer.launcher.models import (
    CommandPresentation,
    CommandSpec,
    ParameterPresentation,
    ParameterSpec,
)


def build_catalog(root: click.Group) -> tuple[CommandSpec, ...]:
    """Вернуть видимые leaf-команды, не вычисляя callable defaults."""
    return tuple(_walk(root, (), click.Context(root, info_name=root.name or "reviewer")))


def _walk(command: click.Command, path: tuple[str, ...], ctx: click.Context):
    if not isinstance(command, click.Group):
        yield _build_spec(command, path, ctx)
        return

    for name in command.list_commands(ctx):
        child = command.get_command(ctx, name)
        if child is None or child.hidden:
            continue
        child_path = (*path, name)
        child_ctx = click.Context(child, parent=ctx, info_name=child.name or "reviewer")
        yield from _walk(child, child_path, child_ctx)


def _build_spec(command: click.Command, path: tuple[str, ...], ctx: click.Context) -> CommandSpec:
    presentation = COMMAND_PRESENTATION.get(path, _fallback_presentation(command))
    return CommandSpec(
        path=path,
        command=command,
        summary=presentation.summary,
        details=presentation.details,
        effects=presentation.effects,
        scenarios=presentation.scenarios,
        keywords=presentation.keywords,
        params=tuple(_parameter_spec(path, parameter, ctx) for parameter in command.params),
        special_action=presentation.special_action,
    )


def _fallback_presentation(command: click.Command) -> CommandPresentation:
    text = command.short_help or command.help or ""
    return CommandPresentation(text, text, (), (), ())


def _parameter_spec(
    path: tuple[str, ...], parameter: click.Parameter, ctx: click.Context
) -> ParameterSpec:
    presentation = PARAMETER_PRESENTATION.get((*path, parameter.name), ParameterPresentation())
    choices = parameter.type.choices if isinstance(parameter.type, click.Choice) else ()
    return ParameterSpec(
        source=parameter,
        name=parameter.name,
        kind="argument" if isinstance(parameter, click.Argument) else "option",
        option_strings=tuple(parameter.opts),
        secondary_strings=tuple(parameter.secondary_opts),
        required=parameter.required,
        nargs=parameter.nargs,
        multiple=parameter.multiple,
        count=getattr(parameter, "count", False),
        is_flag=getattr(parameter, "is_flag", False),
        default=parameter.get_default(ctx, call=False),
        choices=tuple(str(choice) for choice in choices),
        section=presentation.section,
        sensitive=presentation.sensitive,
        label=_public_label(parameter),
        description=(
            presentation.description
            if presentation.description is not None
            else getattr(parameter, "help", None)
        ),
        metavar=getattr(parameter, "metavar", None),
    )


def _public_label(parameter: click.Parameter) -> str:
    if isinstance(parameter, click.Option):
        return " / ".join((*parameter.opts, *parameter.secondary_opts))
    return parameter.human_readable_name
