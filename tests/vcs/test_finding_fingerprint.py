"""Тесты для normalize_message и Finding.fingerprint() из reviewer/vcs/base.py.

Проверяет, что нормализация устраняет незначимые различия (регистр, пробелы,
пунктуация) и fingerprint стабилен к дрейфу формулировки LLM.
"""
from reviewer.vcs.base import Finding, normalize_message


# ---------------------------------------------------------------------------
# Вспомогательная фабрика
# ---------------------------------------------------------------------------

def make_finding(
    file: str = "a.py",
    line: int | None = 10,
    side: str = "RIGHT",
    category: str = "correctness",
    message: str = "проблема",
) -> Finding:
    return Finding(
        category=category,
        severity="medium",
        file=file,
        line=line,
        side=side,
        message=message,
        suggestion=None,
        confidence=0.8,
    )


# ---------------------------------------------------------------------------
# normalize_message: юнит-тесты
# ---------------------------------------------------------------------------

def test_normalize_lowercase_ascii():
    """Регистр ASCII приводится к нижнему."""
    assert normalize_message("Bug X") == normalize_message("bug x")


def test_normalize_lowercase_cyrillic():
    """Регистр кириллицы приводится к нижнему (casefold)."""
    assert normalize_message("Баг X") == normalize_message("баг x")


def test_normalize_collapses_spaces():
    """Множественные пробелы схлопываются в один."""
    assert normalize_message("a   b") == "a b"


def test_normalize_punctuation_to_space():
    """Пунктуация заменяется пробелом."""
    # «eval().» и «eval» должны дать одинаковый нормализованный вид
    assert normalize_message("eval().") == normalize_message("eval")


def test_normalize_trims_whitespace():
    """Пробелы по краям удаляются."""
    assert normalize_message("  x  ") == "x"


def test_normalize_preserves_cyrillic_letters():
    """Кириллические буквы (\\w) сохраняются после нормализации."""
    result = normalize_message("Использование eval() небезопасно.")
    assert "использование" in result
    assert "eval" in result
    assert "небезопасно" in result


def test_normalize_trailing_dot_equivalent():
    """Сообщение с финальной точкой и без — одинаковая нормализованная форма."""
    assert normalize_message("баг найден.") == normalize_message("баг найден")


def test_normalize_extra_spaces_around_punctuation():
    """Пробелы вокруг пунктуации не создают артефактов."""
    result = normalize_message("  Баг ,  пробел.  ")
    assert result == normalize_message("Баг пробел")


def test_normalize_different_meaningful_messages_stay_different():
    """Содержательно разные сообщения остаются разными после нормализации."""
    a = normalize_message("утечка памяти в цикле")
    b = normalize_message("SQL-инъекция в запросе")
    assert a != b


# ---------------------------------------------------------------------------
# Finding.fingerprint: стабильность к дрейфу формулировки
# ---------------------------------------------------------------------------

def test_fingerprint_stable_to_case():
    """Регистровый дрейф не создаёт новый fingerprint."""
    f1 = make_finding(message="Баг X")
    f2 = make_finding(message="баг x")
    assert f1.fingerprint() == f2.fingerprint()


def test_fingerprint_stable_to_trailing_punctuation():
    """Финальная точка не создаёт новый fingerprint."""
    f1 = make_finding(message="баг найден")
    f2 = make_finding(message="баг найден.")
    assert f1.fingerprint() == f2.fingerprint()


def test_fingerprint_stable_to_extra_spaces():
    """Лишние пробелы не создают новый fingerprint."""
    f1 = make_finding(message="баг  в  коде")
    f2 = make_finding(message="баг в коде")
    assert f1.fingerprint() == f2.fingerprint()


def test_fingerprint_differs_for_different_message():
    """Содержательно разные message дают разный fingerprint."""
    f1 = make_finding(message="утечка памяти")
    f2 = make_finding(message="SQL-инъекция")
    assert f1.fingerprint() != f2.fingerprint()


def test_fingerprint_differs_for_different_file():
    """Разный file → разный fingerprint (нормализация не схлопнула координаты)."""
    f1 = make_finding(file="a.py", message="баг")
    f2 = make_finding(file="b.py", message="баг")
    assert f1.fingerprint() != f2.fingerprint()


def test_fingerprint_differs_for_different_line():
    """Разная line → разный fingerprint."""
    f1 = make_finding(line=10, message="баг")
    f2 = make_finding(line=20, message="баг")
    assert f1.fingerprint() != f2.fingerprint()


def test_fingerprint_differs_for_different_category():
    """Разная category → разный fingerprint."""
    f1 = make_finding(category="correctness", message="баг")
    f2 = make_finding(category="security", message="баг")
    assert f1.fingerprint() != f2.fingerprint()


def test_fingerprint_differs_for_different_side():
    """Разный side → разный fingerprint."""
    f1 = make_finding(side="RIGHT", message="баг")
    f2 = make_finding(side="LEFT", message="баг")
    assert f1.fingerprint() != f2.fingerprint()


# ---------------------------------------------------------------------------
# Идемпотентность: повторный прогон не плодит дубли
# ---------------------------------------------------------------------------

def test_idempotency_case_drift():
    """Первый прогон создаёт fingerprint; второй с дрейфом регистра возвращает тот же.

    Моделируем ситуацию: LLM во втором прогоне вернула тот же баг, но с иным регистром.
    fingerprint второй находки должен совпадать с уже опубликованным из первого прогона.
    """
    f1 = make_finding(message="Баг в функции eval")   # первый прогон
    f2 = make_finding(message="баг в функции eval")   # второй прогон — дрейф регистра

    existing_fps = {f1.fingerprint()}
    assert f2.fingerprint() in existing_fps


def test_idempotency_punctuation_drift():
    """Финальная точка в сообщении второго прогона не создаёт новый fingerprint."""
    f1 = make_finding(message="небезопасная функция")
    f2 = make_finding(message="небезопасная функция.")

    existing_fps = {f1.fingerprint()}
    assert f2.fingerprint() in existing_fps


def test_idempotency_space_drift():
    """Лишний пробел в сообщении второго прогона не создаёт новый fingerprint."""
    f1 = make_finding(message="переменная не используется")
    f2 = make_finding(message="переменная  не используется")  # два пробела

    existing_fps = {f1.fingerprint()}
    assert f2.fingerprint() in existing_fps
