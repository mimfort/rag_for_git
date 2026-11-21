import json

import pytest
from click.testing import CliRunner

from reviewer.entrypoints.cli import cli
from reviewer.metrics.client_usage import measure_usage


def events(*rows):
    return [json.dumps(row) for row in rows]


def test_codex_counts_cached_input_and_reasoning_once():
    usage = {"input_tokens": 120, "cached_input_tokens": 40, "cache_write_input_tokens": 10,
             "output_tokens": 30, "reasoning_output_tokens": 7}
    report = measure_usage(
        events({"type": "turn.started"}, {"type": "turn.completed", "usage": usage}),
        "codex-exec", model="gpt-6-luna",
    )
    total = {"fresh_in": 70, "output": 30, "cache_write": 10, "cache_read": 40}
    assert report == {"source": "codex-exec", "records": 1, "usage": total,
                      "by_model": {"gpt-6-luna": total}}


def test_claude_keeps_final_usage_snapshot_per_message():
    usage = {"input_tokens": 20, "output_tokens": 4,
             "cache_creation_input_tokens": 2, "cache_read_input_tokens": 8}
    frames = events(
        {"type": "assistant", "message": {"id": "m1", "model": "claude", "usage": usage}},
        {"type": "assistant", "message": {"id": "m1", "model": "claude",
                                          "usage": {**usage, "output_tokens": 9}}},
        {"type": "user"},
    )
    report = measure_usage(frames, "claude-code")
    assert report["usage"] == {"fresh_in": 20, "output": 9, "cache_write": 2, "cache_read": 8}
    assert report["records"] == 1


def test_normalized_events_from_other_clients_can_be_grouped():
    a = {"fresh_in": 5, "output": 2, "cache_write": 0, "cache_read": 10}
    b = {"fresh_in": 6, "output": 1, "cache_write": 1, "cache_read": 2}
    report = measure_usage(events({"type": "llm_usage", "model": "a", "usage": a},
                                   {"type": "llm_usage", "model": "b", "usage": b}), "normalized")
    assert report["usage"] == {"fresh_in": 11, "output": 3, "cache_write": 1, "cache_read": 12}
    assert report["by_model"] == {"a": a, "b": b}


def codex_snapshot(thread, total, last):
    return {
        "method": "thread/tokenUsage/updated",
        "params": {"threadId": thread, "tokenUsage": {"total": total, "last": last}},
    }


def test_codex_app_server_resumed_stream_counts_last_and_then_deltas_once():
    first = codex_snapshot(
        "resumed",
        {"inputTokens": 50, "cachedInputTokens": 17, "outputTokens": 10},
        {"inputTokens": 15, "cachedInputTokens": 3, "outputTokens": 4},
    )
    second = codex_snapshot(
        "resumed",
        {"inputTokens": 75, "cachedInputTokens": 25, "outputTokens": 16},
        {"inputTokens": 10, "cachedInputTokens": 4, "outputTokens": 2},
    )
    new_usage = {"inputTokens": 8, "cachedInputTokens": 2,
                 "cacheWriteInputTokens": 1, "outputTokens": 5}
    separate = codex_snapshot("new", new_usage, new_usage)
    report = measure_usage(events(first, separate, second, second), "codex-app-server",
                           model="gpt-6-luna")

    assert report["records"] == 3
    assert report["usage"] == {"fresh_in": 34, "output": 15, "cache_write": 1, "cache_read": 13}
    assert report["by_model"] == {"gpt-6-luna": report["usage"]}


def test_codex_app_server_rejects_backwards_snapshots():
    first = {"inputTokens": 15, "cachedInputTokens": 8, "outputTokens": 5}
    older = {"inputTokens": 10, "cachedInputTokens": 7, "outputTokens": 4}

    with pytest.raises(ValueError, match=r"line 2: cumulative usage decreased"):
        measure_usage(events(codex_snapshot("one", first, first),
                             codex_snapshot("one", older, older)), "codex-app-server")


@pytest.mark.parametrize("usage", [
    {"input_tokens": 4, "cached_input_tokens": 5, "output_tokens": 1},
    {"input_tokens": -1, "output_tokens": 1},
    {"input_tokens": True, "output_tokens": 1},
    {"output_tokens": 1},
])
def test_rejects_corrupt_codex_record_with_line_number(usage):
    with pytest.raises(ValueError, match="line 2"):
        measure_usage(events({"type": "turn.started"}, {"type": "turn.completed", "usage": usage}),
                      "codex-exec")


def test_cli_reads_jsonl_from_stdin_and_returns_machine_readable_usage():
    input_text = json.dumps({"type": "turn.completed",
                             "usage": {"input_tokens": 9, "output_tokens": 4}})
    result = CliRunner().invoke(cli, ["measure-client-usage", "--source", "codex-exec", "--model",
                                     "gpt-6-luna"], input=input_text)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["usage"]["fresh_in"] == 9
