"""Guard: словарь классов причин в коде и в тексте скилла не расходится (PRI-272).

Расхождение здесь молчаливо: незнакомый класс проходит мимо ветки шага 0a,
и пользователь снова не узнаёт причину — ровно тот дефект, который чинится.
"""
from reviewer import storage_health as sh

from .test_assembled_prompts import assemble


def _constants(prefix: str) -> set[str]:
    """Все значения `<prefix>*` модуля, интроспекцией — не перечислением руками.

    Перечисление руками не заметило бы появления новой константы, а guard
    существует ровно ради этого случая: захардкоженный кортеж воспроизвёл бы
    в самом тесте тот молчаливый дефект, который PRI-272 убирает из скилла
    (находка ревью задачи 6; та же форма, что `_detail_constants` в
    `tests/test_storage_health.py`).
    """
    return {
        value for name, value in vars(sh).items()
        if name.startswith(prefix) and isinstance(value, str)
    }


def _skill() -> str:
    return assemble("solve-task/SKILL.md")


def test_every_cause_constant_is_named_in_the_skill():
    causes = _constants("CAUSE_")
    assert causes, "CAUSE_* константы не найдены — тест ничего не проверяет"
    missing = {c for c in causes if c not in _skill()}
    assert not missing, f"классы не названы в скилле: {missing}"


def test_every_detail_constant_is_named_in_the_skill():
    details = _constants("DETAIL_")
    assert details, "DETAIL_* константы не найдены — тест ничего не проверяет"
    missing = {d for d in details if d not in _skill()}
    assert not missing, f"уточнения не названы в скилле: {missing}"


def test_skill_does_not_gate_on_a_single_cause_equality():
    """Шаг 0a обязан ветвиться по классу вообще, а не по равенству одному."""
    text = _skill()
    assert "`cause` is `storage_unavailable`" not in text
