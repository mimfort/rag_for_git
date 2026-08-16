"""Парсер путей из отрендеренного вывода ретрива (PRI-254)."""
from __future__ import annotations

from eval.solve_task_metrics.context_paths import extract_context_paths

SAMPLE = """// reviewer/services/brief_quality.py#measure (reviewer/services/brief_quality.py:87-168)
   87 | def measure(
   88 |     *,
   89 |     task_key: str | None,

// reviewer/web/history.py#ReviewHistory (reviewer/web/history.py:21-601)
   21 | class ReviewHistory:
   22 |     \"\"\"Персистирует историю прогонов ревью.\"\"\"

— контекст обрезан по cliff: 15 из 58 (скор 0.51→0.37, обрыв на 0.37). За обрезом ещё 17 релевантных: reviewer (0.37).
"""


def test_extracts_paths_from_headers():
    assert extract_context_paths(SAMPLE) == {
        "reviewer/services/brief_quality.py",
        "reviewer/web/history.py",
    }


def test_empty_result_marker_yields_no_paths():
    assert extract_context_paths("(ничего не найдено)") == set()


def test_blank_input_yields_no_paths():
    assert extract_context_paths("") == set()


def test_code_line_mentioning_a_path_is_not_a_candidate():
    """Путь в теле кода — не кандидат ретрива: заголовок начинается с '// '."""
    text = (
        "// reviewer/a.py#f (reviewer/a.py:1-3)\n"
        "    1 |     from reviewer.b import thing  # reviewer/b.py\n"
        '    2 |     path = "tests/test_zzz.py"\n'
    )
    assert extract_context_paths(text) == {"reviewer/a.py"}


def test_truncated_header_is_dropped_not_guessed():
    """Обрыв по max_context_chars может разрезать заголовок — половину не берём."""
    text = (
        "// reviewer/a.py#f (reviewer/a.py:1-3)\n"
        "    1 | x = 1\n\n"
        "// reviewer/b.py#g (reviewer/b.py:10-\n"
        "[...truncated]"
    )
    assert extract_context_paths(text) == {"reviewer/a.py"}


def test_degraded_note_is_not_a_path():
    text = "(ничего не найдено)\n\n(реранкер недоступен: выдача деградировала)"
    assert extract_context_paths(text) == set()
