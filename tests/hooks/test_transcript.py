import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugin" / "hooks"))

import _transcript  # noqa: E402


def _assistant(model, usage, sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain,
            "message": {"model": model, "usage": usage}}


def test_weigh_uses_bucket_weights_not_plain_sum():
    bucket = {"fresh_in": 100, "output": 100, "cache_write": 100, "cache_read": 100}
    # 100*1 + 100*5 + 100*1.25 + 100*0.1 = 735.0, а простая сумма дала бы 400
    assert _transcript.weigh(bucket) == 735.0


def test_find_window_start_is_parameterised_by_marker():
    lines = [
        {"type": "user", "message": {"content": "Base directory for this skill: x/skills/solve-task"}},
        {"type": "user", "message": {"content": "Base directory for this skill: x/skills/review-pr"}},
    ]
    assert _transcript.find_window_start(lines, "skills/review-pr") == 1
    assert _transcript.find_window_start(lines, "skills/solve-task") == 0
    assert _transcript.find_window_start(lines, "skills/nope") == -1


def test_aggregate_usage_splits_main_and_sidechain():
    lines = [
        {"type": "user", "message": {"content": "start"}},
        _assistant("opus", {"input_tokens": 10, "output_tokens": 2}),
        _assistant("sonnet", {"input_tokens": 5, "cache_read_input_tokens": 7}, sidechain=True),
    ]
    main, side = _transcript.aggregate_usage(lines, 0)
    assert main["opus"]["fresh_in"] == 10
    assert main["opus"]["output"] == 2
    assert side["sonnet"]["cache_read"] == 7
    assert "sonnet" not in main


def test_resolve_transcript_prefers_payload_path(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    assert _transcript.resolve_transcript({"transcript_path": str(path)}) == (str(path), "payload")


def test_resolve_transcript_falls_back_to_session_id(tmp_path, monkeypatch):
    projects = tmp_path / ".claude" / "projects" / "slug"
    projects.mkdir(parents=True)
    transcript = projects / "sess-1.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    found, source = _transcript.resolve_transcript({"session_id": "sess-1"})
    assert found == str(transcript)
    assert source == "session_id"


def test_resolve_transcript_returns_none_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _transcript.resolve_transcript({}) == (None, None)


def test_read_jsonl_skips_broken_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"a": 1}\nnot json\n\n{"b": 2}\n', encoding="utf-8")
    assert _transcript.read_jsonl(str(path)) == [{"a": 1}, {"b": 2}]
