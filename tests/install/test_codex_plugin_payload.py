import json
from pathlib import Path

from reviewer.install_codex import (
    expected_plugin_version,
    payload_digest,
    project_manifest_from,
    sync_plugin_metadata,
)

ROOT = Path(__file__).resolve().parents[2]


def test_project_manifest_rewrites_only_payload_relative_paths():
    canonical = {
        "name": "rag-reviewer",
        "version": "0.2.27+codex.123456789abc",
        "skills": "./skills/",
        "interface": {"composerIcon": "./assets/icon.svg"},
    }
    projected = project_manifest_from(canonical)
    assert projected["skills"] == "./plugin/skills/"
    assert projected["interface"]["composerIcon"] == "./plugin/assets/icon.svg"
    assert projected["version"] == canonical["version"]


def test_payload_digest_ignores_only_manifest_version(tmp_path):
    plugin = tmp_path / "plugin"
    manifest = plugin / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "rag-reviewer", "version": "1+codex.first"}))
    (plugin / "skills" / "ask").mkdir(parents=True)
    (plugin / "skills" / "ask" / "SKILL.md").write_text("ask")
    first = payload_digest(plugin)
    manifest.write_text(json.dumps({"name": "rag-reviewer", "version": "1+codex.second"}))
    assert payload_digest(plugin) == first
    (plugin / "skills" / "ask" / "SKILL.md").write_text("changed")
    assert payload_digest(plugin) != first


def test_repo_codex_payload_is_synchronized():
    assert sync_plugin_metadata(ROOT, check=True) == []
    canonical = json.loads(
        (ROOT / "plugin/.codex-plugin/plugin.json").read_text(encoding="utf-8")
    )
    assert canonical["version"] == expected_plugin_version(ROOT)
    assert "mcpServers" not in canonical
