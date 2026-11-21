"""Native Codex usage capture via the registered plugin command."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "plugin/hooks/codex_usage.py"
REQUEST = {
    "hook_event_name": "PostModelRequest", "status": "completed", "model": "gpt-6-luna",
    "session_id": "test-session", "turn_id": "test-turn", "transcript_path": "/private/file",
    "error": "provider details", "token_usage": {
        "input_tokens": 50, "cached_input_tokens": 10, "output_tokens": 6,
        "extra_field": "private data",
    },
}


def test_usage_hook_does_not_write_if_unconfigured_or_when_counters_are_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("REVIEWER_CODEX_USAGE_FILE", raising=False)
    subprocess.run([sys.executable, str(HOOK)], input=json.dumps(REQUEST), text=True,
                   cwd=tmp_path, check=True)
    assert not list(tmp_path.iterdir())

    output = tmp_path / "usage.jsonl"
    monkeypatch.setenv("REVIEWER_CODEX_USAGE_FILE", str(output))
    event = {**REQUEST, "status": "failed", "token_usage": None}
    subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), text=True,
                   cwd=tmp_path, check=True)
    assert not output.exists()
