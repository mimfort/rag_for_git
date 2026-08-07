from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tomllib
from uuid import uuid4
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath, PurePosixPath
from typing import Any, Callable, Literal, Protocol

PLUGIN_NAME = "rag-reviewer"
MARKETPLACE_NAME = "rag-reviewer"
MARKETPLACE_SOURCE = "mimfort/rag_for_git"
MARKETPLACE_GIT_SOURCE = "https://github.com/mimfort/rag_for_git.git"
MARKETPLACE_REF = "main"
MARKETPLACE_SPARSE = (".agents/plugins", "plugin")
_NORMALIZED_VERSION = "0.0.0+codex.normalized"
_FORBIDDEN_PAYLOAD_PARTS = {".git", ".env", ".venv", "build", "dist"}
# Байткод — производное от .py, которые уже в digest: запуск хуков плагина не должен
# ломать install (payload_digest падал на plugin/hooks/__pycache__/*.pyc).
# Метаданные файлового менеджера (.DS_Store, Thumbs.db) появляются сами от простого
# просмотра каталога и в payload не входят — иначе --check краснеет на пустом месте.
_IGNORED_PAYLOAD_PARTS = {"__pycache__", ".DS_Store", "Thumbs.db"}
_IGNORED_PAYLOAD_SUFFIXES = {".pyc", ".pyo"}
_WINDOWS_REPARSE_POINT = 0x400


def _link_kind(path: Path, label: str) -> str | None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"{label}: cannot inspect {path}: {exc}") from exc
    if stat.S_ISLNK(status.st_mode):
        return "symlink"
    attributes = getattr(status, "st_file_attributes", 0)
    if attributes & _WINDOWS_REPARSE_POINT:
        return "reparse point"
    return None


def _reject_symlink_path(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    for candidate in (*reversed(absolute.parents), absolute):
        link_kind = _link_kind(candidate, label)
        if link_kind is not None:
            raise RuntimeError(f"{label}: {link_kind} is forbidden: {candidate}")
    return absolute


def _reject_symlink_tree(root: Path, label: str) -> Path:
    absolute = _reject_symlink_path(root, label)
    if not absolute.is_dir():
        raise RuntimeError(f"{label}: directory is missing: {absolute}")
    pending = [absolute]
    while pending:
        current = pending.pop()
        try:
            entries = tuple(current.iterdir())
        except OSError as exc:
            raise RuntimeError(f"{label}: cannot read {current}: {exc}") from exc
        for entry in entries:
            try:
                link_kind = _link_kind(entry, label)
                if link_kind is not None:
                    raise RuntimeError(f"{label}: {link_kind} is forbidden: {entry}")
                if entry.is_dir():
                    pending.append(entry)
            except OSError as exc:
                raise RuntimeError(f"{label}: cannot inspect {entry}: {exc}") from exc
    return absolute


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


def _is_ignored_payload(rel: PurePath) -> bool:
    if any(part in _IGNORED_PAYLOAD_PARTS for part in rel.parts):
        return True
    return PurePosixPath(rel.as_posix()).suffix in _IGNORED_PAYLOAD_SUFFIXES


def payload_digest(plugin_root: Path) -> str:
    _reject_symlink_tree(plugin_root, "plugin payload")
    digest = hashlib.sha256()
    files = sorted(
        (path for path in plugin_root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(plugin_root).parts,
    )
    for path in files:
        rel = path.relative_to(plugin_root)
        if _is_ignored_payload(rel):
            continue
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
    claude_path = plugin_root / ".claude-plugin" / "plugin.json"
    payload_icon = plugin_root / "assets" / "icon.svg"
    source_icon = repo_root / "assets" / "icon.svg"
    base_version = project_version(repo_root)
    if not check:
        canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        canonical.pop("mcpServers", None)
        payload_icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_icon, payload_icon)
        canonical["version"] = base_version
        canonical_path.write_text(_canonical_json(canonical), encoding="utf-8")
        claude = json.loads(claude_path.read_text(encoding="utf-8"))
        claude["version"] = base_version
        claude_path.write_text(_canonical_json(claude), encoding="utf-8")
        # digest берётся после записи Claude-манифеста: он лежит внутри payload и,
        # в отличие от Codex-манифеста, его версия не нормализуется.
        canonical["version"] = expected_plugin_version(repo_root)
        canonical_path.write_text(_canonical_json(canonical), encoding="utf-8")
        project_path.write_text(
            _canonical_json(project_manifest_from(canonical)), encoding="utf-8"
        )
        return []

    errors: list[str] = []
    canonical = _checked_json(canonical_path, "canonical Codex manifest", errors)
    actual_project = _checked_json(project_path, "root Codex manifest", errors)
    claude = _checked_json(claude_path, "Claude manifest", errors)
    if claude is not None and claude.get("version") != base_version:
        errors.append(f"Claude manifest version {claude.get('version')!r} != {base_version!r}")
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


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def __call__(self, argv: tuple[str, ...]) -> CommandResult:
        raise NotImplementedError


@dataclass(frozen=True)
class CodexCapabilities:
    executable: Path


@dataclass(frozen=True)
class MarketplaceState:
    name: str
    root: Path
    source: str | None = None
    ref: str | None = None
    sparse_paths: tuple[str, ...] | None = None


@dataclass(frozen=True)
class MarketplaceMetadata:
    source_type: str | None
    source: str | None
    ref: str | None
    sparse_paths: tuple[str, ...] | None
    legacy_source: bool = False


@dataclass(frozen=True)
class SnapshotVerification:
    root: Path
    plugin_root: Path
    version: str
    skills: tuple[str, ...]


@dataclass(frozen=True)
class PluginState:
    name: str
    marketplace: str
    version: str
    installed: bool
    enabled: bool


@dataclass(frozen=True)
class CodexPluginState:
    executable: Path
    marketplace: MarketplaceState | None
    plugin: PluginState | None


@dataclass(frozen=True)
class CodexInstallOptions:
    dry_run: bool = False
    include_mcp: bool = True
    mcp_version: str = "latest"
    mcp_path: Path | None = None
    codex_home: Path | None = None


@dataclass(frozen=True)
class CodexPluginPlan:
    state: CodexPluginState
    options: CodexInstallOptions
    marketplace_action: Literal["add", "upgrade"]
    marketplace_argv: tuple[str, ...]
    plugin_argv: tuple[str, ...]


def subprocess_runner(
    argv: tuple[str, ...], *, env: dict[str, str] | None = None
) -> CommandResult:
    completed = subprocess.run(
        argv, capture_output=True, text=True, check=False, env=env
    )
    return CommandResult(argv, completed.returncode, completed.stdout, completed.stderr)


def find_codex_executable(
    which: Callable[[str], str | None] = shutil.which,
) -> Path:
    found = which("codex")
    if not found:
        raise RuntimeError("Codex CLI не найден; установите или обновите Codex")
    return Path(found).resolve()


def _require_help(runner: Runner, argv: tuple[str, ...], tokens: tuple[str, ...]) -> None:
    response = runner(argv)
    text = response.stdout + response.stderr
    missing = [token for token in tokens if token not in text]
    if response.returncode or missing:
        raise RuntimeError(f"Codex CLI не поддерживает {argv[2:]}: отсутствуют {missing}")


def detect_codex_capabilities(executable: Path, runner: Runner) -> CodexCapabilities:
    exe = str(executable)
    _require_help(runner, (exe, "plugin", "--help"), ("add", "marketplace", "list"))
    _require_help(
        runner,
        (exe, "plugin", "marketplace", "add", "--help"),
        ("--json", "--sparse", "--ref"),
    )
    _require_help(
        runner, (exe, "plugin", "marketplace", "upgrade", "--help"), ("--json",)
    )
    _require_help(runner, (exe, "plugin", "add", "--help"), ("--json",))
    _require_help(
        runner, (exe, "plugin", "list", "--help"), ("--json", "--available")
    )
    return CodexCapabilities(executable)


def _json_result(runner: Runner, argv: tuple[str, ...], phase: str) -> dict:
    response = runner(argv)
    if response.returncode:
        raise RuntimeError(f"{phase}: {response.stderr.strip()}")
    try:
        data = json.loads(response.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{phase}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{phase}: expected JSON object")
    return data


def _required_object_array(data: dict, field: str, phase: str) -> list[dict]:
    if field not in data:
        raise RuntimeError(f"{phase}: missing field {field}")
    value = data[field]
    if not isinstance(value, list):
        raise RuntimeError(f"{phase}: {field} must be an array")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise RuntimeError(f"{phase}: {field}[{index}] must be an object")
    return value


def _required_string(item: dict, field: str, label: str) -> str:
    if field not in item:
        raise RuntimeError(f"{label} missing field {field}")
    value = item[field]
    if not isinstance(value, str):
        raise RuntimeError(f"{label}.{field} must be a string")
    return value


def _required_boolean(item: dict, field: str, label: str) -> bool:
    if field not in item:
        raise RuntimeError(f"{label} missing field {field}")
    value = item[field]
    if not isinstance(value, bool):
        raise RuntimeError(f"{label}.{field} must be a boolean")
    return value


def _optional_string(item: dict, field: str, label: str) -> str | None:
    value = item.get(field)
    if value is not None and not isinstance(value, str):
        raise RuntimeError(f"{label}.{field} must be a string")
    return value


def _optional_string_tuple(item: dict, field: str, label: str) -> tuple[str, ...] | None:
    value = item.get(field)
    if value is None:
        return None
    if not isinstance(value, list):
        raise RuntimeError(f"{label}.{field} must be an array")
    for index, entry in enumerate(value):
        if not isinstance(entry, str):
            raise RuntimeError(f"{label}.{field}[{index}] must be a string")
    return tuple(value)


def _configured_marketplace_metadata(
    config_path: Path,
) -> MarketplaceMetadata | None:
    label = f"Codex config {config_path}"
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"{label}: {exc}") from exc
    marketplaces = data.get("marketplaces")
    if marketplaces is None:
        return None
    if not isinstance(marketplaces, dict):
        raise RuntimeError(f"{label}.marketplaces must be an object")
    configured = marketplaces.get(MARKETPLACE_NAME)
    if configured is None:
        return None
    if not isinstance(configured, dict):
        raise RuntimeError(
            f"{label}.marketplaces.{MARKETPLACE_NAME} must be an object"
        )
    source_type = _optional_string(configured, "source_type", label)
    source = _optional_string(configured, "source", label)
    ref = _optional_string(configured, "ref", label)
    sparse_paths = _optional_string_tuple(configured, "sparse_paths", label)
    if (
        source_type != "git"
        or not source
        or not source.strip()
        or not ref
        or not ref.strip()
        or not sparse_paths
        or any(not path.strip() for path in sparse_paths)
    ):
        raise RuntimeError(
            f"{label}.marketplaces.{MARKETPLACE_NAME} must be a complete git "
            "source/ref/sparse tuple"
        )
    if (
        _canonical_marketplace_source(source) != MARKETPLACE_GIT_SOURCE
        or ref != MARKETPLACE_REF
        or sparse_paths != MARKETPLACE_SPARSE
    ):
        raise RuntimeError(
            f"{label}.marketplaces.{MARKETPLACE_NAME} must be the canonical git "
            "source/ref/sparse tuple"
        )
    return MarketplaceMetadata(source_type, source, ref, sparse_paths)


def _marketplace_metadata(
    item: dict, label: str
) -> MarketplaceMetadata:
    if "marketplaceSource" not in item:
        return MarketplaceMetadata(
            None, _optional_string(item, "source", label), None, None, True
        )
    configured = item["marketplaceSource"]
    if not isinstance(configured, dict):
        raise RuntimeError(f"{label}.marketplaceSource must be an object")
    source_label = f"{label}.marketplaceSource"
    source_type = _optional_string(configured, "sourceType", source_label)
    source = _optional_string(configured, "source", source_label)
    ref = _optional_string(configured, "ref", source_label)
    sparse_paths = _optional_string_tuple(configured, "sparsePaths", source_label)
    return MarketplaceMetadata(source_type, source, ref, sparse_paths)


def _canonical_marketplace_source(source: str | None) -> str | None:
    if source == MARKETPLACE_SOURCE:
        return MARKETPLACE_GIT_SOURCE
    return source


def _merge_marketplace_metadata(
    public: MarketplaceMetadata,
    configured: MarketplaceMetadata | None,
    *,
    config_path: Path | None,
) -> tuple[str | None, str | None, tuple[str, ...] | None]:
    configured_source = None
    configured_ref = None
    configured_sparse_paths = None
    if configured is not None:
        if public.source is not None and configured.source is not None:
            if _canonical_marketplace_source(
                public.source
            ) != _canonical_marketplace_source(configured.source):
                raise RuntimeError(
                    f"Codex config {config_path} source conflicts with public "
                    "marketplace source"
                )
        if configured.source_type == "git":
            configured_source = configured.source
            configured_ref = configured.ref
            configured_sparse_paths = configured.sparse_paths

    if public.source_type != "git" and not public.legacy_source:
        source = None
    elif public.source is not None:
        source = public.source
    else:
        source = configured_source
    ref = public.ref if public.ref is not None else configured_ref
    sparse_paths = (
        public.sparse_paths
        if public.sparse_paths is not None
        else configured_sparse_paths
    )
    return source, ref, sparse_paths


def read_codex_state(
    executable: Path,
    runner: Runner,
    *,
    config_path: Path | None = None,
) -> CodexPluginState:
    exe = str(executable)
    marketplace_data = _json_result(
        runner, (exe, "plugin", "marketplace", "list", "--json"), "marketplace list"
    )
    marketplaces = _required_object_array(
        marketplace_data, "marketplaces", "marketplace list"
    )
    configured_metadata = (
        _configured_marketplace_metadata(config_path) if config_path is not None else None
    )
    plugin_data = _json_result(
        runner, (exe, "plugin", "list", "--available", "--json"), "plugin list"
    )
    installed = _required_object_array(plugin_data, "installed", "plugin list")
    marketplace: MarketplaceState | None = None
    for index, item in enumerate(marketplaces):
        label = f"marketplace list: marketplaces[{index}]"
        name = _required_string(item, "name", label)
        root = _required_string(item, "root", label)
        metadata = _marketplace_metadata(item, label)
        if name == MARKETPLACE_NAME and marketplace is None:
            if not root:
                raise RuntimeError("marketplace list: rag-reviewer root отсутствует")
            source, ref, sparse_paths = _merge_marketplace_metadata(
                metadata, configured_metadata, config_path=config_path
            )
            marketplace = MarketplaceState(
                MARKETPLACE_NAME, Path(root), source, ref, sparse_paths
            )
    plugin: PluginState | None = None
    for index, item in enumerate(installed):
        label = f"plugin list: installed[{index}]"
        name = _required_string(item, "name", label)
        marketplace_name = _required_string(item, "marketplaceName", label)
        version = _required_string(item, "version", label)
        is_installed = _required_boolean(item, "installed", label)
        enabled = _required_boolean(item, "enabled", label)
        if name == PLUGIN_NAME and plugin is None:
            plugin = PluginState(
                PLUGIN_NAME,
                marketplace_name,
                version,
                is_installed,
                enabled,
            )
    return CodexPluginState(executable, marketplace, plugin)


def _read_json(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label}: expected JSON object")
    return data


def _inside(root: Path, relative: str, label: str) -> Path:
    try:
        candidate = (root / relative).resolve()
        resolved_root = root.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"{label}: cannot resolve path: {exc}") from exc
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise RuntimeError(f"{label}: path leaves marketplace root")
    return candidate


def marketplace_is_owned(state: MarketplaceState) -> bool:
    source = _canonical_marketplace_source(state.source)
    return (
        state.name == MARKETPLACE_NAME
        and source == MARKETPLACE_GIT_SOURCE
        and state.ref == MARKETPLACE_REF
        and state.sparse_paths == MARKETPLACE_SPARSE
    )


def verify_marketplace_snapshot(root: Path, expected_base_version: str) -> SnapshotVerification:
    snapshot_root = _reject_symlink_path(root, "marketplace root")
    metadata_root = _reject_symlink_tree(snapshot_root / ".agents/plugins", "marketplace metadata")
    marketplace_path = metadata_root / "marketplace.json"
    marketplace = _read_json(marketplace_path, "marketplace")
    if marketplace.get("name") != MARKETPLACE_NAME:
        raise RuntimeError("marketplace name mismatch")
    entries = marketplace.get("plugins")
    if not isinstance(entries, list) or len(entries) != 1:
        raise RuntimeError("marketplace must contain exactly one plugin")
    entry = entries[0]
    if not isinstance(entry, dict):
        raise RuntimeError("marketplace plugin entry must be an object")
    if entry.get("name") != PLUGIN_NAME:
        raise RuntimeError("marketplace plugin name mismatch")
    source = entry.get("source")
    if source != {"source": "local", "path": "./plugin"}:
        raise RuntimeError("marketplace source must be relative ./plugin")
    _reject_symlink_tree(snapshot_root / "plugin", "plugin payload")
    plugin_root = _inside(snapshot_root, "plugin", "plugin source")
    manifest = _read_json(plugin_root / ".codex-plugin/plugin.json", "Codex manifest")
    if manifest.get("name") != PLUGIN_NAME:
        raise RuntimeError("plugin name mismatch")
    if "mcpServers" in manifest:
        raise RuntimeError("Codex manifest must not declare mcpServers")
    version = manifest.get("version")
    if not isinstance(version, str):
        raise RuntimeError("plugin version must be a string")
    skills_rel = manifest.get("skills")
    if skills_rel != "./skills/":
        raise RuntimeError("Codex manifest skills must be ./skills/")
    interface = manifest.get("interface")
    if not isinstance(interface, dict):
        raise RuntimeError("Codex manifest interface must be an object")
    icon_rel = interface.get("composerIcon")
    if icon_rel != "./assets/icon.svg":
        raise RuntimeError("Codex manifest composerIcon must be ./assets/icon.svg")
    prefix = f"{expected_base_version}+codex."
    if not version.startswith(prefix) or version.count("+codex.") != 1:
        raise RuntimeError(f"plugin version {version!r} does not match {prefix!r}")
    try:
        actual_digest = payload_digest(plugin_root)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"plugin payload: {exc}") from exc
    if version.removeprefix(prefix) != actual_digest:
        raise RuntimeError("plugin payload hash does not match manifest version")
    skills_root = _inside(plugin_root, skills_rel, "skills")
    if not skills_root.is_dir():
        raise RuntimeError("plugin skills directory is missing")
    try:
        skill_entries = tuple(skills_root.iterdir())
    except OSError as exc:
        raise RuntimeError(f"plugin skills directory cannot be read: {exc}") from exc
    skills = tuple(
        sorted(
            path.name for path in skill_entries if path.is_dir() and (path / "SKILL.md").is_file()
        )
    )
    if not skills:
        raise RuntimeError("plugin contains no registered skills")
    common = skills_root / "_common"
    if not common.is_dir() or (common / "SKILL.md").exists():
        raise RuntimeError("_common must be delivered but not registered as a skill")
    if not _inside(plugin_root, icon_rel, "icon").is_file():
        raise RuntimeError("declared composerIcon is missing")
    if not (plugin_root / "hooks/hooks.json").is_file():
        raise RuntimeError("plugin hooks payload is missing")
    return SnapshotVerification(root, plugin_root, version, skills)


def build_codex_plugin_plan(
    state: CodexPluginState, options: CodexInstallOptions
) -> CodexPluginPlan:
    if state.marketplace is not None and not marketplace_is_owned(state.marketplace):
        raise RuntimeError(
            f"marketplace {MARKETPLACE_NAME!r} уже связан с другим source/root "
            "или неполным source/ref/sparse: "
            f"source={state.marketplace.source!r}, ref={state.marketplace.ref!r}, "
            f"sparse={state.marketplace.sparse_paths!r}, root={state.marketplace.root}"
        )
    exe = str(state.executable)
    if state.marketplace is None:
        marketplace_action: Literal["add", "upgrade"] = "add"
        marketplace_argv = (
            exe,
            "plugin",
            "marketplace",
            "add",
            MARKETPLACE_SOURCE,
            "--ref",
            MARKETPLACE_REF,
            "--sparse",
            MARKETPLACE_SPARSE[0],
            "--sparse",
            MARKETPLACE_SPARSE[1],
            "--json",
        )
    else:
        marketplace_action = "upgrade"
        marketplace_argv = (
            exe,
            "plugin",
            "marketplace",
            "upgrade",
            MARKETPLACE_NAME,
            "--json",
        )
    plugin_argv = (
        exe,
        "plugin",
        "add",
        f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
        "--json",
    )
    return CodexPluginPlan(
        state, options, marketplace_action, marketplace_argv, plugin_argv
    )


class CodexInstallError(RuntimeError):
    def __init__(self, phase: str, argv: tuple[str, ...], detail: str):
        self.phase = phase
        self.argv = argv
        self.detail = detail
        rendered_argv = " ".join(argv) if argv else "<none>"
        super().__init__(f"{phase}: {detail}; argv={rendered_argv}")


@dataclass(frozen=True)
class ConfigSnapshot:
    path: Path
    existed: bool
    content: bytes
    backup_path: Path


@dataclass(frozen=True)
class LegacyMigrationResult:
    backup_root: Path | None
    moved: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class LegacySkillCandidate:
    name: str
    source: Path
    reason: str


def _directory_files(root: Path) -> dict[str, bytes]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _stamp_owned(skills_root: Path, name: str, source: Path) -> bool:
    stamp_path = skills_root / ".reviewer-skills.json"
    try:
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(stamp, dict):
        return False
    source_url = stamp.get("source_url")
    skills = stamp.get("skills")
    if not isinstance(source_url, str) or not isinstance(skills, dict):
        return False
    expected_hash = skills.get(name)
    if "mimfort/rag_for_git" not in source_url or not isinstance(expected_hash, str):
        return False
    digest = hashlib.sha256()
    for relative, content in _directory_files(source).items():
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(content)
    return expected_hash == "sha256:" + digest.hexdigest()


def find_owned_legacy_skills(
    skills_root: Path, plugin_root: Path
) -> tuple[LegacySkillCandidate, ...]:
    payload_skills = plugin_root / "skills"
    candidates: list[LegacySkillCandidate] = []
    if not skills_root.is_dir():
        return ()
    for source in sorted(path for path in skills_root.iterdir() if path.is_dir()):
        expected = payload_skills / source.name
        if not expected.is_dir():
            continue
        if _stamp_owned(skills_root, source.name, source):
            candidates.append(
                LegacySkillCandidate(source.name, source, "valid installer stamp")
            )
        elif _directory_files(source) == _directory_files(expected):
            candidates.append(
                LegacySkillCandidate(source.name, source, "exact payload match")
            )
    return tuple(candidates)


def migrate_legacy_skills(skills_root: Path, plugin_root: Path) -> LegacyMigrationResult:
    candidates = find_owned_legacy_skills(skills_root, plugin_root)
    payload_names = {
        path.name for path in (plugin_root / "skills").iterdir() if path.is_dir()
    }
    candidate_names = {item.name for item in candidates}
    ambiguous = (
        tuple(
            sorted(
                path.name
                for path in skills_root.iterdir()
                if path.is_dir()
                and path.name in payload_names
                and path.name not in candidate_names
            )
        )
        if skills_root.is_dir()
        else ()
    )
    warnings = [
        f"legacy skill {name!r} изменён/неполон — оставлен на месте"
        for name in ambiguous
    ]
    if not candidates:
        return LegacyMigrationResult(None, (), tuple(warnings))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = skills_root.parent / "reviewer-legacy-backups" / stamp
    moved: list[tuple[Path, Path]] = []
    try:
        for item in candidates:
            target = backup_root / "skills" / item.name
            target.parent.mkdir(parents=True, exist_ok=True)
            item.source.replace(target)
            moved.append((item.source, target))
    except OSError as exc:
        for source, target in reversed(moved):
            source.parent.mkdir(parents=True, exist_ok=True)
            target.replace(source)
        warnings.append(f"legacy migration отменена и восстановлена: {exc}")
        return LegacyMigrationResult(None, (), tuple(warnings))
    return LegacyMigrationResult(
        backup_root,
        tuple(source.name for source, target in moved),
        tuple(warnings),
    )


@dataclass(frozen=True)
class CodexInstallResult:
    plan: CodexPluginPlan
    verification: SnapshotVerification | None
    config_backup: Path | None
    migration: LegacyMigrationResult
    warnings: tuple[str, ...]
    mcp_preview: str | None = None
    config_backups: tuple[Path, ...] = ()


def _checked(runner: Runner, argv: tuple[str, ...], phase: str) -> dict:
    response = runner(argv)
    if response.returncode:
        raise CodexInstallError(phase, argv, response.stderr.strip())
    try:
        data: Any = json.loads(response.stdout)
    except json.JSONDecodeError as exc:
        raise CodexInstallError(phase, argv, f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CodexInstallError(phase, argv, "expected JSON object")
    return data


def _snapshot_config(path: Path) -> ConfigSnapshot:
    existed = path.exists()
    content = path.read_bytes() if existed else b""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        backup = path.with_name(
            path.name + f".rag-reviewer.{stamp}.{uuid4().hex}.bak"
        )
        try:
            with backup.open("xb") as backup_file:
                backup_file.write(content)
        except FileExistsError:
            continue
        break
    try:
        backup_content = backup.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"config backup cannot be verified: {backup}") from exc
    if backup_content != content:
        raise RuntimeError(f"config backup bytes do not match: {backup}")
    return ConfigSnapshot(path, existed, content, backup)


def _restore_config(snapshot: ConfigSnapshot) -> None:
    if snapshot.existed:
        snapshot.path.parent.mkdir(parents=True, exist_ok=True)
        snapshot.path.write_bytes(snapshot.content)
    else:
        try:
            snapshot.path.lstat()
        except FileNotFoundError:
            return
        snapshot.path.unlink()


def _restore_configs(snapshots: tuple[ConfigSnapshot, ...]) -> None:
    for snapshot in reversed(snapshots):
        _restore_config(snapshot)


def _verify_restored_configs(snapshots: tuple[ConfigSnapshot, ...]) -> None:
    for snapshot in snapshots:
        if snapshot.existed:
            try:
                actual = snapshot.path.read_bytes()
            except OSError as exc:
                raise RuntimeError(
                    f"config rollback cannot read {snapshot.path}"
                ) from exc
            if actual != snapshot.content:
                raise RuntimeError(
                    f"config rollback bytes do not match {snapshot.path}"
                )
            continue
        try:
            snapshot.path.lstat()
        except FileNotFoundError:
            continue
        raise RuntimeError(f"config rollback must remove {snapshot.path}")


def _backup_paths(snapshots: tuple[ConfigSnapshot, ...]) -> str:
    return ", ".join(str(snapshot.backup_path) for snapshot in snapshots)


def _codex_home(options: CodexInstallOptions) -> Path:
    if options.codex_home is not None:
        return Path(os.path.abspath(options.codex_home.expanduser()))
    configured = os.environ.get("CODEX_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return Path(os.path.abspath(home))


def _mcp_config_path(options: CodexInstallOptions, effective_config: Path) -> Path:
    if not options.include_mcp or options.mcp_path is None:
        return effective_config
    return Path(os.path.abspath(options.mcp_path.expanduser()))


def _runner_for_codex_home(runner: Runner, codex_home: Path) -> Runner:
    if runner is not subprocess_runner:
        return runner
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(codex_home)

    def effective_runner(argv: tuple[str, ...]) -> CommandResult:
        return subprocess_runner(argv, env=environment)

    return effective_runner


def _verify_mcp_config(
    generic: Any, config_path: Path, version: str
) -> None:
    try:
        final_plan = generic.build_plan(
            generic.CLIENTS["codex"], version=version, path_override=str(config_path)
        )
        actual = tomllib.loads(final_plan.path.read_text(encoding="utf-8"))
        expected = tomllib.loads(final_plan.content)
    except (OSError, UnicodeError, ValueError) as exc:
        raise CodexInstallError("MCP config verification", (), str(exc)) from exc
    actual_servers = actual.get("mcp_servers")
    expected_servers = expected.get("mcp_servers")
    actual_reviewer = (
        actual_servers.get("reviewer") if isinstance(actual_servers, dict) else None
    )
    expected_reviewer = (
        expected_servers.get("reviewer") if isinstance(expected_servers, dict) else None
    )
    matches_canonical_entry = (
        isinstance(actual_reviewer, dict)
        and isinstance(expected_reviewer, dict)
        and all(
            key in actual_reviewer
            and key in expected_reviewer
            and type(actual_reviewer[key]) is type(expected_reviewer[key])
            and actual_reviewer[key] == expected_reviewer[key]
            for key in ("command", "args")
        )
        and actual_reviewer.get("enabled", True) is True
    )
    if not matches_canonical_entry:
        raise CodexInstallError(
            "MCP config verification",
            (),
            "reviewer MCP table is missing or differs from canonical configuration",
        )


def _verified_installed(state: CodexPluginState, expected_version: str) -> None:
    plugin = state.plugin
    if plugin is None:
        raise RuntimeError("plugin list: rag-reviewer не установлен")
    if plugin.marketplace != MARKETPLACE_NAME:
        raise RuntimeError(f"plugin установлен из {plugin.marketplace!r}")
    if not plugin.installed or not plugin.enabled:
        raise RuntimeError("plugin должен быть installed и enabled")
    if plugin.version != expected_version:
        raise RuntimeError(
            f"installed version {plugin.version!r} != {expected_version!r}"
        )


def _verify_marketplace_ownership(
    state: CodexPluginState, argv: tuple[str, ...]
) -> None:
    if state.marketplace is None or not marketplace_is_owned(state.marketplace):
        raise CodexInstallError(
            "marketplace ownership verification",
            argv,
            "marketplace rag-reviewer после mutation имеет неполные или чужие "
            "source/ref/sparse metadata",
        )


def run_codex_install(
    options: CodexInstallOptions,
    *,
    runner: Runner = subprocess_runner,
    which: Callable[[str], str | None] = shutil.which,
    legacy_migrator: Callable[[Path, Path], LegacyMigrationResult] | None = None,
) -> CodexInstallResult:
    from reviewer import install as generic

    codex_home = _codex_home(options)
    effective_config = codex_home / "config.toml"
    effective_runner = _runner_for_codex_home(runner, codex_home)
    executable = find_codex_executable(which)
    detect_codex_capabilities(executable, effective_runner)
    state = read_codex_state(executable, effective_runner, config_path=effective_config)
    plan = build_codex_plugin_plan(state, options)
    empty_migration = LegacyMigrationResult(None, (), ())
    mcp_config = _mcp_config_path(options, effective_config)
    mcp_preview = None
    if options.include_mcp:
        mcp_preview = generic.build_plan(
            generic.CLIENTS["codex"],
            version=options.mcp_version,
            path_override=str(mcp_config),
        ).content
    if options.dry_run:
        return CodexInstallResult(plan, None, None, empty_migration, (), mcp_preview)

    snapshot_paths = [effective_config]
    if options.include_mcp and mcp_config != effective_config:
        snapshot_paths.append(mcp_config)
    snapshots = tuple(_snapshot_config(path) for path in snapshot_paths)
    try:
        _checked(
            effective_runner,
            plan.marketplace_argv,
            f"marketplace {plan.marketplace_action}",
        )
        try:
            refreshed = read_codex_state(
                executable, effective_runner, config_path=effective_config
            )
        except Exception as exc:
            raise CodexInstallError(
                "marketplace ownership verification", plan.marketplace_argv, str(exc)
            ) from exc
        _verify_marketplace_ownership(refreshed, plan.marketplace_argv)
        try:
            verification = verify_marketplace_snapshot(
                refreshed.marketplace.root, generic.current_pkg_version()
            )
        except RuntimeError as exc:
            raise CodexInstallError(
                "snapshot verification", plan.marketplace_argv, str(exc)
            ) from exc
        if options.include_mcp:
            try:
                mcp_plan = generic.build_plan(
                    generic.CLIENTS["codex"],
                    version=options.mcp_version,
                    path_override=str(mcp_config),
                )
                generic.apply_plan(mcp_plan)
            except (OSError, UnicodeError, ValueError) as exc:
                raise CodexInstallError("MCP config update", (), str(exc)) from exc
        _checked(effective_runner, plan.plugin_argv, "plugin add")
        try:
            installed = read_codex_state(
                executable, effective_runner, config_path=effective_config
            )
        except Exception as exc:
            raise CodexInstallError(
                "marketplace ownership verification", plan.plugin_argv, str(exc)
            ) from exc
        _verify_marketplace_ownership(installed, plan.plugin_argv)
        try:
            _verified_installed(installed, verification.version)
        except RuntimeError as exc:
            raise CodexInstallError(
                "post-install verification",
                (str(executable), "plugin", "list", "--available", "--json"),
                str(exc),
            ) from exc
        if options.include_mcp:
            _verify_mcp_config(generic, mcp_config, options.mcp_version)
        migrator = legacy_migrator or migrate_legacy_skills
        migration = migrator(codex_home / "skills", verification.plugin_root)
        return CodexInstallResult(
            plan,
            verification,
            snapshots[0].backup_path,
            migration,
            migration.warnings,
            mcp_preview,
            tuple(snapshot.backup_path for snapshot in snapshots),
        )
    except Exception:
        try:
            _restore_configs(snapshots)
            _verify_restored_configs(snapshots)
            rolled_back = read_codex_state(
                executable, effective_runner, config_path=effective_config
            )
            if rolled_back.plugin != state.plugin:
                raise RuntimeError(
                    "config rollback не восстановил предыдущую plugin selection; "
                    f"backups: {_backup_paths(snapshots)}"
                )
        except Exception as restore_error:
            raise RuntimeError(
                "config rollback failed; "
                f"backups: {_backup_paths(snapshots)}: {restore_error}"
            ) from restore_error
        raise
