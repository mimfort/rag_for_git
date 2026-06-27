"""Unit-тесты PostToolUse-хука brief_cost (токены этапа solve-task в брифе)."""
import importlib.util
import json
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


def test_message_text_handles_list_content():
    line = {"message": {"content": [
        {"type": "text", "text": "Base directory for this skill: skills/solve-task"},
    ]}}
    assert "solve-task" in bc._message_text(line)


def test_find_window_start_returns_last_marker():
    lines = [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "user", "message": {
            "content": "Base directory for this skill: /x/plugin/skills/solve-task\n# Solve Task"}},
        {"type": "assistant", "message": {"model": "m", "usage": {"input_tokens": 1}}},
        {"type": "user", "message": {
            "content": "Base directory for this skill: /x/plugin/skills/solve-task (rerun)"}},
    ]
    assert bc.find_window_start(lines) == 3


def test_find_window_start_absent_returns_minus_one():
    lines = [{"type": "user", "message": {"content": "nothing here"}}]
    assert bc.find_window_start(lines) == -1


def test_aggregate_usage_sums_per_model_skips_sidechain():
    lines = [
        {"type": "user", "message": {
            "content": "Base directory for this skill: skills/solve-task"}},
        {"type": "assistant", "message": {"model": "claude-opus-4-8", "usage": {
            "input_tokens": 100, "output_tokens": 200,
            "cache_creation_input_tokens": 300, "cache_read_input_tokens": 400}}},
        {"type": "assistant", "isSidechain": True, "message": {
            "model": "claude-opus-4-8", "usage": {"input_tokens": 999}}},
        {"type": "assistant", "message": {"model": "claude-haiku-4-5", "usage": {
            "input_tokens": 1, "output_tokens": 2,
            "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}}},
    ]
    by_model = bc.aggregate_usage(lines, 0)
    assert by_model["claude-opus-4-8"] == {
        "fresh_in": 100, "output": 200, "cache_write": 300, "cache_read": 400}
    assert by_model["claude-haiku-4-5"] == {
        "fresh_in": 1, "output": 2, "cache_write": 3, "cache_read": 4}


def test_read_flag_true_only_when_set():
    assert bc.read_flag("solve_task:\n  brief_token_cost: true\n") is True
    assert bc.read_flag("solve_task:\n  brief_token_cost: false\n") is False
    assert bc.read_flag("other: 1\n") is False
    assert bc.read_flag(None) is False


def test_under_briefs_path_guard():
    assert bc._under_briefs("/Users/me/repo/docs/superpowers/briefs/2026-06-27-x.md") is True
    assert bc._under_briefs("/Users/me/repo/docs/superpowers/specs/x.md") is False


def _setup_repo(tmp_path, flag="true", brief_name="x.md", brief_body="# Brief\n"):
    (tmp_path / ".review.yml").write_text(
        f"solve_task:\n  brief_token_cost: {flag}\n", encoding="utf-8")
    briefs = tmp_path / "docs" / "superpowers" / "briefs"
    briefs.mkdir(parents=True)
    brief = briefs / brief_name
    brief.write_text(brief_body, encoding="utf-8")
    return brief


def _write_transcript(tmp_path, rows):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return transcript


_SOLVE_USER = {"type": "user", "message": {
    "content": "Base directory for this skill: skills/solve-task"}}


def test_run_end_to_end_writes_block(tmp_path):
    brief = _setup_repo(tmp_path, brief_body="# Brief — x\n\n## Task\nt\n")
    transcript = _write_transcript(tmp_path, [
        _SOLVE_USER,
        {"type": "assistant", "message": {"model": "claude-opus-4-8", "usage": {
            "input_tokens": 9900, "output_tokens": 164000,
            "cache_creation_input_tokens": 533000, "cache_read_input_tokens": 14200000}}},
    ])
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(brief)},
               "transcript_path": str(transcript), "cwd": str(tmp_path)}
    assert bc.run(payload) == 0
    out = brief.read_text(encoding="utf-8")
    assert "## Токены (этап solve-task)" in out
    assert "Всего: 14.9M токенов" in out


def test_run_noop_when_flag_off(tmp_path):
    brief = _setup_repo(tmp_path, flag="false")
    original = brief.read_text(encoding="utf-8")
    transcript = _write_transcript(tmp_path, [_SOLVE_USER])
    payload = {"tool_input": {"file_path": str(brief)},
               "transcript_path": str(transcript), "cwd": str(tmp_path)}
    assert bc.run(payload) == 0
    assert brief.read_text(encoding="utf-8") == original


def test_run_noop_when_path_not_brief(tmp_path):
    (tmp_path / ".review.yml").write_text(
        "solve_task:\n  brief_token_cost: true\n", encoding="utf-8")
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    spec = specs / "x.md"
    spec.write_text("# Spec\n", encoding="utf-8")
    payload = {"tool_input": {"file_path": str(spec)},
               "transcript_path": "", "cwd": str(tmp_path)}
    assert bc.run(payload) == 0
    assert spec.read_text(encoding="utf-8") == "# Spec\n"


def test_run_noop_when_no_marker(tmp_path):
    brief = _setup_repo(tmp_path)
    transcript = _write_transcript(tmp_path, [
        {"type": "assistant", "message": {"model": "m", "usage": {"input_tokens": 5}}}])
    payload = {"tool_input": {"file_path": str(brief)},
               "transcript_path": str(transcript), "cwd": str(tmp_path)}
    assert bc.run(payload) == 0
    assert brief.read_text(encoding="utf-8") == "# Brief\n"


def test_run_replaces_on_rerun(tmp_path):
    brief = _setup_repo(tmp_path)
    transcript = _write_transcript(tmp_path, [
        _SOLVE_USER,
        {"type": "assistant", "message": {"model": "m", "usage": {"input_tokens": 1000}}}])
    payload = {"tool_input": {"file_path": str(brief)},
               "transcript_path": str(transcript), "cwd": str(tmp_path)}
    bc.run(payload)
    bc.run(payload)
    out = brief.read_text(encoding="utf-8")
    assert out.count("## Токены (этап solve-task)") == 1


def test_hooks_json_registers_posttooluse_write():
    hooks = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    entries = hooks["hooks"]["PostToolUse"]
    write_entries = [e for e in entries if e.get("matcher") == "Write"]
    assert write_entries, "нужен PostToolUse-матчер на Write"
    command = write_entries[0]["hooks"][0]["command"]
    assert "brief_cost.py" in command
    assert "${CLAUDE_PLUGIN_ROOT}" in command


def test_read_flag_fallback_block_style_true():
    assert bc._read_flag_fallback("solve_task:\n  brief_token_cost: true\n") is True


def test_read_flag_fallback_inline_true():
    assert bc._read_flag_fallback("solve_task: {brief_token_cost: true}\n") is True


def test_read_flag_fallback_rejects_truely():
    assert bc._read_flag_fallback("solve_task: {brief_token_cost: truely}\n") is False
    assert bc._read_flag_fallback("solve_task:\n  brief_token_cost: truely\n") is False


def test_read_flag_fallback_false_and_absent():
    assert bc._read_flag_fallback("solve_task:\n  brief_token_cost: false\n") is False
    assert bc._read_flag_fallback("other: 1\n") is False


def test_upsert_block_appends_when_header_only_in_prose():
    brief = "# Brief — test\n\nсм. раздел ## Токены (этап solve-task) ниже\n"
    block = ("## Токены (этап solve-task)\nМодель: x\n"
             "fresh-in 1K · out 1K · cache-write 0 · cache-read 0\nВсего: 2K токенов")
    out = bc.upsert_block(brief, block)
    # стандартный заголовок-строка появился ровно один раз
    assert sum(1 for ln in out.splitlines() if ln.strip() == "## Токены (этап solve-task)") == 1
    assert "см. раздел ## Токены" in out  # прозаическая строка не съедена
