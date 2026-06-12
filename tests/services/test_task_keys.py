"""Unit-тесты извлечения ключей задачи из текстов PR (без сети)."""
from reviewer.services.task_keys import DEFAULT_KEY_PATTERN, extract_task_keys


def test_primary_from_title_precedence_over_body_and_branch():
    out = extract_task_keys(
        DEFAULT_KEY_PATTERN,
        title="SAI-515 add logout",
        body="relates to SAI-517",
        branch="feature/SAI-519-x",
    )
    assert out == {"primary": "SAI-515", "others": ["SAI-517", "SAI-519"]}


def test_primary_falls_back_to_body_then_branch():
    out = extract_task_keys(
        DEFAULT_KEY_PATTERN, title="no key here", body="", branch="feature/SAI-700-x"
    )
    assert out == {"primary": "SAI-700", "others": []}


def test_dedup_keeps_first_appearance_order():
    out = extract_task_keys(
        DEFAULT_KEY_PATTERN, title="SAI-1 SAI-1 SAI-2", body=None, branch=None
    )
    assert out == {"primary": "SAI-1", "others": ["SAI-2"]}


def test_no_match_returns_empty():
    out = extract_task_keys(DEFAULT_KEY_PATTERN, title="nothing", body=None, branch=None)
    assert out == {"primary": None, "others": []}


def test_invalid_pattern_returns_empty():
    out = extract_task_keys("[unclosed", title="SAI-1", body=None, branch=None)
    assert out == {"primary": None, "others": []}


def test_none_pattern_uses_default():
    out = extract_task_keys(None, title="SAI-42 fix", body=None, branch=None)
    assert out == {"primary": "SAI-42", "others": []}
