from __future__ import annotations

import click

from reviewer.entrypoints.cli import cli
from reviewer.launcher.catalog import build_catalog
from reviewer.launcher.metadata import COMMAND_PRESENTATION, PARAMETER_PRESENTATION
from reviewer.launcher.models import Effect, ParameterPresentation


VISIBLE_COMMANDS = {
    "check",
    "config migrate",
    "config show",
    "gc",
    "index",
    "init",
    "install",
    "install-skills",
    "migrate-branches",
    "search",
    "serve",
    "status",
    "update",
}


def test_catalog_contains_every_visible_click_command():
    """Каталог должен показывать все доступные команды Click."""
    catalog = build_catalog(cli)

    assert {" ".join(item.path) for item in catalog} == VISIBLE_COMMANDS


def test_status_schema_comes_from_click():
    """Схема параметров сохраняет свойства объявлений Click."""
    status = next(item for item in build_catalog(cli) if item.path == ("status",))
    by_name = {param.name: param for param in status.params}

    assert by_name["path"].default == "."
    assert by_name["repo_tag"].option_strings == ("--repo",)
    assert by_name["as_json"].is_flag is True


def test_init_schema_exposes_scope_choices_and_repo_option():
    init = next(item for item in build_catalog(cli) if item.path == ("init",))
    by_name = {parameter.name: parameter for parameter in init.params}

    assert by_name["scope"].choices == ("all", "global", "repo")
    assert by_name["scope"].default == "all"
    assert by_name["repo_opt"].option_strings == ("--repo",)


def test_init_metadata_mentions_both_targets_preview_and_repo_scenario():
    init = next(item for item in build_catalog(cli) if item.path == ("init",))

    assert "global .env" in init.details
    assert "per-repo branch config" in init.details
    assert "preview" in init.details
    assert "Добавление репозитория" in init.scenarios


def test_catalog_uses_public_click_labels_and_help_for_options():
    """Форма получает публичные имена и справку, а не Python destination."""
    status = next(item for item in build_catalog(cli) if item.path == ("status",))
    by_name = {param.name: param for param in status.params}

    assert by_name["repo_tag"].label == "--repo"
    assert by_name["repo_tag"].description == (
        "owner/name тег индекса; по умолчанию из git remote origin"
    )
    assert by_name["branch_opt"].label == "--branch"
    assert by_name["branch_opt"].description == (
        "одна ветка; по умолчанию все отслеживаемые ветки репозитория (см. reviewer config show)"
    )


def test_catalog_preserves_explicit_click_metavar_for_repeatable_option():
    check = next(item for item in build_catalog(cli) if item.path == ("check",))
    board_project = next(param for param in check.params if param.name == "board_project_values")

    assert board_project.metavar == "TYPE=PROJECT"
    assert board_project.multiple is True


def test_catalog_preserves_click_choices_and_presentation_description(monkeypatch):
    """Choices остаются Click-authority, а presentation может уточнить подпись."""

    @click.group()
    def root() -> None:
        pass

    @root.command()
    @click.option(
        "--mode",
        "internal_mode",
        type=click.Choice(("fast", "safe")),
        help="Режим из Click.",
    )
    def choose(internal_mode: str | None) -> None:
        pass

    monkeypatch.setitem(
        PARAMETER_PRESENTATION,
        ("choose", "internal_mode"),
        ParameterPresentation(description="Публичное описание режима."),
    )

    parameter = build_catalog(root)[0].params[0]

    assert parameter.label == "--mode"
    assert parameter.description == "Публичное описание режима."
    assert parameter.choices == ("fast", "safe")


def test_catalog_skips_hidden_commands_and_does_not_call_default():
    """Скрытые команды не показываются, а callable default остаётся ленивым."""
    called = False

    def deferred_default() -> str:
        nonlocal called
        called = True
        return "значение по умолчанию"

    @click.group()
    def root() -> None:
        pass

    @root.command()
    @click.option("--value", default=deferred_default)
    def visible(value: str) -> None:
        pass

    @root.command(hidden=True, deprecated=True)
    def hidden() -> None:
        pass

    catalog = build_catalog(root)

    assert [item.path for item in catalog] == [("visible",)]
    assert catalog[0].params[0].default is deferred_default
    assert called is False


def test_current_commands_have_rich_metadata_without_orphans():
    """Метаданные должны покрывать ровно актуальные команды каталога."""
    paths = {item.path for item in build_catalog(cli)}

    assert set(COMMAND_PRESENTATION) == paths


def test_update_metadata_discloses_conditional_persistent_write():
    update = next(item for item in build_catalog(cli) if item.path == ("update",))

    assert update.effects == (Effect.READ, Effect.NETWORK, Effect.WRITE)
    assert "постоянную uv tool-установку" in update.details
    assert "только после отдельного подтверждения" in update.details
