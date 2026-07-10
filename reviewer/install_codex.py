from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tomllib
from pathlib import Path

PLUGIN_NAME = "rag-reviewer"
MARKETPLACE_NAME = "rag-reviewer"
MARKETPLACE_SOURCE = "mimfort/rag_for_git"
MARKETPLACE_REF = "main"
MARKETPLACE_SPARSE = (".agents/plugins", "plugin")
_NORMALIZED_VERSION = "0.0.0+codex.normalized"
_FORBIDDEN_PAYLOAD_PARTS = {".git", ".env", ".venv", "__pycache__", "build", "dist"}


def project_version(repo_root: Path) -> str:
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _payload_bytes(path: Path, plugin_root: Path) -> bytes:
    rel = path.relative_to(plugin_root).as_posix()
    if rel != ".codex-plugin/plugin.json":
        return path.read_bytes()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = _NORMALIZED_VERSION
    return (json.dumps(data, sort_keys=True, ensure_ascii=False) + "\n").encode()


def payload_digest(plugin_root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in plugin_root.rglob("*") if path.is_file())
    for path in files:
        rel = path.relative_to(plugin_root)
        if any(part in _FORBIDDEN_PAYLOAD_PARTS for part in rel.parts):
            raise ValueError(f"forbidden payload path: {rel.as_posix()}")
        rel_bytes = rel.as_posix().encode()
        payload_bytes = _payload_bytes(path, plugin_root)
        digest.update(len(rel_bytes).to_bytes(8, "big"))
        digest.update(rel_bytes)
        digest.update(len(payload_bytes).to_bytes(8, "big"))
        digest.update(payload_bytes)
    return digest.hexdigest()[:12]


def expected_plugin_version(repo_root: Path) -> str:
    return f"{project_version(repo_root)}+codex.{payload_digest(repo_root / 'plugin')}"


def project_manifest_from(canonical: dict) -> dict:
    projected = copy.deepcopy(canonical)
    projected["skills"] = "./plugin/skills/"
    interface = projected.setdefault("interface", {})
    interface["composerIcon"] = "./plugin/assets/icon.svg"
    projected.pop("mcpServers", None)
    return projected


def _canonical_json(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _checked_json(path: Path, label: str, errors: list[str]) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"{label} is missing")
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        errors.append(f"{label} is invalid")
        return None
    if not isinstance(data, dict):
        errors.append(f"{label} is invalid")
        return None
    return data


def sync_plugin_metadata(repo_root: Path, *, check: bool) -> list[str]:
    plugin_root = repo_root / "plugin"
    canonical_path = plugin_root / ".codex-plugin" / "plugin.json"
    project_path = repo_root / ".codex-plugin" / "plugin.json"
    payload_icon = plugin_root / "assets" / "icon.svg"
    source_icon = repo_root / "assets" / "icon.svg"
    if not check:
        canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        canonical.pop("mcpServers", None)
        payload_icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_icon, payload_icon)
        canonical["version"] = project_version(repo_root)
        canonical_path.write_text(_canonical_json(canonical), encoding="utf-8")
        canonical["version"] = expected_plugin_version(repo_root)
        canonical_path.write_text(_canonical_json(canonical), encoding="utf-8")
        project_path.write_text(
            _canonical_json(project_manifest_from(canonical)), encoding="utf-8"
        )
        return []

    errors: list[str] = []
    canonical = _checked_json(canonical_path, "canonical Codex manifest", errors)
    actual_project = _checked_json(project_path, "root Codex manifest", errors)
    if canonical is not None:
        expected = expected_plugin_version(repo_root)
        if canonical.get("version") != expected:
            errors.append(f"manifest version {canonical.get('version')!r} != {expected!r}")
        if "mcpServers" in canonical:
            errors.append("Codex manifest must not declare mcpServers")
        if (
            actual_project is not None
            and actual_project != project_manifest_from(canonical)
        ):
            errors.append("root Codex manifest is not the canonical path projection")
    try:
        actual_icon = payload_icon.read_bytes()
    except FileNotFoundError:
        errors.append("plugin/assets/icon.svg is missing")
    except OSError:
        errors.append("plugin/assets/icon.svg is invalid")
    else:
        if actual_icon != source_icon.read_bytes():
            errors.append("plugin/assets/icon.svg differs from assets/icon.svg")
    return errors
