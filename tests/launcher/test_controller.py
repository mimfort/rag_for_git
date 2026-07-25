from __future__ import annotations

import click
import pytest

from reviewer.entrypoints.cli import cli
from reviewer.launcher.catalog import build_catalog
from reviewer.launcher.controller import LauncherController, Screen
from reviewer.launcher.models import (
    CommandSpec,
    Effect,
    LauncherResult,
    ParameterSpec,
    ParamSection,
)


def _spec(name: str) -> CommandSpec:
    return next(item for item in build_catalog(cli) if item.path == (name,))


def _search_spec(name: str) -> CommandSpec:
    return CommandSpec(
        path=(name,),
        command=click.Command(name),
        summary="",
        details="",
        effects=(),
        scenarios=(),
        keywords=(),
        params=(),
    )


def _secret_spec() -> CommandSpec:
    source = click.Option(["--token"], required=True)
    parameter = ParameterSpec(
        source=source,
        name="token",
        kind="option",
        option_strings=("--token",),
        secondary_strings=(),
        required=True,
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
        details="Тестовая команда с секретом.",
        effects=(Effect.WRITE,),
        scenarios=(),
        keywords=("release",),
        params=(parameter,),
    )


def _sensitive_validation_spec(parameter_type: click.ParamType) -> CommandSpec:
    source = click.Option(["--token"], type=parameter_type)
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
        path=("validate",),
        command=click.Command("validate", params=[source]),
        summary="Проверить секрет",
        details="Тестовая команда с чувствительным параметром.",
        effects=(),
        scenarios=(),
        keywords=(),
        params=(parameter,),
    )


def test_command_requires_details_then_preview_then_confirm():
    controller = LauncherController((_spec("status"),))

    controller.open_selected()
    assert controller.screen is Screen.DETAILS

    controller.set_value("repo_tag", "a/x")
    controller.open_preview()
    assert controller.screen is Screen.PREVIEW
    assert controller.result is None

    controller.confirm()
    assert controller.result is not None
    assert controller.result.argv == ("status", "--repo", "a/x")


def test_search_filters_metadata_and_prioritizes_command_name():
    controller = LauncherController((_spec("gc"), _spec("status"), _spec("check")))

    controller.set_query("overlay")
    assert [command.path for command in controller.filtered_commands] == [
        ("gc",),
        ("status",),
    ]

    controller.set_query("status")
    assert controller.selected.path == ("status",)


def test_search_matches_subsequences_in_command_names_and_keywords():
    controller = LauncherController((_spec("status"), _spec("index"), _spec("serve")))

    controller.set_query("idx")
    assert [command.path for command in controller.filtered_commands] == [("index",)]

    controller.set_query("dbrd")
    assert [command.path for command in controller.filtered_commands] == [("serve",)]


def test_search_orders_exact_prefix_substring_then_subsequence():
    controller = LauncherController(
        (
            _search_spec("in-d-ex"),
            _search_spec("reindex"),
            _search_spec("indexing"),
            _search_spec("index"),
        )
    )

    controller.set_query("index")

    assert [command.path for command in controller.filtered_commands] == [
        ("index",),
        ("indexing",),
        ("reindex",),
        ("in-d-ex",),
    ]


def test_move_wraps_inside_filtered_commands():
    controller = LauncherController((_spec("status"), _spec("check")))

    controller.move(-1)
    assert controller.selected.path == ("check",)

    controller.move(1)
    assert controller.selected.path == ("status",)


def test_required_field_error_keeps_details_open():
    controller = LauncherController((_spec("index"),))
    controller.open_selected()

    controller.open_preview()

    assert controller.screen is Screen.DETAILS
    assert controller.errors == {"repo": "Обязательное поле"}
    assert controller.result is None


def test_builtin_click_type_is_validated_without_final_click_parse():
    controller = LauncherController((_spec("serve"),))
    controller.open_selected()
    controller.set_value("port", "не-число")

    controller.open_preview()

    assert controller.screen is Screen.DETAILS
    assert "port" in controller.errors


@pytest.mark.parametrize(
    ("parameter_type", "secret"),
    [
        (click.Choice(("alpha", "beta")), "secret-choice"),
        (click.INT, "secret-integer"),
    ],
)
def test_sensitive_builtin_validation_error_never_contains_raw_value(parameter_type, secret):
    """Ошибка чувствительного встроенного типа хранится только в безопасном виде."""
    controller = LauncherController((_sensitive_validation_spec(parameter_type),))
    controller.open_selected()
    controller.set_value("token", secret)

    controller.open_preview()

    assert controller.errors == {"token": "Некорректное значение"}
    assert secret not in repr(controller.errors)
    assert secret not in repr(controller)


def test_custom_click_type_is_deferred_to_final_click_parse():
    class _CustomType(click.ParamType):
        name = "custom"

        def convert(self, value, param, ctx):
            self.fail("финальная Click-валидация", param, ctx)

    source = click.Option(["--value"], type=_CustomType())
    parameter = ParameterSpec(
        source=source,
        name="value",
        kind="option",
        option_strings=("--value",),
        secondary_strings=(),
        required=False,
        nargs=1,
        multiple=False,
        count=False,
        is_flag=False,
        default=None,
        choices=(),
        section=ParamSection.BASIC,
        sensitive=False,
    )
    spec = CommandSpec(
        path=("custom",),
        command=click.Command("custom", params=[source]),
        summary="Проверить пользовательский тип",
        details="Пользовательский тип валидирует существующий Click path.",
        effects=(),
        scenarios=(),
        keywords=(),
        params=(parameter,),
    )
    controller = LauncherController((spec,))
    controller.open_selected()
    controller.set_value("value", "любой текст")

    controller.open_preview()

    assert controller.screen is Screen.PREVIEW
    assert controller.errors == {}


def test_boolean_flag_can_be_toggled_before_preview():
    controller = LauncherController((_spec("status"),))
    controller.open_selected()

    controller.set_value("as_json", True)
    controller.open_preview()

    assert controller.prepared is not None
    assert controller.prepared.argv == ("status", "--json")


def test_advanced_fields_are_hidden_until_toggled():
    controller = LauncherController((_spec("status"),))
    controller.open_selected()

    assert [parameter.name for parameter in controller.visible_parameters] == [
        "path",
        "repo_tag",
        "as_json",
    ]

    controller.toggle_advanced()

    assert [parameter.name for parameter in controller.visible_parameters] == [
        "path",
        "repo_tag",
        "branch_opt",
        "as_json",
    ]


def test_sensitive_value_is_masked_in_preview_but_preserved_in_result():
    controller = LauncherController((_secret_spec(),))
    controller.open_selected()
    controller.set_value("token", "secret-123")

    controller.open_preview()

    assert controller.prepared is not None
    assert "secret-123" not in controller.prepared.preview
    assert "••••••" in controller.prepared.preview

    controller.confirm()
    assert controller.result == LauncherResult(("deploy", "--token", "secret-123"), 0)


def test_escape_never_executes_command():
    controller = LauncherController((_spec("gc"),))
    controller.open_selected()
    controller.back()
    controller.cancel()

    assert controller.result == LauncherResult(None, 0)


def test_ctrl_c_returns_shell_interrupt_code():
    controller = LauncherController((_spec("gc"),))

    controller.cancel(exit_code=130)

    assert controller.result == LauncherResult(None, 130)


def test_unknown_transition_does_not_change_state_or_create_result():
    controller = LauncherController((_spec("status"),))
    controller.confirm()

    assert controller.screen is Screen.PALETTE
    assert controller.result is None
