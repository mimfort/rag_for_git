"""Unit-тесты полной цены задачи «под ключ» по транскриптам сессий."""
import json

from eval.solve_task_metrics import endtoend


def _user(text):
    return {"type": "user", "message": {"content": text}}


def _assistant(model, out):
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "usage": {
                "input_tokens": 100,
                "output_tokens": out,
                "cache_creation_input_tokens": 1_000,
                "cache_read_input_tokens": 10_000,
            },
        },
    }


SKILL_CALL = (
    "Base directory for this skill: /x/skills/solve-task\n"
    "# Solve Task\n\n`PRI-250` is either:\n"
)


def test_window_start_finds_marker_and_key():
    lines = [_assistant("m", 1), _user(SKILL_CALL), _assistant("m", 2)]

    key, idx = endtoend.window_start(lines)

    assert key == "PRI-250"
    assert idx == 1


def test_window_start_without_markers():
    assert endtoend.window_start([_user("привет")]) == (None, -1)


def test_window_start_without_key_is_unusable():
    lines = [_user("Base directory for this skill: /x/skills/solve-task\nбез ключа")]

    key, idx = endtoend.window_start(lines)

    assert key is None


def test_window_does_not_depend_on_brief_writes():
    """Окно тянется до конца транскрипта и не закрывается записью брифа."""
    lines = [
        _user(SKILL_CALL),
        _assistant("m", 10),
        _user("Записал бриф в docs/superpowers/briefs/x.md"),
        _assistant("m", 20),
    ]

    _, idx = endtoend.window_start(lines)
    buckets = endtoend.aggregate_after(lines, idx)

    assert buckets["output"] == 30


def test_aggregate_after_ignores_turns_before_window():
    lines = [_assistant("m", 999), _user(SKILL_CALL), _assistant("m", 5)]

    _, idx = endtoend.window_start(lines)

    assert endtoend.aggregate_after(lines, idx)["output"] == 5


def test_scan_transcripts_sums_sessions_of_one_task(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    for name in ("a.jsonl", "b.jsonl"):
        rows = [_user(SKILL_CALL), _assistant("m", 10)]
        (project / name).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
        )

    result = endtoend.scan_transcripts(tmp_path)

    assert result["PRI-250"]["sessions"] == 2
    assert result["PRI-250"]["buckets"]["output"] == 20
    assert result["PRI-250"]["weighted"] > 0


def test_scan_transcripts_skips_sessions_without_key(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    rows = [_user("обычная сессия"), _assistant("m", 10)]
    (project / "a.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )

    assert endtoend.scan_transcripts(tmp_path) == {}


def test_scan_transcripts_missing_root_is_empty(tmp_path):
    assert endtoend.scan_transcripts(tmp_path / "нет") == {}


# --- Regression: ключ не должен ловиться из статического примера в теле скилла.

FREE_TEXT_CALL = (
    "Base directory for this skill: /x/skills/solve-task\n"
    "# Solve Task\n\n"
    "`сделать логаут-эндпоинт` is either:\n"
    "- a task key (e.g. `PRI-4`, matching the board's `key_pattern`), or\n"
    "- a free-text description (e.g. \"add a logout endpoint\").\n"
)


def test_window_start_free_text_call_does_not_leak_example_key():
    """Свободнотекстовый вызов не должен приписываться ключу из примера `PRI-4`."""
    lines = [_user(FREE_TEXT_CALL)]

    key, idx = endtoend.window_start(lines)

    assert key is None
    assert idx == 0


def test_window_start_explicit_key_still_recognized():
    lines = [_user(SKILL_CALL)]

    key, idx = endtoend.window_start(lines)

    assert key == "PRI-250"
    assert idx == 0


def test_scan_transcripts_free_text_call_does_not_create_pri4_bucket(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    rows = [_user(FREE_TEXT_CALL), _assistant("m", 10)]
    (project / "a.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )

    result = endtoend.scan_transcripts(tmp_path)

    assert "PRI-4" not in result
    assert result == {}


# --- Несколько вызовов solve-task в одном транскрипте.

SKILL_CALL_B = (
    "Base directory for this skill: /x/skills/solve-task\n"
    "# Solve Task\n\n`PRI-9` is either:\n"
)


def test_find_windows_returns_one_window_per_call():
    lines = [_user(SKILL_CALL), _assistant("m", 10), _user(SKILL_CALL_B), _assistant("m", 20)]

    windows = endtoend.find_windows(lines)

    assert [w[0] for w in windows] == ["PRI-250", "PRI-9"]
    assert windows[0] == ("PRI-250", 0, 2)
    assert windows[1] == ("PRI-9", 2, 4)


def test_two_calls_in_one_transcript_do_not_leak_into_each_other(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    rows = [
        _user(SKILL_CALL),
        _assistant("m", 10),
        _user(SKILL_CALL_B),
        _assistant("m", 20),
    ]
    (project / "a.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )

    result = endtoend.scan_transcripts(tmp_path)

    assert result["PRI-250"]["buckets"]["output"] == 10
    assert result["PRI-9"]["buckets"]["output"] == 20
    assert result["PRI-250"]["sessions"] == 1
    assert result["PRI-9"]["sessions"] == 1
