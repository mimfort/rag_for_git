from __future__ import annotations

import subprocess

import click
import pytest

from reviewer.entrypoints.cli import cli
from reviewer.launcher.catalog import build_catalog
from reviewer.launcher.command import prepare_command
from reviewer.launcher.models import (
    CommandSpec,
    Effect,
    ParameterSpec,
    ParamSection,
)


def _command(name: str) -> CommandSpec:
    return next(item for item in build_catalog(cli) if item.path == (name,))


def _secret_command() -> CommandSpec:
    source = click.Option(["--token"])
    parameter = ParameterSpec(
        source=source,
        name="token",
        kind="option",
        option_strings=("--token",),
        secondary_strings=(),
        required=False,
        nargs=1,
        multiple=False,
        count=False,
        is_flag=False,
        default=None,
        choices=(),
        section=ParamSection.BASIC,
        sensitive=True,
    )
    return CommandSpec(
        path=("deploy",),
        command=click.Command("deploy", params=[source]),
        summary="Развернуть",
        details="Тестовая команда",
        effects=(Effect.WRITE,),
        scenarios=(),
        keywords=(),
        params=(parameter,),
    )


def test_prepare_status_omits_unchanged_defaults_and_emits_changed_flags():
    """Неизменённые опции не попадают в argv, а флаги попадают."""
    status = _command("status")

    prepared = prepare_command(
        status,
        values={"path": ".", "repo_tag": "a/x", "as_json": True},
        changed={"repo_tag", "as_json"},
        platform_name="Linux",
    )

    assert prepared.argv == ("status", "--repo", "a/x", "--json")
    assert prepared.preview == "reviewer status --repo a/x --json"


def test_sensitive_value_is_real_in_argv_but_masked_everywhere_else():
    """Секрет нужен процессу, но не должен попадать в preview или repr."""
    spec = _secret_command()

    prepared = prepare_command(
        spec,
        values={"token": "secret-123"},
        changed={"token"},
        platform_name="Linux",
    )

    assert prepared.argv == ("deploy", "--token", "secret-123")
    assert "secret-123" not in prepared.preview
    assert "secret-123" not in repr(prepared)
    assert "••••••" in prepared.preview


@pytest.mark.parametrize(
    ("parameter", "value", "changed", "expected"),
    [
        (click.Argument(["repo"]), "/tmp/repo", {"repo"}, ("run", "/tmp/repo")),
        (click.Argument(["path"], required=False), None, set(), ("run",)),
        (click.Option(["--cache/--no-cache"]), False, {"cache"}, ("run", "--no-cache")),
        (click.Option(["--label"], multiple=True), ("one", "two"), {"label"}, ("run", "--label", "one", "--label", "two")),
        (click.Option(["-v"], count=True), 2, {"v"}, ("run", "-v", "-v")),
        (click.Option(["--pair"], nargs=2), ("left", "right"), {"pair"}, ("run", "--pair", "left", "right")),
    ],
)
def test_prepare_command_serializes_click_parameter_shapes(parameter, value, changed, expected):
    """Построитель argv сохраняет семантику основных форм параметров Click."""
    command = click.Command("run", params=[parameter])
    spec = CommandSpec(
        path=("run",),
        command=command,
        summary="Запустить",
        details="Тестовая команда",
        effects=(),
        scenarios=(),
        keywords=(),
        params=(
            ParameterSpec(
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
                default=parameter.default,
                choices=(),
                section=ParamSection.BASIC,
                sensitive=False,
            ),
        ),
    )

    prepared = prepare_command(spec, {parameter.name: value}, changed, platform_name="Linux")

    assert prepared.argv == expected


def test_windows_preview_uses_windows_quoting_after_masking_secret():
    """Windows preview экранируется list2cmdline и маскирует секрет до форматирования."""
    prepared = prepare_command(
        _secret_command(),
        values={"token": "secret with spaces"},
        changed={"token"},
        platform_name="Windows",
    )

    assert prepared.preview == subprocess.list2cmdline(("reviewer", "deploy", "--token", "••••••"))
