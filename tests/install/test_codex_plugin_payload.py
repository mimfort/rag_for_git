import json
from pathlib import Path

from reviewer.install_codex import (
    expected_plugin_version,
    payload_digest,
    project_manifest_from,
    sync_plugin_metadata,
)

ROOT = Path(__file__).resolve().parents[2]


def _synced_repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    source_icon = tmp_path / "assets" / "icon.svg"
    source_icon.parent.mkdir()
    source_icon.write_text("icon")
    manifest = tmp_path / "plugin" / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "name": "rag-reviewer",
                "version": "0.1.0",
                "skills": "./skills/",
                "interface": {"composerIcon": "./assets/icon.svg"},
            }
        )
    )
    (tmp_path / ".codex-plugin").mkdir()
    assert sync_plugin_metadata(tmp_path, check=False) == []
    return tmp_path


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


def test_payload_digest_frames_file_boundaries(tmp_path):
    one_file = tmp_path / "one-file"
    one_file.mkdir()
    (one_file / "a").write_bytes(b"firstb\0second")
    two_files = tmp_path / "two-files"
    two_files.mkdir()
    (two_files / "a").write_bytes(b"first")
    (two_files / "b").write_bytes(b"second")

    assert payload_digest(one_file) != payload_digest(two_files)


def test_check_rejects_canonical_manifest_with_mcp_servers(tmp_path):
    repo = _synced_repo(tmp_path)
    manifest = repo / "plugin" / ".codex-plugin" / "plugin.json"
    canonical = json.loads(manifest.read_text())
    canonical["mcpServers"] = "./.mcp.json"
    manifest.write_text(json.dumps(canonical))

    assert "Codex manifest must not declare mcpServers" in sync_plugin_metadata(
        repo, check=True
    )


def test_check_is_observational_and_reports_missing_projection_artifacts(tmp_path):
    repo = _synced_repo(tmp_path)
    payload_assets = repo / "plugin" / "assets"
    (payload_assets / "icon.svg").unlink()
    payload_assets.rmdir()
    (repo / ".codex-plugin" / "plugin.json").unlink()

    errors = sync_plugin_metadata(repo, check=True)

    assert "plugin/assets/icon.svg is missing" in errors
    assert "root Codex manifest is missing" in errors
    assert not payload_assets.exists()


def test_check_reports_missing_canonical_manifest(tmp_path):
    repo = _synced_repo(tmp_path)
    (repo / "plugin" / ".codex-plugin" / "plugin.json").unlink()

    assert "canonical Codex manifest is missing" in sync_plugin_metadata(repo, check=True)


def test_check_reports_invalid_canonical_manifest(tmp_path):
    repo = _synced_repo(tmp_path)
    (repo / "plugin" / ".codex-plugin" / "plugin.json").write_text("{")

    assert "canonical Codex manifest is invalid" in sync_plugin_metadata(repo, check=True)


def test_check_reports_invalid_root_manifest(tmp_path):
    repo = _synced_repo(tmp_path)
    (repo / ".codex-plugin" / "plugin.json").write_text("{")

    assert "root Codex manifest is invalid" in sync_plugin_metadata(repo, check=True)


def test_repo_codex_payload_is_synchronized():
    assert sync_plugin_metadata(ROOT, check=True) == []
    canonical = json.loads(
        (ROOT / "plugin/.codex-plugin/plugin.json").read_text(encoding="utf-8")
    )
    assert canonical["version"] == expected_plugin_version(ROOT)
    assert "mcpServers" not in canonical


def test_real_payload_registers_every_skill_directory_dynamically():
    skills_root = ROOT / "plugin/skills"
    expected = sorted(
        path.name for path in skills_root.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    )
    assert "finish-task" in expected
    assert "_common" not in expected
    assert (skills_root / "_common").is_dir()
    manifest = json.loads(
        (ROOT / "plugin/.codex-plugin/plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["skills"] == "./skills/"
    assert len(expected) == len(list(skills_root.glob("*/SKILL.md")))
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    russian = (ROOT / "README.ru.md").read_text(encoding="utf-8")
    for name in expected:
        marker = f"reviewer_{name}"
        assert marker in english
        assert marker in russian
