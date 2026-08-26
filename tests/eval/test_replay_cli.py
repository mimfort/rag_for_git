"""Подкоманда replay: парсинг аргументов и отказы (PRI-254)."""
from __future__ import annotations

import pytest

from eval.solve_task_metrics import __main__ as cli
from eval.solve_task_metrics import report_merge


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


def test_replay_refuses_report_without_marker_before_any_run(tmp_path, monkeypatch, capsys):
    """Fail-closed стоит ДО прогона: инфраструктура не трогается вовсе.

    Тест проходит без Postgres/Neo4j именно потому, что отказ случается раньше
    открытия живых зависимостей: любой выход в сеть здесь уронил бы тест.
    """
    report = tmp_path / "replay_report.md"
    report.write_text("# Отчёт\n\n## Приёмка PRI-262\n\nручное\n", encoding="utf-8")
    monkeypatch.setattr(cli, "REPLAY_REPORT_PATH", report)
    args = cli.build_parser().parse_args(["replay"])
    assert cli.cmd_replay(args) == 1
    out = capsys.readouterr().out
    assert str(report) in out
    assert report_merge.MARKER in out
    # ручной текст на месте: отказ ничего не переписал
    assert "## Приёмка PRI-262" in report.read_text(encoding="utf-8")


def test_snapshot_refuses_report_without_marker_before_any_run(tmp_path, monkeypatch, capsys):
    report = tmp_path / "solve_task_metrics_report.md"
    report.write_text("# Метрики\n\n## Ручное\n\nтекст\n", encoding="utf-8")
    monkeypatch.setattr(cli, "REPORT_PATH", report)
    args = cli.build_parser().parse_args(["snapshot"])
    assert cli.cmd_snapshot(args) == 1
    assert report_merge.MARKER in capsys.readouterr().out


def test_repository_reports_carry_the_marker():
    """Оба отчёта репозитория пригодны к слиянию — иначе первый же прогон откажет."""
    for path in (cli.REPLAY_REPORT_PATH, cli.REPORT_PATH):
        assert report_merge.MARKER in path.read_text(encoding="utf-8"), path


def test_replay_cli_accepts_context_seeds_flag():
    """Режим сидов задаётся флагом: правка исходника между сторонами A/B
    сделала бы сравнение невалидным."""
    from eval.solve_task_metrics import __main__ as main_mod

    from eval.solve_task_metrics import replay

    parser = main_mod.build_parser()
    for mode in replay.SEED_MODES:
        args = parser.parse_args(["replay", "--context-seeds", mode])
        assert args.context_seeds == mode

    # Дефолт назван константой, а не литералом: он менялся по итогу приёмки
    # PRI-266, и тест не должен быть вторым местом, где записан выбор.
    default_args = parser.parse_args(["replay"])
    assert default_args.context_seeds == replay.SEED_MODE_LINES_SIGNATURE
