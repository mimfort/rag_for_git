"""Native Codex usage capture via the registered plugin command."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reviewer.metrics.client_usage import measure_usage

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


@pytest.mark.skipif(os.name == "nt", reason="the configured hook command uses POSIX shell syntax")
@pytest.mark.parametrize("plugin_root", [ROOT, ROOT / "plugin"])
def test_registered_usage_hook_records_only_reported_counters(tmp_path, plugin_root):
    manifest = json.loads((plugin_root / ".codex-plugin/plugin.json").read_text())
    source = next(path for path in manifest["hooks"] if path.endswith("/codex-usage.json"))
    config = json.loads((plugin_root / source).read_text())
    command = config["hooks"]["PostModelRequest"][0]["hooks"][0]["command"]
    output = tmp_path / "usage.jsonl"
    result = subprocess.run(
        command, shell=True, capture_output=True, text=True, input=json.dumps(REQUEST),
        env={**os.environ, "PLUGIN_ROOT": str(plugin_root), "REVIEWER_CODEX_USAGE_FILE": str(output)},
        check=True,
    )

    assert not result.stdout
    content = output.read_text()
    assert "private" not in content and "provider details" not in content
    assert json.loads(content)["turn_id"] == "test-turn"
    assert measure_usage([content], "codex-hook")["usage"] == {
        "fresh_in": 40, "output": 6, "cache_write": 0, "cache_read": 10,
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
