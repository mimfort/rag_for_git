"""Подкоманда replay: парсинг аргументов и отказы (PRI-254)."""
from __future__ import annotations

import pytest

from eval.solve_task_metrics import __main__ as cli


def test_replay_parses_variant_and_overrides():
    args = cli.build_parser().parse_args(
        [
            "replay", "--variant", "limits", "--set", "search_codebase.ceiling=25",
            "--limit", "3", "--repo", "o/n", "--branch", "dev",
        ]
    )
    assert args.command == "replay"
    assert args.variant == "limits"
    assert args.set == ["search_codebase.ceiling=25"]
    assert args.limit == 3 and args.repo == "o/n" and args.branch == "dev"


def test_replay_defaults_to_baseline_variant():
    args = cli.build_parser().parse_args(["replay"])
    assert args.variant == "baseline"
    assert args.set == [] and args.limit is None and args.baseline is None


@pytest.mark.parametrize("command", ["snapshot", "stats", "compare", "forecast", "steps"])
def test_existing_subcommands_still_parse(command):
    """Критерий 4: существующие команды не тронуты."""
    assert cli.build_parser().parse_args([command]).command == command


def test_unknown_variant_is_reported_without_touching_infrastructure(capsys):
    args = cli.build_parser().parse_args(["replay", "--variant", "нет-такого"])
    assert cli.cmd_replay(args) == 1
    assert "нет-такого" in capsys.readouterr().out


def test_malformed_override_is_reported_without_touching_infrastructure(capsys):
    args = cli.build_parser().parse_args(
        ["replay", "--variant", "limits", "--set", "ceiling=25"]
    )
    assert cli.cmd_replay(args) == 1
    assert "ceiling=25" in capsys.readouterr().out


def test_limits_variant_without_overrides_is_rejected(capsys):
    """Вариант limits без --set — это baseline под чужим именем."""
    args = cli.build_parser().parse_args(["replay", "--variant", "limits"])
    assert cli.cmd_replay(args) == 1
    assert "--set" in capsys.readouterr().out
