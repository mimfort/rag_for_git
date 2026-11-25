import json
import sqlite3
import subprocess

import pytest

from eval.pri278 import briefs, report
from eval.pri298 import replay
from eval.pri299 import pilot
from reviewer.mcp.brief_evidence import _visible_lines


def _case(key):
    return {"key": key, "task": f"Update {key}", "context": (
        f"// {key}.py#f ({key}.py:1-1)\n1 | def f(): pass"
    )}


def _brief(key):
    return {"key": key, "brief": f"## Relevant code\n- {key}.py:1 — проверить\n\n"}


def _output(tmp_path):
    (tmp_path / "schema.json").write_text(json.dumps(briefs.SCHEMA), encoding="utf-8")
    return tmp_path / "batch-1.json"


def _respond(monkeypatch, responses):
    requests = []
    remaining = iter(responses)

    def run(args, *, input, **kwargs):
        requests.append([item["key"] for item in json.loads(input.splitlines()[-1])])
        rows, tokens = next(remaining)
        if isinstance(rows, BaseException):
            raise rows
        usage = tokens if isinstance(tokens, dict) else {"input_tokens": tokens, "output_tokens": 2}
        events = [
            {"type": "item.completed", "item": {
                "type": "agent_message", "text": json.dumps({"briefs": rows})}},
            {"type": "turn.completed", "usage": usage},
        ]
        return subprocess.CompletedProcess(args, 0, "\n".join(map(json.dumps, events)), "")

    monkeypatch.setattr(briefs.subprocess, "run", run)
    return requests


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_preserves_unique_briefs_and_retries_only_missing_keys(monkeypatch, tmp_path):
    batch = [_case(key) for key in ("keep", "duplicate", "missing", "blank", "no_refs", "invalid")]
    keep = _brief("keep")
    first = [keep, _brief("duplicate"), _brief("duplicate"), _brief("unexpected"),
             {"key": "blank", "brief": " \t"}, {"key": "no_refs", "brief": "Нет ссылок"},
             {"key": "invalid", "brief": None}, {"brief": "Нельзя связать с задачей"}]
    requests = _respond(monkeypatch, [(first, 100), ([_brief("missing")], 20)])
    output = _output(tmp_path)

    briefs.run_batch(batch, tmp_path, output)

    result = _read(output)
    assert requests == [[row["key"] for row in batch], ["missing"]]
    assert {b["key"]: b["brief"] for b in result["briefs"]} == {
        "keep": keep["brief"], "missing": _brief("missing")["brief"], "no_refs": "Нет ссылок",
    }
    assert result["coverage"]["first"]["statuses"] == {
        "keep": "verified", "duplicate": "duplicate", "missing": "missing", "blank": "blank",
        "no_refs": "no_verified_refs", "invalid": "invalid",
    }
    assert result["coverage"]["final"]["statuses"]["missing"] == "verified"
    assert result["coverage"]["first"]["unexpected_keys"] == {"unexpected": 1}
    assert result["coverage"]["first"]["unkeyed_records"] == 1
    assert result["usage"] == {"input_tokens": 120, "output_tokens": 4}
    assert result["coverage"]["first"]["returned"] == 2
    assert result["coverage"]["final"]["returned"] == 3
    assert list(tmp_path.glob("batch-*.json")) == [output]

    before = output.read_bytes()
    briefs.run_batch(batch, tmp_path, output)
    assert len(requests) == 2
    assert output.read_bytes() == before


def test_isolated_retry_is_not_repeated_after_interruption(monkeypatch, tmp_path):
    batch = [_case(key) for key in ("keep", "interrupted", "complete")]
    requests = _respond(monkeypatch, [([_brief("keep")], 100), (KeyboardInterrupt(), 0),
                                      ([_brief("complete")], 20)])
    output = _output(tmp_path)

    with pytest.raises(KeyboardInterrupt):
        briefs.run_batch(batch, tmp_path, output)
    assert not output.exists()
    with pytest.raises(ValueError, match="другому входу"):
        briefs.run_batch(list(reversed(batch)), tmp_path, output)

    briefs.run_batch(batch, tmp_path, output)

    result = _read(output)
    assert requests == [["keep", "interrupted", "complete"], ["interrupted"], ["complete"]]
    assert [b["key"] for b in result["briefs"]] == ["keep", "complete"]
    assert result["coverage"]["final"]["statuses"]["interrupted"] == "missing"
    assert result["retry_attempts"]["interrupted"]["state"] == "interrupted"
    assert result["unknown_usage_attempts"] == 1


def test_exhausted_retry_and_full_response_are_stable(monkeypatch, tmp_path):
    batch = [_case("a"), _case("b")]
    requests = _respond(monkeypatch, [([_brief("a")], 110), ([], 12),
                                      ([_brief("a"), _brief("b")], 101)])
    output = _output(tmp_path)
    briefs.run_batch(batch, tmp_path, output)
    briefs.run_batch(batch, tmp_path, output)
    full = tmp_path / "batch-2.json"
    briefs.run_batch(batch, tmp_path, full)
    briefs.run_batch(batch, tmp_path, full)

    assert requests == [["a", "b"], ["b"], ["a", "b"]]
    assert _read(output)["coverage"]["final"]["statuses"]["b"] == "missing"
    assert _read(full)["coverage"]["final"]["statuses"] == {"a": "verified", "b": "verified"}


def test_failed_retry_preserves_previous_brief_and_reports_unknown_usage(monkeypatch, tmp_path):
    batch = [_case("a"), _case("b")]
    timeout = subprocess.TimeoutExpired(cmd=["codex"], timeout=540)
    requests = _respond(monkeypatch, [([_brief("a")], 110), (timeout, 0)])
    output = _output(tmp_path)
    briefs.run_batch(batch, tmp_path, output)
    briefs.run_batch(batch, tmp_path, output)

    result = _read(output)
    assert requests == [["a", "b"], ["b"]]
    assert [b["brief"] for b in result["briefs"]] == [_brief("a")["brief"]]
    assert result["coverage"]["final"]["statuses"]["b"] == "missing"
    assert result["retry_attempts"]["b"] == {"state": "failed", "error": "TimeoutExpired"}
    assert result["unknown_usage_attempts"] == 1


@pytest.mark.parametrize("change", ["task", "context", "order", "model", "prompt", "schema"])
def test_resume_rejects_different_inputs_without_rewriting(monkeypatch, tmp_path, change):
    batch = [_case("a"), _case("b")]
    requests = _respond(monkeypatch, [([_brief("a"), _brief("b")], 100)])
    output = _output(tmp_path)
    briefs.run_batch(batch, tmp_path, output)
    original = output.read_bytes()
    prompt = briefs.PROMPT
    if change in {"task", "context"}:
        batch[0][change] += " another input"
    elif change == "order":
        batch.reverse()
    elif change == "model":
        monkeypatch.setattr(briefs, "MODEL", "test-other-model")
    elif change == "prompt":
        prompt += " changed"
    else:
        (tmp_path / "schema.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="другому входу"):
        briefs.run_batch(batch, tmp_path, output, prompt=prompt)
    assert len(requests) == 1
    assert output.read_bytes() == original


def test_legacy_batch_is_preserved_without_an_implicit_resume(monkeypatch, tmp_path):
    batch = [_case("a")]
    output = _output(tmp_path)
    legacy = '{"briefs": []}\n'
    output.write_text(legacy, encoding="utf-8")
    requests = _respond(monkeypatch, [])

    with pytest.raises(ValueError, match="другому входу"):
        briefs.run_batch(batch, tmp_path, output)
    assert requests == []
    assert output.read_text(encoding="utf-8") == legacy


def test_same_batch_cannot_run_concurrently(monkeypatch, tmp_path):
    batch = [_case("a"), _case("b")]
    output = _output(tmp_path)
    requests = _respond(monkeypatch, [([_brief("a")], 100), ([_brief("b")], 20)])
    respond = briefs.subprocess.run

    def concurrent_attempt(*args, **kwargs):
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            briefs.run_batch(batch, tmp_path, output)
        return respond(*args, **kwargs)

    monkeypatch.setattr(briefs.subprocess, "run", concurrent_attempt)
    briefs.run_batch(batch, tmp_path, output)
    assert requests == [["a", "b"], ["b"]]
    assert _read(output)["coverage"]["final"]["returned"] == 2


def _archive_scores(batch, labels, rows):
    paths_by_key = {row["key"]: set(row["verified_paths"]) for row in rows}
    by_key = {row["key"]: row["brief"] for row in rows}
    scores = {}
    for group in ("all", "bulk", "ordinary"):
        cases = [case for case in labels if group == "all" or case["bulk"] == (group == "bulk")]
        scores[group] = {}
        for label in ("obligatory", "read_only"):
            expected = {case["key"]: {ref["path"] for ref in case[label]} for case in cases}
            scores[group][label] = [
                sum(len(paths & paths_by_key.get(key, set())) for key, paths in expected.items()),
                sum(map(len, expected.values())),
            ]

    valid_body = canonical = 0
    for item in batch:
        key = item["key"]
        if key in by_key:
            _, _, errors, header = pilot.score_markdown(
                {"key": key, "brief": by_key[key]}, _visible_lines(item["context"]),
            )
            valid_body += not errors
            canonical += header
    scores["citation_syntax_ok"] = [valid_body, len(batch)]
    scores["canonical_heading"] = [canonical, len(batch)]
    return scores


def test_archive_keeps_first_five_and_replays_only_pri_221(monkeypatch, tmp_path):
    archive = _read(pilot.HERE / "responses.json")
    batch = json.loads(pilot.load_input())
    labels = _read(pilot.HERE.parent / "pri278/independent-corpus.json")
    labels = [case for case in labels if case["key"] in archive["tasks"]]
    first, second = (run for run in archive["runs"] if run["condition"] == "markdown")
    first_rows = json.loads(first["response"])["briefs"]
    recovered = [row for row in json.loads(second["response"])["briefs"] if row["key"] == "PRI-221"]

    with pytest.raises(ValueError, match="omitted or duplicated"):
        list(briefs.check_briefs(first_rows, batch))
    # The recovered row came from a full second run. Its isolated usage is unknown.
    requests = _respond(monkeypatch, [(first_rows, first["usage"]), (recovered, {})])
    directory = tmp_path / "luna-all-archive"
    directory.mkdir()
    output = directory / _output(tmp_path).name
    briefs.run_batch(batch, tmp_path, output)

    result = _read(output)
    ledger = _read(directory / ".batch-ledgers" / output.name)
    assert requests == [archive["tasks"], ["PRI-221"]]
    assert result["coverage"]["first"]["returned"] == 5
    assert result["coverage"]["final"]["returned"] == 6
    assert result["usage"] == first["usage"]
    assert result["unknown_usage_attempts"] == 1
    expected = {r["key"]: r["brief"] for r in first_rows + recovered}
    assert {r["key"]: r["brief"] for r in result["briefs"]} == expected
    assert report.model_batches(tmp_path, "all-archive") == [result]
    assert replay.model_briefs(tmp_path, "archive", set(expected))[0] == expected
    first_scores = _archive_scores(batch, labels, list(ledger["first"]["briefs"].values()))
    final_scores = _archive_scores(batch, labels, result["briefs"])
    assert first_scores == {
        "all": {"obligatory": [18, 54], "read_only": [3, 16]},
        "bulk": {"obligatory": [9, 36], "read_only": [2, 7]},
        "ordinary": {"obligatory": [9, 18], "read_only": [1, 9]},
        "citation_syntax_ok": [5, 6],
        "canonical_heading": [0, 6],
    }
    assert final_scores == {
        "all": {"obligatory": [22, 54], "read_only": [4, 16]},
        "bulk": {"obligatory": [13, 36], "read_only": [3, 7]},
        "ordinary": {"obligatory": [9, 18], "read_only": [1, 9]},
        "citation_syntax_ok": [6, 6],
        "canonical_heading": [0, 6],
    }
    print(json.dumps({"first": first_scores, "final": final_scores}, ensure_ascii=False))
