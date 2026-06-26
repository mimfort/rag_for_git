"""Unit-тесты PostToolUse-хука brief_cost (токены этапа solve-task в брифе)."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOK_PATH = ROOT / "plugin" / "hooks" / "brief_cost.py"


def _load():
    spec = importlib.util.spec_from_file_location("brief_cost", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bc = _load()


def test_human_tokens_formats_k_and_m():
    assert bc.human_tokens(512) == "512"
    assert bc.human_tokens(9900) == "9.9K"
    assert bc.human_tokens(164000) == "164K"
    assert bc.human_tokens(14200000) == "14.2M"


def test_render_block_single_model():
    by_model = {
        "claude-opus-4-8": {
            "fresh_in": 9900, "output": 164000,
            "cache_write": 533000, "cache_read": 14200000,
        },
    }
    block = bc.render_block(by_model)
    assert block.splitlines()[0] == "## Токены (этап solve-task)"
    assert "Модель: claude-opus-4-8" in block
    assert "fresh-in 9.9K · out 164K · cache-write 533K · cache-read 14.2M" in block
    # сумма всех бакетов = 9900+164000+533000+14200000 = 14_906_900 → 14.9M
    assert "Всего: 14.9M токенов" in block


def test_upsert_block_appends_when_absent():
    brief = "# Brief — test\n\n## Task\nдетали\n"
    block = ("## Токены (этап solve-task)\nМодель: x\n"
             "fresh-in 1K · out 1K · cache-write 0 · cache-read 0\nВсего: 2K токенов")
    out = bc.upsert_block(brief, block)
    assert out.count("## Токены (этап solve-task)") == 1
    assert out.startswith("# Brief — test")
    assert out.rstrip().endswith("Всего: 2K токенов")
    assert out.endswith("\n")


def test_upsert_block_replaces_when_present():
    block_old = ("## Токены (этап solve-task)\nМодель: x\n"
                 "fresh-in 1K · out 1K · cache-write 0 · cache-read 0\nВсего: 2K токенов")
    brief = "# Brief — test\n\n## Task\nдетали\n\n" + block_old + "\n"
    block_new = ("## Токены (этап solve-task)\nМодель: y\n"
                 "fresh-in 3K · out 0 · cache-write 0 · cache-read 0\nВсего: 3K токенов")
    out = bc.upsert_block(brief, block_new)
    assert out.count("## Токены (этап solve-task)") == 1
    assert "Модель: y" in out
    assert "Модель: x" not in out
    assert "## Task" in out
