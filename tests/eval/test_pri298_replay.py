"""Проверки возобновления парного эвала PRI-298."""
import json

import pytest

from eval.pri298 import replay


def test_prepare_requires_saved_context_only_for_completed_cases(tmp_path, monkeypatch):
    cases = [{"key": key, "base_sha": "base", "input": "task", "split": "tune"}
             for key in ("PRI-1", "PRI-2")]
    context = "// a.py#f (a.py:1-1)\n    1 | x = 1"
    archived = {case["key"]: {
        "context": context, "source_chars": 5, "context_chars": len(context),
        "selected_paths": ["a.py"], "elapsed_ms": 0.0,
    } for case in cases}
    monkeypatch.setattr(replay, "frozen_inputs", lambda _: (cases, archived))

    replay.prepare(tmp_path, tmp_path, tmp_path, ("off",))
    batch_dir = tmp_path / "luna-all-off"
    batch_dir.mkdir()
    (batch_dir / "batch-1.json").write_text(json.dumps({"briefs": [{"key": "PRI-1"}]}))
    completed = tmp_path / "all-off" / "PRI-1.json"
    unfinished = tmp_path / "all-off" / "PRI-2.json"
    saved = completed.read_bytes()
    completed.unlink()
    unfinished.unlink()

    with pytest.raises(ValueError, match="all-off/PRI-1"):
        replay.prepare(tmp_path, tmp_path, tmp_path, ("off",))
    assert not completed.exists()

    completed.write_bytes(saved)
    replay.prepare(tmp_path, tmp_path, tmp_path, ("off",))
    assert completed.read_bytes() == saved
    assert json.loads(unfinished.read_text())["modes"]["hybrid"]["context"] == context
