"""Тесты для reviewer/agent/dedup.py."""
from reviewer.agent.dedup import dedup_findings
from reviewer.vcs.base import Finding


def make_finding(
    file: str = "a.py",
    line: int | None = 10,
    category: str = "correctness",
    message: str = "проблема",
    severity: str = "medium",
    confidence: float = 0.8,
    fix_start: int | None = None,
    fix_end: int | None = None,
    replacement: str | None = None,
    side: str = "RIGHT",
    suggestion: str | None = None,
) -> Finding:
    return Finding(
        category=category,
        severity=severity,
        file=file,
        line=line,
        side=side,
        message=message,
        suggestion=suggestion,
        confidence=confidence,
        fix_start=fix_start,
        fix_end=fix_end,
        replacement=replacement,
    )


# ---------------------------------------------------------------------------
# Граничные случаи
# ---------------------------------------------------------------------------

def test_empty_list():
    assert dedup_findings([]) == []


def test_single_finding():
    f = make_finding()
    result = dedup_findings([f])
    assert result == [f]


# ---------------------------------------------------------------------------
# Точные дубли
# ---------------------------------------------------------------------------

def test_exact_duplicate_keeps_first():
    f1 = make_finding(message="баг X", line=5)
    f2 = make_finding(message="баг X", line=5)
    result = dedup_findings([f1, f2])
    assert len(result) == 1
    assert result[0] is f1


def test_exact_duplicate_three_keeps_first():
    f1 = make_finding(message="баг Y", line=7)
    f2 = make_finding(message="баг Y", line=7)
    f3 = make_finding(message="баг Y", line=7)
    result = dedup_findings([f1, f2, f3])
    assert len(result) == 1
    assert result[0] is f1


def test_no_exact_duplicate_different_message():
    f1 = make_finding(message="баг A", line=5)
    f2 = make_finding(message="баг B", line=5)
    result = dedup_findings([f1, f2])
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Fuzzy-дубли: одна строка
# ---------------------------------------------------------------------------

def test_fuzzy_same_line_similar_messages_merged():
    f1 = make_finding(message="использование небезопасной функции eval", line=10)
    f2 = make_finding(message="использование небезопасной функции eval здесь", line=10)
    result = dedup_findings([f1, f2], similarity=0.85)
    assert len(result) == 1


def test_fuzzy_same_line_very_different_messages_not_merged():
    f1 = make_finding(message="утечка памяти", line=10)
    f2 = make_finding(message="SQL-инъекция в запросе", line=10)
    result = dedup_findings([f1, f2], similarity=0.9)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Fuzzy-дубли: соседние строки
# ---------------------------------------------------------------------------

def test_fuzzy_adjacent_lines_within_2_merged():
    msg = "переменная не используется"
    f1 = make_finding(message=msg, line=10)
    f2 = make_finding(message=msg, line=12)  # +2 — ещё должны схлопнуться
    result = dedup_findings([f1, f2], similarity=0.9)
    assert len(result) == 1


def test_fuzzy_adjacent_lines_diff_3_not_merged():
    msg = "переменная не используется"
    f1 = make_finding(message=msg, line=10)
    f2 = make_finding(message=msg, line=13)  # +3 — не схлопываем
    result = dedup_findings([f1, f2], similarity=0.9)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Выживает max severity
# ---------------------------------------------------------------------------

def test_max_severity_survives():
    f_low = make_finding(message="одинаковое", line=5, severity="low", confidence=0.9)
    f_high = make_finding(message="одинаковое", line=5, severity="high", confidence=0.5)
    result = dedup_findings([f_low, f_high], similarity=0.9)
    assert len(result) == 1
    assert result[0].severity == "high"


def test_max_severity_all_levels():
    messages = ["одинаковая проблема"] * 4
    severities = ["low", "medium", "critical", "high"]
    findings = [make_finding(message=m, line=1, severity=s) for m, s in zip(messages, severities)]
    result = dedup_findings(findings, similarity=0.9)
    assert len(result) == 1
    assert result[0].severity == "critical"


def test_equal_severity_max_confidence_survives():
    f1 = make_finding(message="одна проблема", line=5, severity="medium", confidence=0.6)
    f2 = make_finding(message="одна проблема", line=5, severity="medium", confidence=0.9)
    result = dedup_findings([f1, f2], similarity=0.9)
    assert len(result) == 1
    assert result[0].confidence == 0.9


# ---------------------------------------------------------------------------
# Перенос fix-полей
# ---------------------------------------------------------------------------

def test_fix_transferred_from_duplicate_to_winner():
    """Победитель без fix получает fix с проигравшего дубля."""
    f_winner = make_finding(message="одна и та же", line=5, severity="high", confidence=0.9)
    f_loser_with_fix = make_finding(
        message="одна и та же",
        line=5,
        severity="low",
        confidence=0.5,
        fix_start=5,
        fix_end=5,
        replacement="исправленный код",
    )
    result = dedup_findings([f_winner, f_loser_with_fix], similarity=0.9)
    assert len(result) == 1
    r = result[0]
    assert r.severity == "high"
    assert r.fix_start == 5
    assert r.fix_end == 5
    assert r.replacement == "исправленный код"


def test_fix_not_overwritten_if_winner_has_fix():
    """Если у победителя уже есть fix — он не перезаписывается."""
    f1 = make_finding(
        message="одна и та же",
        line=5,
        severity="high",
        fix_start=5,
        fix_end=5,
        replacement="правка-победителя",
    )
    f2 = make_finding(
        message="одна и та же",
        line=5,
        severity="low",
        fix_start=5,
        fix_end=5,
        replacement="правка-проигравшего",
    )
    result = dedup_findings([f1, f2], similarity=0.9)
    assert len(result) == 1
    assert result[0].replacement == "правка-победителя"


# ---------------------------------------------------------------------------
# Изоляция групп: разные файлы / разные категории
# ---------------------------------------------------------------------------

def test_different_files_not_merged():
    f1 = make_finding(file="a.py", message="одинаково", line=5, category="correctness")
    f2 = make_finding(file="b.py", message="одинаково", line=5, category="correctness")
    result = dedup_findings([f1, f2], similarity=0.9)
    assert len(result) == 2


def test_different_categories_not_merged():
    f1 = make_finding(category="correctness", message="одинаково", line=5)
    f2 = make_finding(category="security", message="одинаково", line=5)
    result = dedup_findings([f1, f2], similarity=0.9)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Порядок выживших
# ---------------------------------------------------------------------------

def test_order_preserved():
    """Порядок выживших должен соответствовать порядку во входном списке."""
    f1 = make_finding(message="первое замечание", line=1, category="correctness")
    f2 = make_finding(message="второе замечание", line=2, category="correctness")
    f3 = make_finding(message="третье замечание", line=3, category="correctness")
    result = dedup_findings([f1, f2, f3])
    assert result == [f1, f2, f3]


def test_order_after_dedup_preserved():
    """После схлопывания порядок выживших — как во входе (не пересортированный)."""
    f1 = make_finding(message="первый уникальный", line=1)
    f2 = make_finding(message="дубль дубль дубль", line=10)
    f3 = make_finding(message="второй уникальный", line=20)
    f4 = make_finding(message="дубль дубль дубль", line=10)  # дубль f2
    result = dedup_findings([f1, f2, f3, f4])
    assert len(result) == 3
    # f1 — первый, f2 — второй, f3 — третий; f4 схлопнулась в f2
    assert result[0] is f1
    assert result[1] is f2
    assert result[2] is f3


# ---------------------------------------------------------------------------
# Нормализация message в точном dedup (PRI-152)
# ---------------------------------------------------------------------------

def test_exact_dedup_collapses_on_case_difference():
    """Находки, различающиеся ТОЛЬКО регистром и пунктуацией, схлопываются на точном шаге.

    similarity=1.0 отключает fuzzy-шаг для этой пары (SequenceMatcher ratio < 1.0
    из-за точки), поэтому дубль должен пойматься именно точным шагом по нормализации.
    """
    f1 = make_finding(message="Баг X.", line=5)
    f2 = make_finding(message="баг x", line=5)
    result = dedup_findings([f1, f2], similarity=1.0)
    assert len(result) == 1


def test_exact_dedup_collapses_on_trailing_punctuation():
    """Находки, различающиеся ТОЛЬКО финальной точкой в message, схлопываются на точном шаге.

    similarity=1.0 отключает fuzzy — дубль должен пойматься именно точным шагом.
    """
    f1 = make_finding(message="небезопасная функция", line=5)
    f2 = make_finding(message="небезопасная функция.", line=5)
    result = dedup_findings([f1, f2], similarity=1.0)
    assert len(result) == 1


def test_exact_dedup_collapses_on_extra_spaces():
    """Находки, различающиеся ТОЛЬКО лишним пробелом в message, схлопываются на точном шаге.

    similarity=1.0 отключает fuzzy — дубль должен пойматься именно точным шагом.
    """
    f1 = make_finding(message="переменная не используется", line=5)
    f2 = make_finding(message="переменная  не используется", line=5)
    result = dedup_findings([f1, f2], similarity=1.0)
    assert len(result) == 1


def test_fuzzy_dedup_uses_normalized_messages():
    """Fuzzy-dedup для соседних строк с дрейфом регистра/пунктуации схлопывает находки."""
    f1 = make_finding(message="небезопасный вызов функции func", line=10)
    f2 = make_finding(message="Небезопасный вызов функции func.", line=11)
    result = dedup_findings([f1, f2], similarity=0.9)
    assert len(result) == 1
