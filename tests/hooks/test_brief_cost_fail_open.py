"""brief_cost.py обязан оставаться fail-open, даже если общий _transcript недоступен.

Отдельный файл (не test_brief_cost.py — его трогать нельзя): воспроизводит сценарий
ревьюера — копия хука без _transcript.py рядом падала с ImportError на импорте
модуля, то есть ДО входа в run()/main(), поэтому оборачивать тело run() в try/except
было недостаточно.
"""
import subprocess
import sys
from pathlib import Path

HOOK_PATH = Path(__file__).resolve().parents[2] / "plugin" / "hooks" / "brief_cost.py"


def test_brief_cost_is_fail_open_when_transcript_module_missing(tmp_path):
    """Копия хука в каталоге без _transcript.py: exit 0, ничего в stderr."""
    isolated_hook = tmp_path / "brief_cost.py"
    isolated_hook.write_text(HOOK_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(isolated_hook)],
        input="{}",
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stderr == ""
