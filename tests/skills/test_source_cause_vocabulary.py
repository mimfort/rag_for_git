"""Guard: словарь классов причин в коде и в тексте скилла не расходится (PRI-272).

Расхождение здесь молчаливо: незнакомый класс проходит мимо ветки шага 0a,
и пользователь снова не узнаёт причину — ровно тот дефект, который чинится.
"""
from reviewer import storage_health as sh

from .test_assembled_prompts import assemble


def _skill() -> str:
    return assemble("solve-task/SKILL.md")


def test_every_cause_constant_is_named_in_the_skill():
    text = _skill()
    for cause in (sh.CAUSE_STORAGE_UNAVAILABLE, sh.CAUSE_EMBEDDER_UNAVAILABLE,
                  sh.CAUSE_UNKNOWN):
        assert cause in text, f"класс {cause} не назван в скилле"


def test_every_detail_constant_is_named_in_the_skill():
    text = _skill()
    for detail in (sh.DETAIL_AUTH_FAILED, sh.DETAIL_MISSING_DATABASE,
                   sh.DETAIL_POOL_EXHAUSTED):
        assert detail in text, f"уточнение {detail} не названо в скилле"


def test_skill_does_not_gate_on_a_single_cause_equality():
    """Шаг 0a обязан ветвиться по классу вообще, а не по равенству одному."""
    text = _skill()
    assert "`cause` is `storage_unavailable`" not in text
