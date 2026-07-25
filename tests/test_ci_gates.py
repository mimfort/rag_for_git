"""Гейты CI покрывают ветки, через которые код доезжает до main.

Инвариант: ни один коммит не попадает в main непроверенным. Разработка идёт в dev,
поэтому прогон тестов обязан висеть и на PR в dev, и на прямых push в dev — иначе
поломка обнаруживается только после мержа dev → main, где она блокирует publish
(так и произошло с рассинхроном версии манифеста 0.3.6 vs pyproject 0.3.7).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _triggers(workflow: dict) -> dict:
    """Секция триггеров workflow.

    В YAML голый ключ ``on:`` — булев литерал, поэтому safe_load кладёт его как
    ``True``, а не как строку "on" (грабля YAML 1.1).
    """
    return workflow.get("on", workflow.get(True)) or {}


def _runs_full_test_suite(workflow: dict) -> bool:
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if "pytest -q" in str(step.get("run", "")):
                return True
    return False


def _branches(triggers: dict, event: str) -> set[str]:
    spec = triggers.get(event)
    if not isinstance(spec, dict):
        return set()
    return set(spec.get("branches") or [])


@pytest.fixture(scope="module")
def workflows() -> list[dict]:
    loaded = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        loaded.append(yaml.safe_load(path.read_text(encoding="utf-8")))
    return loaded


@pytest.mark.parametrize(
    ("event", "branch"),
    [
        ("pull_request", "dev"),   # feature → dev
        ("pull_request", "main"),  # dev → main
        ("push", "dev"),           # прямой коммит в dev (в т.ч. правка через веб-UI)
        ("push", "main"),          # финальный гейт перед публикацией
    ],
)
def test_full_test_suite_gates_every_path_into_main(workflows, event, branch):
    gating = [w for w in workflows if _runs_full_test_suite(w)]
    assert gating, "ни один workflow не запускает полный прогон pytest -q"
    covered = any(branch in _branches(_triggers(w), event) for w in gating)
    assert covered, f"{event} в ветку {branch} не покрыт прогоном тестов"


def test_manifest_sync_is_checked_before_merge(workflows):
    # Рассинхрон сгенерированных манифестов плагина с версией pyproject ловится
    # раньше и внятнее полного прогона: отдельным шагом с понятной подсказкой.
    checking = [
        w
        for w in workflows
        if any(
            "update_codex_plugin_manifest.py --check" in str(step.get("run", ""))
            for job in (w.get("jobs") or {}).values()
            for step in job.get("steps") or []
        )
    ]
    assert checking, "проверка синхронности манифестов не запускается в CI"
    covered = any("dev" in _branches(_triggers(w), "pull_request") for w in checking)
    assert covered, "проверка манифестов не гейтит PR в dev"
