"""Guard: обе версии README документируют команду харнесса метрик."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
COMMAND = "python -m eval.solve_task_metrics"


def test_spike_script_removed():
    assert not (ROOT / "eval" / "pri246_solve_task_cost.py").exists()


def test_spike_report_kept_as_historical_artifact():
    assert (ROOT / "eval" / "pri246_report.md").exists()


def test_both_readmes_document_the_command():
    for name in ("README.md", "README.ru.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert COMMAND in text, f"{name} не документирует команду харнесса"
        for subcommand in ("snapshot", "compare", "forecast"):
            assert f"{COMMAND} {subcommand}" in text, f"{name}: нет {subcommand}"
