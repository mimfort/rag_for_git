"""Unit-тесты парсера корпуса брифов офлайн-харнесса метрик solve-task."""
import pytest

from eval.solve_task_metrics import briefs

BRIEF_WITH_TOKENS = """# Brief — PRI-42 пример

## Relevant code
- `reviewer/mcp/service.py:944` — точка входа
- reviewer/policy/policy.py:12 — гейтинг
- `web/Dockerfile:26` — сборка фронта
- (dropped 3: не информируют реализацию)

## Test exemplars
- `tests/policy/test_policy.py:10` — образец

## Токены (этап solve-task)
Модель: claude-opus-4-8
fresh-in 9.9K · out 164K · cache-write 533K · cache-read 14.2M
Всего: 14.9M токенов

В т.ч. sidechain-сабагент:
Модель: claude-sonnet-4-5
fresh-in 500 · out 2K · cache-write 10K · cache-read 1.5M
Sidechain всего: 1.5M токенов
"""


def test_parse_human_tokens_units():
    assert briefs.parse_human_tokens("900") == 900.0
    assert briefs.parse_human_tokens("51.2K") == 51_200.0
    assert briefs.parse_human_tokens("3.3M") == 3_300_000.0


def test_parse_human_tokens_rejects_garbage():
    with pytest.raises(ValueError):
        briefs.parse_human_tokens("много")


def test_parse_token_block_main_and_sidechain():
    block = briefs.parse_token_block(BRIEF_WITH_TOKENS)
    assert block is not None
    assert block.main_by_model["claude-opus-4-8"] == {
        "fresh_in": 9_900.0,
        "output": 164_000.0,
        "cache_write": 533_000.0,
        "cache_read": 14_200_000.0,
    }
    assert block.sidechain_by_model["claude-sonnet-4-5"]["output"] == 2_000.0
    assert block.main_total() == pytest.approx(14_906_900.0)
    assert block.sidechain_total() == pytest.approx(1_512_500.0)


def test_parse_token_block_absent_returns_none():
    assert briefs.parse_token_block("# Brief — PRI-1\n\n## Task\nбез токенов\n") is None


def test_extract_section_paths_backticks_bare_and_dropped():
    paths = briefs.extract_section_paths(BRIEF_WITH_TOKENS, briefs.RELEVANT_HEADER)
    assert paths == {
        "reviewer/mcp/service.py",
        "reviewer/policy/policy.py",
        "web/Dockerfile",
    }


def test_extract_section_paths_test_section_is_separate():
    assert briefs.extract_section_paths(BRIEF_WITH_TOKENS, briefs.TEST_HEADER) == {
        "tests/policy/test_policy.py",
    }


def test_extract_section_paths_missing_header_is_empty():
    assert briefs.extract_section_paths("# Brief\n", briefs.RELEVANT_HEADER) == set()


def test_extract_task_key_from_filename():
    assert briefs.extract_task_key("2026-08-14-PRI-250-harness.md") == "PRI-250"
    assert briefs.extract_task_key("2026-08-14-pri-42-x.md") == "PRI-42"
    assert briefs.extract_task_key("2026-08-14-no-key.md") is None


def test_load_briefs_reads_corpus(tmp_path):
    (tmp_path / "2026-01-01-PRI-7-a.md").write_text(BRIEF_WITH_TOKENS, encoding="utf-8")
    (tmp_path / "2026-01-02-plain.md").write_text("# Brief\n", encoding="utf-8")

    records = briefs.load_briefs(tmp_path)

    assert [r.task_key for r in records] == ["PRI-7", None]
    assert records[0].token_block is not None
    assert records[1].token_block is None
    assert records[1].relevant_paths == set()
