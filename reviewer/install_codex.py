from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol

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


def subprocess_runner(argv: tuple[str, ...]) -> CommandResult:
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
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


def read_codex_state(executable: Path, runner: Runner) -> CodexPluginState:
    exe = str(executable)
    marketplace_data = _json_result(
        runner, (exe, "plugin", "marketplace", "list", "--json"), "marketplace list"
    )
    marketplaces = _required_object_array(
        marketplace_data, "marketplaces", "marketplace list"
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
        source = item.get("source")
        if source is not None and not isinstance(source, str):
            raise RuntimeError(f"{label}.source must be a string")
        if name == MARKETPLACE_NAME and marketplace is None:
            if not root:
                raise RuntimeError("marketplace list: rag-reviewer root отсутствует")
            marketplace = MarketplaceState(MARKETPLACE_NAME, Path(root), source)
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


def build_codex_plugin_plan(
    state: CodexPluginState, options: CodexInstallOptions
) -> CodexPluginPlan:
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
