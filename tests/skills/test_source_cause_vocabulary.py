"""Guard: словарь классов причин в коде и в тексте скилла не расходится (PRI-272).

Расхождение здесь молчаливо: незнакомый класс проходит мимо ветки шага 0a,
и пользователь снова не узнаёт причину — ровно тот дефект, который чинится.

Тексты проверяются ПО ОТДЕЛЬНОСТИ (находка I3 финального ревью). Ветвится по
классу `references/preflight.md`, а `SKILL.md` перечисляет тот же словарь в
описании payload — и, будучи включённым в сборку, прикрывал соседа: удаление
всех упоминаний `pool_exhausted` из самой ветки оставляло guard зелёным.
Проверка каждого файла отдельно закрывает именно этот случай.
"""
from pathlib import Path

from reviewer import storage_health as sh

from .test_assembled_prompts import assemble

_PREFLIGHT = "solve-task/references/preflight.md"
_SKILLS_DIR = Path(__file__).resolve().parents[2] / "plugin" / "skills"


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
    """SKILL.md БЕЗ развёрнутых include: иначе сосед прикрывает соседа."""
    return (_SKILLS_DIR / "solve-task/SKILL.md").read_text(encoding="utf-8")


def _preflight() -> str:
    """Текст ветки шага 0a — тот файл, который классами реально ветвится."""
    return (_SKILLS_DIR / _PREFLIGHT).read_text(encoding="utf-8")


def test_every_cause_constant_is_named_in_the_preflight_branch():
    causes = _constants("CAUSE_")
    assert causes, "CAUSE_* константы не найдены — тест ничего не проверяет"
    missing = {c for c in causes if c not in _preflight()}
    assert not missing, f"классы не названы в ветке шага 0a: {missing}"


def test_every_detail_constant_is_named_in_the_preflight_branch():
    details = _constants("DETAIL_")
    assert details, "DETAIL_* константы не найдены — тест ничего не проверяет"
    missing = {d for d in details if d not in _preflight()}
    assert not missing, f"уточнения не названы в ветке шага 0a: {missing}"


def test_every_cause_constant_is_named_in_the_skill_body():
    """SKILL.md описывает форму payload и обязан перечислять тот же словарь."""
    causes = _constants("CAUSE_")
    missing = {c for c in causes if c not in _skill()}
    assert not missing, f"классы не названы в SKILL.md: {missing}"


def test_every_detail_constant_is_named_in_the_skill_body():
    details = _constants("DETAIL_")
    missing = {d for d in details if d not in _skill()}
    assert not missing, f"уточнения не названы в SKILL.md: {missing}"


def test_assembled_prompt_still_carries_the_whole_vocabulary():
    """Сборка промпта не теряет словарь: маркер include развёрнут, ничего не съедено."""
    assembled = assemble("solve-task/SKILL.md")
    missing = {v for v in _constants("CAUSE_") | _constants("DETAIL_")
               if v not in assembled}
    assert not missing, f"словарь не доехал до собранного промпта: {missing}"


def test_skill_does_not_gate_on_a_single_cause_equality():
    """Шаг 0a обязан ветвиться по классу вообще, а не по равенству одному."""
    assert "`cause` is `storage_unavailable`" not in assemble("solve-task/SKILL.md")
