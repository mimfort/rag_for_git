# PRI-210 Codex Plugin Auto-Install Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `reviewer install codex` install and safely update one global reviewer MCP plus a namespaced Codex plugin from a portable Git marketplace on Windows, macOS, and Linux.

**Architecture:** Keep generic MCP/file-skill logic in `reviewer/install.py` and introduce `reviewer/install_codex.py` for Codex capability detection, marketplace planning, snapshot verification, transaction/rollback, and legacy migration. Package a compact shared `plugin/` payload with a Codex manifest that never registers MCP, and drive all Codex entrypoints through one orchestration function.

**Tech Stack:** Python 3.11–3.13 stdlib (`dataclasses`, `hashlib`, `json`, `pathlib`, `subprocess`, `tomllib`), Click 8.1+, pytest 8.2+, Codex CLI public `plugin <subcommand> --json` commands, GitHub Actions.

## Global Constraints

- Python support remains `>=3.11,<3.14`; add no runtime dependency.
- Never use `shell=True`, `bash`, `curl`, symlinks, or machine-specific paths in the Codex flow.
- Codex marketplace follows Git `main`; this task does not introduce tags or a release pipeline.
- Marketplace source is `mimfort/rag_for_git`, sparse paths are `.agents/plugins` and `plugin`.
- Codex manifest version is `<pyproject-version>+codex.<12-hex-payload-hash>` with exactly one `+codex.` token.
- The Codex manifest must not declare `mcpServers`; global `reviewer install codex` owns the only reviewer MCP.
- `--dry-run` may run local read-only discovery but must not perform network or mutating Codex commands.
- Preserve non-reviewer TOML byte-for-byte and create a recoverable config backup before mutation.
- Migrate legacy skills only after plugin/MCP verification and only by valid stamp or exact payload match.
- Finish UX says New Chat/new CLI session; IDE users also get Reload Window.
- Preserve unrelated working-tree changes and commit without self-attribution.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `reviewer/install_codex.py` | Create | Codex state, planning, snapshot verification, execution, rollback, legacy migration, release hash helpers |
| `reviewer/install.py` | Modify | Remove stale hardcoded inventory; safely replace Codex MCP TOML tables |
| `reviewer/entrypoints/cli.py` | Modify | Route `install`, `install-skills`, `install --all`, and interactive `init` into the canonical Codex flow |
| `scripts/update_codex_plugin_manifest.py` | Create | Update/check deterministic Codex manifest versions and root manifest projection |
| `.agents/plugins/marketplace.json` | Create | Portable Codex marketplace pointing to relative `./plugin` |
| `plugin/.codex-plugin/plugin.json` | Create | Canonical compact Codex plugin manifest without MCP |
| `.codex-plugin/plugin.json` | Modify | Project-level projection of canonical manifest without MCP |
| `plugin/assets/icon.svg` | Create | Payload-local copy of canonical `assets/icon.svg` |
| `tests/install/test_codex_plugin_payload.py` | Create | Marketplace, manifest transform, payload hash, inventory guards |
| `tests/install/test_codex_install.py` | Create | Discovery, planning, verification, transaction, rollback, legacy unit tests |
| `tests/install/fake_codex.py` | Create | Stateful argv/JSON fake for integration tests |
| `tests/install/test_codex_cli.py` | Create | Click entrypoint and end-to-end fake Codex tests |
| `tests/install/test_install.py` | Modify | Codex TOML update and path-preservation tests |
| `tests/install/test_common_shared_dir.py` | Modify | Remove hardcoded `SKILL_NAMES` assertion; retain dynamic `_common` coverage |
| `.github/workflows/codex-plugin.yml` | Create | Three-OS focused tests and payload guard |
| `README.md` | Modify | Canonical English Codex install/update/diagnostics docs |
| `README.ru.md` | Modify | Canonical Russian Codex install/update/diagnostics docs |
| `AGENTS.md` | Modify | Replace manual global skill extraction with one installer command |
| `plugin/README.md` | Modify | Shared Claude/Codex payload and dynamic skill inventory docs |

---

### Task 1: Compact Codex marketplace and deterministic release identity

**Files:**
- Create: `reviewer/install_codex.py`
- Create: `scripts/update_codex_plugin_manifest.py`
- Create: `.agents/plugins/marketplace.json`
- Create: `plugin/.codex-plugin/plugin.json`
- Create: `plugin/assets/icon.svg`
- Modify: `.codex-plugin/plugin.json`
- Create: `tests/install/test_codex_plugin_payload.py`

**Interfaces:**
- Consumes: repository root containing `pyproject.toml`, `assets/icon.svg`, and `plugin/skills/`.
- Produces:
  - `project_version(repo_root: Path) -> str`
  - `payload_digest(plugin_root: Path) -> str`
  - `expected_plugin_version(repo_root: Path) -> str`
  - `project_manifest_from(canonical: dict) -> dict`
  - `sync_plugin_metadata(repo_root: Path, *, check: bool) -> list[str]`

- [ ] **Step 1: Write payload guard tests**

Create `tests/install/test_codex_plugin_payload.py`:

```python
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
```

- [ ] **Step 2: Run the tests and verify the missing module failure**

Run:

```bash
pytest tests/install/test_codex_plugin_payload.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'reviewer.install_codex'`.

- [ ] **Step 3: Add deterministic payload helpers**

Create `reviewer/install_codex.py` with this initial content:

```python
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
        digest.update(rel.as_posix().encode())
        digest.update(b"\0")
        digest.update(_payload_bytes(path, plugin_root))
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


def sync_plugin_metadata(repo_root: Path, *, check: bool) -> list[str]:
    plugin_root = repo_root / "plugin"
    canonical_path = plugin_root / ".codex-plugin" / "plugin.json"
    project_path = repo_root / ".codex-plugin" / "plugin.json"
    payload_icon = plugin_root / "assets" / "icon.svg"
    source_icon = repo_root / "assets" / "icon.svg"
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    canonical.pop("mcpServers", None)
    payload_icon.parent.mkdir(parents=True, exist_ok=True)
    if not check:
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
    expected = expected_plugin_version(repo_root)
    if canonical.get("version") != expected:
        errors.append(f"manifest version {canonical.get('version')!r} != {expected!r}")
    if "mcpServers" in canonical:
        errors.append("Codex manifest must not declare mcpServers")
    if payload_icon.read_bytes() != source_icon.read_bytes():
        errors.append("plugin/assets/icon.svg differs from assets/icon.svg")
    projected = project_manifest_from(canonical)
    actual_project = json.loads(project_path.read_text(encoding="utf-8"))
    if actual_project != projected:
        errors.append("root Codex manifest is not the canonical path projection")
    return errors
```

- [ ] **Step 4: Add the marketplace and canonical manifest**

Create `.agents/plugins/marketplace.json`:

```json
{
  "name": "rag-reviewer",
  "interface": {"displayName": "RAG Reviewer"},
  "plugins": [
    {
      "name": "rag-reviewer",
      "source": {"source": "local", "path": "./plugin"},
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
        "products": ["CODEX"]
      },
      "category": "Productivity"
    }
  ]
}
```

Create `plugin/.codex-plugin/plugin.json`:

```json
{
  "name": "rag-reviewer",
  "version": "0.1.0",
  "description": "Agentic PR review: hybrid RAG + code graph via MCP, review skills for Codex",
  "repository": "https://github.com/mimfort/rag_for_git",
  "license": "MIT",
  "author": {"name": "mimfort"},
  "homepage": "https://github.com/mimfort/rag_for_git#readme",
  "keywords": ["code-review", "rag", "code-graph", "mcp", "pr-review"],
  "skills": "./skills/",
  "interface": {
    "displayName": "RAG Reviewer",
    "shortDescription": "RAG + code graph PR review pipeline",
    "longDescription": "Agentic pull request review using hybrid RAG search, code graph analysis, and MCP tools.",
    "developerName": "mimfort",
    "category": "Productivity",
    "capabilities": ["Interactive", "Write"],
    "defaultPrompt": [
      "Review PR 123 for owner/repo",
      "Solve task PRI-4",
      "Sync tasks from the board"
    ],
    "composerIcon": "./assets/icon.svg",
    "brandColor": "#3B82F6"
  }
}
```

The seed version is deliberately the current exact manifest value. Step 5 replaces it
deterministically before the task is considered complete.

- [ ] **Step 5: Add the update/check script and generate synchronized files**

Create `scripts/update_codex_plugin_manifest.py`:

```python
from __future__ import annotations

import argparse
from pathlib import Path

from reviewer.install_codex import sync_plugin_metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    errors = sync_plugin_metadata(root, check=args.check)
    if errors:
        for error in errors:
            print(error)
        return 1
    if not args.check:
        print("Codex plugin manifests synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Run:

```bash
python scripts/update_codex_plugin_manifest.py
python scripts/update_codex_plugin_manifest.py --check
```

Expected: both commands exit 0; the first creates `plugin/assets/icon.svg`, assigns a
`0.2.27+codex.<12-hex>` version, and rewrites the root manifest without `mcpServers`.

- [ ] **Step 6: Run focused tests**

Run:

```bash
pytest tests/install/test_codex_plugin_payload.py -v
ruff check reviewer/install_codex.py scripts/update_codex_plugin_manifest.py tests/install/test_codex_plugin_payload.py
```

Expected: all tests pass and Ruff reports no errors.

- [ ] **Step 7: Commit the payload foundation**

```bash
git add reviewer/install_codex.py scripts/update_codex_plugin_manifest.py \
  .agents/plugins/marketplace.json plugin/.codex-plugin/plugin.json \
  plugin/assets/icon.svg .codex-plugin/plugin.json \
  tests/install/test_codex_plugin_payload.py
git commit -m "feat(install): добавить Codex marketplace и release guard"
```

---

### Task 2: Codex CLI capability discovery and pure planning

**Files:**
- Modify: `reviewer/install_codex.py`
- Create: `tests/install/test_codex_install.py`

**Interfaces:**
- Consumes: constants and payload helpers from Task 1.
- Produces:
  - `CommandResult(argv, returncode, stdout, stderr)`
  - `Runner` protocol
  - `CodexCapabilities`, `MarketplaceState`, `PluginState`, `CodexPluginState`
  - `CodexInstallOptions`, `CodexPluginPlan`
  - `subprocess_runner(argv: tuple[str, ...]) -> CommandResult`
  - `find_codex_executable(which=shutil.which) -> Path`
  - `detect_codex_capabilities(executable: Path, runner: Runner) -> CodexCapabilities`
  - `read_codex_state(executable: Path, runner: Runner) -> CodexPluginState`
  - `build_codex_plugin_plan(state, options) -> CodexPluginPlan`

- [ ] **Step 1: Write discovery and planning tests**

Append to `tests/install/test_codex_install.py`:

```python
import json
from pathlib import Path

import pytest

from reviewer.install_codex import (
    CodexInstallOptions,
    CodexPluginState,
    CommandResult,
    MarketplaceState,
    PluginState,
    build_codex_plugin_plan,
    detect_codex_capabilities,
    find_codex_executable,
    read_codex_state,
)


class MappingRunner:
    def __init__(self, responses: dict[tuple[str, ...], CommandResult]):
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...]) -> CommandResult:
        self.calls.append(argv)
        return self.responses[argv]


def result(argv: tuple[str, ...], stdout: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(argv, returncode, stdout, "failure" if returncode else "")


def test_find_codex_requires_an_absolute_executable(tmp_path):
    executable = (tmp_path / "Codex Dir" / "codex").resolve()
    assert find_codex_executable(lambda name: str(executable)) == executable
    with pytest.raises(RuntimeError, match="Codex CLI не найден"):
        find_codex_executable(lambda name: None)


def test_capability_detection_is_feature_based(tmp_path):
    exe = tmp_path / "codex"
    commands = {
        (str(exe), "plugin", "--help"): result(
            (str(exe), "plugin", "--help"), "Commands: add marketplace list"
        ),
        (str(exe), "plugin", "marketplace", "add", "--help"): result(
            (str(exe), "plugin", "marketplace", "add", "--help"), "--json --sparse --ref"
        ),
        (str(exe), "plugin", "marketplace", "upgrade", "--help"): result(
            (str(exe), "plugin", "marketplace", "upgrade", "--help"), "--json"
        ),
        (str(exe), "plugin", "add", "--help"): result(
            (str(exe), "plugin", "add", "--help"), "--json"
        ),
        (str(exe), "plugin", "list", "--help"): result(
            (str(exe), "plugin", "list", "--help"), "--json --available"
        ),
    }
    capabilities = detect_codex_capabilities(exe, MappingRunner(commands))
    assert capabilities.executable == exe


def test_old_codex_without_plugin_marketplace_is_actionable(tmp_path):
    exe = tmp_path / "codex"
    argv = (str(exe), "plugin", "--help")
    runner = MappingRunner({argv: result(argv, "Commands: list")})
    with pytest.raises(RuntimeError, match="не поддерживает"):
        detect_codex_capabilities(exe, runner)


def test_read_state_accepts_extra_json_fields(tmp_path):
    exe = tmp_path / "codex"
    marketplace_argv = (str(exe), "plugin", "marketplace", "list", "--json")
    plugin_argv = (str(exe), "plugin", "list", "--available", "--json")
    runner = MappingRunner({
        marketplace_argv: result(marketplace_argv, json.dumps({
            "marketplaces": [{"name": "rag-reviewer", "root": str(tmp_path), "new": 1}]
        })),
        plugin_argv: result(plugin_argv, json.dumps({
            "installed": [{
                "name": "rag-reviewer", "marketplaceName": "rag-reviewer",
                "version": "0.2.27+codex.123456789abc", "installed": True,
                "enabled": True, "extra": "ignored"
            }],
            "available": []
        })),
    })
    state = read_codex_state(exe, runner)
    assert state.marketplace is not None and state.marketplace.root == tmp_path
    assert state.plugin is not None and state.plugin.enabled is True


def test_plan_chooses_add_for_fresh_and_upgrade_for_owned_marketplace(tmp_path):
    exe = tmp_path / "codex"
    fresh = read_codex_state(exe, MappingRunner({
        (str(exe), "plugin", "marketplace", "list", "--json"): result(
            (str(exe), "plugin", "marketplace", "list", "--json"), '{"marketplaces": []}'
        ),
        (str(exe), "plugin", "list", "--available", "--json"): result(
            (str(exe), "plugin", "list", "--available", "--json"),
            '{"installed": [], "available": []}'
        ),
    }))
    fresh_plan = build_codex_plugin_plan(fresh, CodexInstallOptions())
    assert fresh_plan.marketplace_action == "add"
    assert "--sparse" in fresh_plan.marketplace_argv
    owned = CodexPluginState(exe, MarketplaceState("rag-reviewer", tmp_path,
                                                    "mimfort/rag_for_git"), None)
    assert build_codex_plugin_plan(owned, CodexInstallOptions()).marketplace_action == "upgrade"
```

- [ ] **Step 2: Run tests and verify missing symbols**

Run:

```bash
pytest tests/install/test_codex_install.py -v
```

Expected: collection fails because `CommandResult` and the state types are not defined.

- [ ] **Step 3: Add state, runner, and capability types**

Extend the import block in `reviewer/install_codex.py` with `import subprocess`,
`from dataclasses import dataclass`, and `from typing import Callable, Literal, Protocol`. Then append:

```python
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
```

- [ ] **Step 4: Add strict-required/tolerant-extra JSON discovery and plan building**

Append:

```python
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


def read_codex_state(executable: Path, runner: Runner) -> CodexPluginState:
    exe = str(executable)
    marketplaces = _json_result(
        runner, (exe, "plugin", "marketplace", "list", "--json"), "marketplace list"
    ).get("marketplaces", [])
    installed = _json_result(
        runner, (exe, "plugin", "list", "--available", "--json"), "plugin list"
    ).get("installed", [])
    marketplace: MarketplaceState | None = None
    for item in marketplaces:
        if item.get("name") == MARKETPLACE_NAME:
            if not item.get("root"):
                raise RuntimeError("marketplace list: rag-reviewer root отсутствует")
            marketplace = MarketplaceState(
                MARKETPLACE_NAME, Path(item["root"]), item.get("source")
            )
            break
    plugin: PluginState | None = None
    for item in installed:
        if item.get("name") == PLUGIN_NAME:
            required = ("marketplaceName", "version", "installed", "enabled")
            missing = [key for key in required if key not in item]
            if missing:
                raise RuntimeError(f"plugin list: missing fields {missing}")
            plugin = PluginState(
                PLUGIN_NAME,
                str(item["marketplaceName"]),
                str(item["version"]),
                bool(item["installed"]),
                bool(item["enabled"]),
            )
            break
    return CodexPluginState(executable, marketplace, plugin)


def build_codex_plugin_plan(
    state: CodexPluginState, options: CodexInstallOptions
) -> CodexPluginPlan:
    exe = str(state.executable)
    if state.marketplace is None:
        marketplace_action: Literal["add", "upgrade"] = "add"
        marketplace_argv = (
            exe, "plugin", "marketplace", "add", MARKETPLACE_SOURCE,
            "--ref", MARKETPLACE_REF,
            "--sparse", MARKETPLACE_SPARSE[0],
            "--sparse", MARKETPLACE_SPARSE[1],
            "--json",
        )
    else:
        marketplace_action = "upgrade"
        marketplace_argv = (
            exe, "plugin", "marketplace", "upgrade", MARKETPLACE_NAME, "--json"
        )
    plugin_argv = (exe, "plugin", "add", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}", "--json")
    return CodexPluginPlan(state, options, marketplace_action, marketplace_argv, plugin_argv)
```

- [ ] **Step 5: Run focused tests and lint**

Run:

```bash
pytest tests/install/test_codex_install.py -v
ruff check reviewer/install_codex.py tests/install/test_codex_install.py
```

Expected: all discovery/planning tests pass; Ruff reports no errors.

- [ ] **Step 6: Commit discovery and planning**

```bash
git add reviewer/install_codex.py tests/install/test_codex_install.py
git commit -m "feat(install): спланировать lifecycle Codex plugin"
```

---

### Task 3: Safe in-place update of the reviewer MCP TOML tables

**Files:**
- Modify: `reviewer/install.py:1-20,423-482`
- Modify: `tests/install/test_install.py:79-110`

**Interfaces:**
- Consumes: existing `_render_codex(command, args)` and `build_plan` inputs.
- Produces:
  - `codex_home_path() -> Path`
  - `_toml_table_name(line: str) -> str | None`
  - `_replace_codex_reviewer_tables(raw: str, rendered: str) -> tuple[str, bool]`
  - Updated `build_plan(client: Client, *, system: str | None = None, version: str = "latest", path_override: str | None = None) -> InstallPlan` that replaces stale command/args.

- [ ] **Step 1: Replace the old append-only assertions with failing update tests**

Add `import tomllib` to `tests/install/test_install.py` and replace
`test_plan_codex_idempotent` with:

```python
def test_plan_codex_updates_existing_reviewer_and_preserves_other_toml(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    original_other = "# keep\n[other]\npath = 'C:\\\\Program Files\\\\Tool'\n\n"
    cfg.write_text(
        original_other
        + "[mcp_servers.reviewer]\ncommand = \"old\"\nargs = [\"old\"]\n"
        + "[mcp_servers.reviewer.env]\nOLD = \"1\"\n"
        + "[tail]\nvalue = 3\n"
    )
    plan = inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))
    assert plan.already is True
    assert plan.content.startswith(original_other)
    assert plan.content.count("[mcp_servers.reviewer]") == 1
    assert "command = \"/fake/bin/uvx\"" in plan.content
    assert "[mcp_servers.reviewer.env]" not in plan.content
    assert "[tail]\nvalue = 3\n" in plan.content
    assert tomllib.loads(plan.content)["mcp_servers"]["reviewer"]["command"] == "/fake/bin/uvx"


def test_plan_codex_rejects_inline_reviewer_table(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('mcp_servers = { reviewer = { command = "old" } }\n')
    with pytest.raises(ValueError, match="inline"):
        inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))


def test_plan_codex_rejects_invalid_toml(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[broken\n")
    with pytest.raises(ValueError, match="невалидный TOML"):
        inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))


def test_codex_client_path_honors_codex_home(monkeypatch, tmp_path):
    codex_home = tmp_path / "Codex Home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert inst.CLIENTS["codex"].path_fn("Windows") == codex_home / "config.toml"


def test_launch_command_requires_uvx_or_uv(monkeypatch):
    monkeypatch.setattr(inst.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="uvx/uv не найден"):
        inst.launch_command("latest")
```

- [ ] **Step 2: Run the three tests and verify failures**

Run:

```bash
pytest tests/install/test_install.py -k 'plan_codex' -v
```

Expected: stale command remains, inline form is not rejected with the expected message, and invalid
TOML raises a raw parser error.

- [ ] **Step 3: Add a validated line-aware TOML table rewriter**

Add imports `re` and `tomllib` to `reviewer/install.py`. Add this helper immediately after
`_home()` and use it in the Codex `Client` entry:

```python
def codex_home_path() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else _home() / ".codex"
```

Replace the Codex registry lambda with:

```python
        Client("codex", "Codex CLI", "codex",
               lambda s: codex_home_path() / "config.toml"),
```

Then add after `_render_codex`:

```python
_TOML_TABLE_RE = re.compile(r"^\s*\[([^\[\]]+)]\s*(?:#.*)?$")


def _toml_table_name(line: str) -> str | None:
    match = _TOML_TABLE_RE.match(line.rstrip("\r\n"))
    return match.group(1).strip() if match else None


def _is_reviewer_table(name: str) -> bool:
    return name == "mcp_servers.reviewer" or name.startswith("mcp_servers.reviewer.")


def _replace_codex_reviewer_tables(raw: str, rendered: str) -> tuple[str, bool]:
    try:
        parsed = tomllib.loads(raw) if raw.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"невалидный TOML: {exc}") from exc
    lines = raw.splitlines(keepends=True)
    output: list[str] = []
    insert_at: int | None = None
    removed = False
    index = 0
    while index < len(lines):
        name = _toml_table_name(lines[index])
        if name is not None and _is_reviewer_table(name):
            if insert_at is None:
                insert_at = len(output)
            removed = True
            index += 1
            while index < len(lines) and _toml_table_name(lines[index]) is None:
                index += 1
            continue
        output.append(lines[index])
        index += 1
    existing = parsed.get("mcp_servers") if isinstance(parsed, dict) else None
    has_inline = isinstance(existing, dict) and "reviewer" in existing and not removed
    if has_inline:
        raise ValueError("inline mcp_servers.reviewer нельзя безопасно обновить")
    block = rendered.lstrip("\n")
    if insert_at is None:
        content = "".join(output).rstrip("\n")
        return content + ("\n" if content else "") + block, False
    output.insert(insert_at, block)
    return "".join(output), True
```

- [ ] **Step 4: Route Codex build plans through replacement**

Replace the Codex branch in `build_plan` with:

```python
    if client.dialect == "codex":
        content, already = _replace_codex_reviewer_tables(raw, _render_codex(command, args))
        return InstallPlan(client, path, content, command, args, not existed, already)
```

In `launch_command`, replace the PATH-dependent fallback with:

```python
    raise RuntimeError(
        "uvx/uv не найден; установите uv и повторите reviewer install"
    )
```

- [ ] **Step 5: Run installer tests**

Run:

```bash
pytest tests/install/test_install.py -v
ruff check reviewer/install.py tests/install/test_install.py
```

Expected: all existing config/backup tests and new Codex update tests pass.

- [ ] **Step 6: Commit safe MCP update**

```bash
git add reviewer/install.py tests/install/test_install.py
git commit -m "fix(install): обновлять существующий Codex MCP безопасно"
```

---

### Task 4: Marketplace ownership and compact snapshot verification

**Files:**
- Modify: `reviewer/install_codex.py`
- Modify: `tests/install/test_codex_install.py`

**Interfaces:**
- Consumes: `payload_digest`, marketplace constants, `MarketplaceState`.
- Produces:
  - `SnapshotVerification(root: Path, plugin_root: Path, version: str, skills: tuple[str, ...])`
  - `marketplace_is_owned(state: MarketplaceState) -> bool`
  - `verify_marketplace_snapshot(root: Path, expected_base_version: str) -> SnapshotVerification`

- [ ] **Step 1: Write ownership and verification tests**

Append:

```python
from reviewer.install_codex import marketplace_is_owned, verify_marketplace_snapshot


def make_snapshot(root: Path, version: str) -> Path:
    plugin = root / "plugin"
    (root / ".agents/plugins").mkdir(parents=True)
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / "assets").mkdir()
    (plugin / "skills/ask/references").mkdir(parents=True)
    (plugin / "skills/_common").mkdir(parents=True)
    (plugin / "hooks").mkdir()
    (root / ".agents/plugins/marketplace.json").write_text(json.dumps({
        "name": "rag-reviewer",
        "plugins": [{"name": "rag-reviewer",
                     "source": {"source": "local", "path": "./plugin"}}],
    }))
    (plugin / ".codex-plugin/plugin.json").write_text(json.dumps({
        "name": "rag-reviewer", "version": version, "skills": "./skills/",
        "repository": "https://github.com/mimfort/rag_for_git",
        "interface": {"composerIcon": "./assets/icon.svg"},
    }))
    (plugin / "assets/icon.svg").write_text("<svg/>")
    (plugin / "skills/ask/SKILL.md").write_text("ask")
    (plugin / "skills/ask/references/example.md").write_text("reference")
    (plugin / "skills/_common/shared.md").write_text("shared")
    (plugin / "hooks/hooks.json").write_text("{}")
    return plugin


def test_snapshot_verifies_dynamic_skills_and_common(tmp_path):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.initial")
    digest = payload_digest(plugin)
    manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
    manifest["version"] = f"0.2.27+codex.{digest}"
    (plugin / ".codex-plugin/plugin.json").write_text(json.dumps(manifest))
    verified = verify_marketplace_snapshot(tmp_path, "0.2.27")
    assert verified.skills == ("ask",)
    assert (verified.plugin_root / "skills/_common/shared.md").is_file()


def test_snapshot_rejects_bundled_mcp_and_bad_hash(tmp_path):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.000000000000")
    manifest_path = plugin / ".codex-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["mcpServers"] = "./.mcp.json"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="mcpServers"):
        verify_marketplace_snapshot(tmp_path, "0.2.27")


def test_marketplace_conflict_is_not_owned(tmp_path):
    state = MarketplaceState("rag-reviewer", tmp_path, "someone/else")
    assert marketplace_is_owned(state) is False
```

- [ ] **Step 2: Run tests and verify missing verification symbols**

Run:

```bash
pytest tests/install/test_codex_install.py -k 'snapshot or marketplace_conflict' -v
```

Expected: import fails for `verify_marketplace_snapshot`.

- [ ] **Step 3: Implement snapshot verification**

Append:

```python
@dataclass(frozen=True)
class SnapshotVerification:
    root: Path
    plugin_root: Path
    version: str
    skills: tuple[str, ...]


def _read_json(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label}: expected JSON object")
    return data


def _inside(root: Path, relative: str, label: str) -> Path:
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise RuntimeError(f"{label}: path leaves marketplace root")
    return candidate


def marketplace_is_owned(state: MarketplaceState) -> bool:
    if state.source is not None:
        return state.source == MARKETPLACE_SOURCE
    try:
        marketplace = _read_json(
            state.root / ".agents/plugins/marketplace.json", "marketplace identity"
        )
        plugin = _read_json(
            state.root / "plugin/.codex-plugin/plugin.json", "plugin identity"
        )
    except RuntimeError:
        return False
    return marketplace.get("name") == MARKETPLACE_NAME and plugin.get("repository") == (
        "https://github.com/mimfort/rag_for_git"
    )


def verify_marketplace_snapshot(
    root: Path, expected_base_version: str
) -> SnapshotVerification:
    marketplace = _read_json(root / ".agents/plugins/marketplace.json", "marketplace")
    if marketplace.get("name") != MARKETPLACE_NAME:
        raise RuntimeError("marketplace name mismatch")
    entries = marketplace.get("plugins")
    if not isinstance(entries, list) or len(entries) != 1:
        raise RuntimeError("marketplace must contain exactly one plugin")
    entry = entries[0]
    source = entry.get("source") if isinstance(entry, dict) else None
    if source != {"source": "local", "path": "./plugin"}:
        raise RuntimeError("marketplace source must be relative ./plugin")
    plugin_root = _inside(root, "plugin", "plugin source")
    manifest = _read_json(plugin_root / ".codex-plugin/plugin.json", "Codex manifest")
    if manifest.get("name") != PLUGIN_NAME:
        raise RuntimeError("plugin name mismatch")
    if "mcpServers" in manifest:
        raise RuntimeError("Codex manifest must not declare mcpServers")
    version = str(manifest.get("version", ""))
    prefix = f"{expected_base_version}+codex."
    if not version.startswith(prefix) or version.count("+codex.") != 1:
        raise RuntimeError(f"plugin version {version!r} does not match {prefix!r}")
    if version.removeprefix(prefix) != payload_digest(plugin_root):
        raise RuntimeError("plugin payload hash does not match manifest version")
    skills_root = _inside(plugin_root, str(manifest.get("skills", "")), "skills")
    skills = tuple(sorted(
        path.name for path in skills_root.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    ))
    if not skills:
        raise RuntimeError("plugin contains no registered skills")
    common = skills_root / "_common"
    if not common.is_dir() or (common / "SKILL.md").exists():
        raise RuntimeError("_common must be delivered but not registered as a skill")
    icon_rel = manifest.get("interface", {}).get("composerIcon")
    if not isinstance(icon_rel, str) or not _inside(plugin_root, icon_rel, "icon").is_file():
        raise RuntimeError("declared composerIcon is missing")
    if not (plugin_root / "hooks/hooks.json").is_file():
        raise RuntimeError("plugin hooks payload is missing")
    return SnapshotVerification(root, plugin_root, version, skills)
```

- [ ] **Step 4: Reject an occupied foreign marketplace during planning**

At the start of `build_codex_plugin_plan`, add:

```python
    if state.marketplace is not None and not marketplace_is_owned(state.marketplace):
        raise RuntimeError(
            f"marketplace {MARKETPLACE_NAME!r} уже связан с другим source/root: "
            f"{state.marketplace.source or state.marketplace.root}"
        )
```

- [ ] **Step 5: Run verification tests and payload guard**

Run:

```bash
pytest tests/install/test_codex_install.py tests/install/test_codex_plugin_payload.py -v
python scripts/update_codex_plugin_manifest.py --check
```

Expected: all tests pass and payload metadata is synchronized.

- [ ] **Step 6: Commit snapshot verification**

```bash
git add reviewer/install_codex.py tests/install/test_codex_install.py
git commit -m "feat(install): проверять Codex marketplace snapshot"
```

---

### Task 5: Transactional marketplace/plugin execution and config rollback

**Files:**
- Modify: `reviewer/install_codex.py`
- Create: `tests/install/fake_codex.py`
- Modify: `tests/install/test_codex_install.py`

**Interfaces:**
- Consumes: Tasks 2–4 state/plan/verification APIs and `reviewer.install.build_plan/apply_plan`.
- Produces:
  - `CodexInstallError(phase, argv, detail)`
  - `ConfigSnapshot(path, existed, content, backup_path)`
  - `LegacyMigrationResult(backup_root, moved, warnings)` shared with Task 6
  - `CodexInstallResult(plan, verification, config_backup, migration, warnings)`
  - `run_codex_install(options: CodexInstallOptions, *, runner: Runner = subprocess_runner, which: Callable[[str], str | None] = shutil.which, legacy_migrator: Callable[[Path, Path], LegacyMigrationResult] | None = None) -> CodexInstallResult`

- [ ] **Step 1: Create a stateful fake Codex runner**

Create `tests/install/fake_codex.py`:

```python
import json
from pathlib import Path

from reviewer.install_codex import CommandResult


class FakeCodex:
    def __init__(self, executable: Path, marketplace_root: Path):
        self.executable = executable
        self.marketplace_root = marketplace_root
        self.marketplace = False
        self.installed: dict | None = None
        self.calls: list[tuple[str, ...]] = []
        self.fail: tuple[str, ...] | None = None

    def __call__(self, argv: tuple[str, ...]) -> CommandResult:
        self.calls.append(argv)
        tail = argv[1:]
        if self.fail is not None and tail[:len(self.fail)] == self.fail:
            return CommandResult(argv, 1, "", "injected failure")
        if tail[-1:] == ("--help",):
            return CommandResult(argv, 0, "add marketplace list --json --sparse --ref --available", "")
        if tail == ("plugin", "marketplace", "list", "--json"):
            rows = ([{"name": "rag-reviewer", "root": str(self.marketplace_root),
                      "source": "mimfort/rag_for_git"}] if self.marketplace else [])
            return CommandResult(argv, 0, json.dumps({"marketplaces": rows}), "")
        if tail == ("plugin", "list", "--available", "--json"):
            installed = [self.installed] if self.installed is not None else []
            return CommandResult(argv, 0, json.dumps({"installed": installed, "available": []}), "")
        if tail[:3] == ("plugin", "marketplace", "add"):
            self.marketplace = True
            return CommandResult(argv, 0, json.dumps({"name": "rag-reviewer"}), "")
        if tail[:3] == ("plugin", "marketplace", "upgrade"):
            return CommandResult(argv, 0, json.dumps({"name": "rag-reviewer"}), "")
        if tail[:2] == ("plugin", "add"):
            manifest = json.loads(
                (self.marketplace_root / "plugin/.codex-plugin/plugin.json").read_text()
            )
            self.installed = {
                "name": "rag-reviewer", "marketplaceName": "rag-reviewer",
                "version": manifest["version"], "installed": True, "enabled": True,
            }
            return CommandResult(argv, 0, json.dumps(self.installed), "")
        return CommandResult(argv, 2, "", f"unexpected argv: {argv}")
```

- [ ] **Step 2: Write fresh, dry-run, and rollback tests**

Append to `tests/install/test_codex_install.py`:

```python
from tests.install.fake_codex import FakeCodex
from reviewer.install_codex import run_codex_install


def test_run_codex_install_fresh_updates_mcp_and_plugin(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    fake = FakeCodex(tmp_path / "bin/codex", repo)
    codex_home = tmp_path / "Codex Home"
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[other]\nvalue = 1\n")
    monkeypatch.setattr("reviewer.install.shutil.which",
                        lambda name: "C:/Program Files/uv/uvx.exe" if name == "uvx" else None)
    result = run_codex_install(
        CodexInstallOptions(codex_home=codex_home),
        runner=fake,
        which=lambda name: str(fake.executable),
    )
    assert result.verification is not None
    assert result.verification.skills
    assert "[other]" in config.read_text()
    assert "C:/Program Files/uv/uvx.exe" in config.read_text()
    assert fake.installed is not None and fake.installed["enabled"] is True


def test_run_codex_install_dry_run_has_no_mutating_calls(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    fake = FakeCodex(tmp_path / "codex", repo)
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    result = run_codex_install(
        CodexInstallOptions(dry_run=True, codex_home=tmp_path / "home"),
        runner=fake,
        which=lambda name: str(fake.executable),
    )
    assert result.verification is None
    assert result.mcp_preview is not None
    assert "[mcp_servers.reviewer]" in result.mcp_preview
    mutating = []
    for call in fake.calls:
        tail = call[1:]
        if tail[-1:] == ("--help",):
            continue
        if tail[:3] in {
            ("plugin", "marketplace", "add"),
            ("plugin", "marketplace", "upgrade"),
        } or tail[:2] == ("plugin", "add"):
            mutating.append(call)
    assert mutating == []


def test_plugin_add_failure_restores_exact_config(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    fake = FakeCodex(tmp_path / "codex", repo)
    fake.fail = ("plugin", "add")
    codex_home = tmp_path / "home"
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True)
    original = "# exact\n[other]\nvalue = 'keep'\n"
    config.write_text(original)
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    with pytest.raises(RuntimeError, match="plugin add"):
        run_codex_install(
            CodexInstallOptions(codex_home=codex_home), runner=fake,
            which=lambda name: str(fake.executable),
        )
    assert config.read_text() == original
    assert list(codex_home.glob("config.toml.rag-reviewer.*.bak"))


def test_invalid_marketplace_snapshot_restores_config(tmp_path, monkeypatch):
    empty_snapshot = tmp_path / "invalid snapshot"
    empty_snapshot.mkdir()
    fake = FakeCodex(tmp_path / "codex", empty_snapshot)
    codex_home = tmp_path / "home"
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True)
    original = "[other]\nvalue = 1\n"
    config.write_text(original)
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    with pytest.raises(RuntimeError, match="snapshot verification"):
        run_codex_install(
            CodexInstallOptions(codex_home=codex_home), runner=fake,
            which=lambda name: str(fake.executable),
        )
    assert config.read_text() == original


def test_marketplace_add_failure_restores_config(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    fake = FakeCodex(tmp_path / "codex", repo)
    fake.fail = ("plugin", "marketplace", "add")
    codex_home = tmp_path / "home"
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True)
    original = "[other]\nvalue = 1\n"
    config.write_text(original)
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    with pytest.raises(RuntimeError, match="marketplace add"):
        run_codex_install(
            CodexInstallOptions(codex_home=codex_home), runner=fake,
            which=lambda name: str(fake.executable),
        )
    assert config.read_text() == original


def test_mcp_write_failure_restores_config_and_skips_plugin_add(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    fake = FakeCodex(tmp_path / "codex", repo)
    codex_home = tmp_path / "home"
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True)
    original = "[other]\nvalue = 1\n"
    config.write_text(original)
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    monkeypatch.setattr("reviewer.install.apply_plan",
                        lambda plan: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(RuntimeError, match="MCP config update"):
        run_codex_install(
            CodexInstallOptions(codex_home=codex_home), runner=fake,
            which=lambda name: str(fake.executable),
        )
    assert config.read_text() == original
    assert not any(call[1:3] == ("plugin", "add") for call in fake.calls)


def test_post_verification_failure_restores_previous_selection(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    exe = tmp_path / "codex"
    fake = FakeCodex(exe, repo)
    fake.marketplace = True
    codex_home = tmp_path / "home"
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True)
    original = "[plugins]\nselected = 'old'\n"
    config.write_text(original)
    marketplace = MarketplaceState("rag-reviewer", repo, "mimfort/rag_for_git")
    old_plugin = PluginState("rag-reviewer", "rag-reviewer", "0.2.26+codex.old",
                             True, True)
    wrong_plugin = PluginState("rag-reviewer", "rag-reviewer", "wrong-version", True, True)
    states = iter([
        CodexPluginState(exe, marketplace, old_plugin),
        CodexPluginState(exe, marketplace, old_plugin),
        CodexPluginState(exe, marketplace, wrong_plugin),
        CodexPluginState(exe, marketplace, old_plugin),
    ])
    monkeypatch.setattr(
        "reviewer.install_codex.read_codex_state", lambda executable, runner: next(states)
    )
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    with pytest.raises(RuntimeError, match="installed version"):
        run_codex_install(
            CodexInstallOptions(codex_home=codex_home), runner=fake,
            which=lambda name: str(exe),
        )
    assert config.read_text() == original
```

- [ ] **Step 3: Run tests and verify the missing executor failure**

Run:

```bash
pytest tests/install/test_codex_install.py -k 'run_codex or plugin_add_failure' -v
```

Expected: import fails for `run_codex_install`.

- [ ] **Step 4: Implement transaction/result types and checked commands**

Add `import os`, `from datetime import datetime, timezone`, and `Any` to the existing typing import
at the top of `reviewer/install_codex.py`. Then append:

```python
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
class CodexInstallResult:
    plan: CodexPluginPlan
    verification: SnapshotVerification | None
    config_backup: Path | None
    migration: LegacyMigrationResult
    warnings: tuple[str, ...]
    mcp_preview: str | None = None


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
    backup = path.with_name(path.name + f".rag-reviewer.{stamp}.bak")
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(content)
    return ConfigSnapshot(path, existed, content, backup)


def _restore_config(snapshot: ConfigSnapshot) -> None:
    if snapshot.existed:
        snapshot.path.parent.mkdir(parents=True, exist_ok=True)
        snapshot.path.write_bytes(snapshot.content)
    elif snapshot.path.exists():
        snapshot.path.unlink()


def _codex_home(options: CodexInstallOptions) -> Path:
    if options.codex_home is not None:
        return options.codex_home
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"
```

- [ ] **Step 5: Implement the orchestration flow**

Append:

```python
def _verified_installed(state: CodexPluginState, expected_version: str) -> None:
    plugin = state.plugin
    if plugin is None:
        raise RuntimeError("plugin list: rag-reviewer не установлен")
    if plugin.marketplace != MARKETPLACE_NAME:
        raise RuntimeError(f"plugin установлен из {plugin.marketplace!r}")
    if not plugin.installed or not plugin.enabled:
        raise RuntimeError("plugin должен быть installed и enabled")
    if plugin.version != expected_version:
        raise RuntimeError(f"installed version {plugin.version!r} != {expected_version!r}")


def run_codex_install(
    options: CodexInstallOptions,
    *,
    runner: Runner = subprocess_runner,
    which: Callable[[str], str | None] = shutil.which,
    legacy_migrator: Callable[[Path, Path], LegacyMigrationResult] | None = None,
) -> CodexInstallResult:
    from reviewer import install as generic

    executable = find_codex_executable(which)
    detect_codex_capabilities(executable, runner)
    state = read_codex_state(executable, runner)
    plan = build_codex_plugin_plan(state, options)
    empty_migration = LegacyMigrationResult(None, (), ())
    codex_home = _codex_home(options)
    config_path = options.mcp_path or (codex_home / "config.toml")
    mcp_preview = None
    if options.include_mcp:
        mcp_preview = generic.build_plan(
            generic.CLIENTS["codex"], version=options.mcp_version,
            path_override=str(config_path),
        ).content
    if options.dry_run:
        return CodexInstallResult(plan, None, None, empty_migration, (), mcp_preview)

    snapshot = _snapshot_config(config_path)
    try:
        _checked(runner, plan.marketplace_argv, f"marketplace {plan.marketplace_action}")
        refreshed = read_codex_state(executable, runner)
        if refreshed.marketplace is None:
            raise RuntimeError("marketplace list не вернул rag-reviewer после mutation")
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
                    generic.CLIENTS["codex"], version=options.mcp_version,
                    path_override=str(config_path),
                )
                generic.apply_plan(mcp_plan)
            except (OSError, ValueError) as exc:
                raise CodexInstallError("MCP config update", (), str(exc)) from exc
        _checked(runner, plan.plugin_argv, "plugin add")
        installed = read_codex_state(executable, runner)
        try:
            _verified_installed(installed, verification.version)
        except RuntimeError as exc:
            raise CodexInstallError(
                "post-install verification",
                (str(executable), "plugin", "list", "--available", "--json"),
                str(exc),
            ) from exc
        migration = (
            legacy_migrator(codex_home / "skills", verification.plugin_root)
            if legacy_migrator is not None else empty_migration
        )
        return CodexInstallResult(
            plan, verification, snapshot.backup_path, migration, migration.warnings,
            mcp_preview,
        )
    except Exception as original_error:
        try:
            _restore_config(snapshot)
            rolled_back = read_codex_state(executable, runner)
            if rolled_back.plugin != state.plugin:
                raise RuntimeError(
                    "config rollback не восстановил предыдущую plugin selection; "
                    f"backup: {snapshot.backup_path}"
                )
        except Exception as restore_error:
            raise RuntimeError(
                f"config rollback failed; backup: {snapshot.backup_path}: {restore_error}"
            ) from restore_error
        raise original_error
```

- [ ] **Step 6: Run transaction tests**

Run:

```bash
pytest tests/install/test_codex_install.py -v
ruff check reviewer/install_codex.py tests/install/fake_codex.py tests/install/test_codex_install.py
```

Expected: fresh, dry-run, and rollback tests pass.

- [ ] **Step 7: Commit the transactional executor**

```bash
git add reviewer/install_codex.py tests/install/fake_codex.py tests/install/test_codex_install.py
git commit -m "feat(install): выполнить Codex plugin transaction с rollback"
```

---

### Task 6: Positive-identification legacy skills migration

**Files:**
- Modify: `reviewer/install_codex.py`
- Modify: `tests/install/test_codex_install.py`
- Modify: `tests/install/test_common_shared_dir.py`
- Modify: `reviewer/install.py:28-40`

**Interfaces:**
- Consumes: verified `plugin_root`, current `.reviewer-skills.json` format, `LegacyMigrationResult`.
- Produces:
  - `LegacySkillCandidate(name, source, reason)`
  - `find_owned_legacy_skills(skills_root, plugin_root) -> tuple[LegacySkillCandidate, ...]`
  - `migrate_legacy_skills(skills_root, plugin_root) -> LegacyMigrationResult`

- [ ] **Step 1: Write stamp, exact-match, ambiguous, and rollback tests**

Append:

```python
from reviewer.install_codex import find_owned_legacy_skills, migrate_legacy_skills


def copy_skill(plugin_root: Path, skills_root: Path, name: str) -> None:
    import shutil
    shutil.copytree(plugin_root / "skills" / name, skills_root / name)


def test_legacy_candidates_require_stamp_or_exact_payload(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    plugin = repo / "plugin"
    skills = tmp_path / "skills"
    skills.mkdir()
    copy_skill(plugin, skills, "ask")
    copy_skill(plugin, skills, "solve-task")
    (skills / "solve-task/SKILL.md").write_text("locally modified")
    candidates = find_owned_legacy_skills(skills, plugin)
    assert [(item.name, item.reason) for item in candidates] == [("ask", "exact payload match")]
    assert (skills / "solve-task").is_dir()


def test_legacy_migration_moves_owned_and_keeps_ambiguous(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    plugin = repo / "plugin"
    skills = tmp_path / "skills"
    skills.mkdir()
    copy_skill(plugin, skills, "ask")
    copy_skill(plugin, skills, "solve-task")
    (skills / "solve-task/SKILL.md").write_text("modified")
    result = migrate_legacy_skills(skills, plugin)
    assert result.moved == ("ask",)
    assert result.backup_root is not None
    assert (result.backup_root / "skills/ask/SKILL.md").is_file()
    assert (skills / "solve-task/SKILL.md").read_text() == "modified"
    assert result.warnings


def test_legacy_migration_restores_already_moved_directories(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    plugin = repo / "plugin"
    skills = tmp_path / "skills"
    skills.mkdir()
    copy_skill(plugin, skills, "ask")
    copy_skill(plugin, skills, "finish-task")
    original_replace = Path.replace

    def flaky_replace(self: Path, target: Path) -> Path:
        if self.parent == skills and self.name == "finish-task":
            raise OSError("injected move failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    result = migrate_legacy_skills(skills, plugin)
    assert result.moved == ()
    assert (skills / "ask/SKILL.md").is_file()
    assert (skills / "finish-task/SKILL.md").is_file()
    assert any("восстановлена" in warning for warning in result.warnings)
```

- [ ] **Step 2: Run migration tests and verify missing functions**

Run:

```bash
pytest tests/install/test_codex_install.py -k legacy -v
```

Expected: import fails for `find_owned_legacy_skills`.

- [ ] **Step 3: Implement byte-for-byte/stamp classification**

Append:

```python
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
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _stamp_owned(skills_root: Path, name: str, source: Path) -> bool:
    stamp_path = skills_root / ".reviewer-skills.json"
    try:
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    source_url = str(stamp.get("source_url", ""))
    expected_hash = (stamp.get("skills") or {}).get(name)
    if "mimfort/rag_for_git" not in source_url or not expected_hash:
        return False
    import hashlib
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
            candidates.append(LegacySkillCandidate(source.name, source, "valid installer stamp"))
        elif _directory_files(source) == _directory_files(expected):
            candidates.append(LegacySkillCandidate(source.name, source, "exact payload match"))
    return tuple(candidates)
```

- [ ] **Step 4: Implement migration as a restoring mini-transaction**

Append:

```python
def migrate_legacy_skills(skills_root: Path, plugin_root: Path) -> LegacyMigrationResult:
    candidates = find_owned_legacy_skills(skills_root, plugin_root)
    payload_names = {
        path.name for path in (plugin_root / "skills").iterdir() if path.is_dir()
    }
    candidate_names = {item.name for item in candidates}
    ambiguous = tuple(sorted(
        path.name for path in skills_root.iterdir()
        if path.is_dir() and path.name in payload_names and path.name not in candidate_names
    )) if skills_root.is_dir() else ()
    warnings = [f"legacy skill {name!r} изменён/неполон — оставлен на месте"
                for name in ambiguous]
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
        backup_root, tuple(source.name for source, target in moved), tuple(warnings)
    )
```

- [ ] **Step 5: Remove the hardcoded skill inventory**

Delete `SKILL_NAMES` from `reviewer/install.py`. Delete
`test_common_registered_in_skill_names` from `tests/install/test_common_shared_dir.py`; retain
`test_common_dir_extracted_and_hashed`, which dynamically proves `_common` is delivered and
hashed.

- [ ] **Step 6: Run migration and existing standalone-skill tests**

Run:

```bash
pytest tests/install/test_codex_install.py tests/install/test_common_shared_dir.py \
  tests/install/test_skills_stamp.py tests/install/test_skills_staleness.py -v
```

Expected: exact/stamped copies migrate, modified copies remain, `_common` coverage passes, and
standalone clients retain their stamp/staleness behavior.

- [ ] **Step 7: Commit legacy migration**

```bash
git add reviewer/install_codex.py reviewer/install.py \
  tests/install/test_codex_install.py tests/install/test_common_shared_dir.py
git commit -m "feat(install): безопасно мигрировать legacy Codex skills"
```

---

### Task 7: Route all Codex CLI entrypoints through the canonical flow

**Files:**
- Modify: `reviewer/entrypoints/cli.py:284-430,513-574`
- Create: `tests/install/test_codex_cli.py`

**Interfaces:**
- Consumes: `run_codex_install`, `migrate_legacy_skills`, `CodexInstallOptions`, and existing
  generic install APIs.
- Produces:
  - `_run_codex_target(*, include_mcp: bool, dry_run: bool, version: str = "latest", path_opt: str | None = None) -> CodexInstallResult`
  - `_print_codex_result(result) -> None`
  - Click behavior for `install codex`, `install-skills codex`, `install --all`, and `init`.

- [ ] **Step 1: Write CLI routing tests**

Create `tests/install/test_codex_cli.py`:

```python
from pathlib import Path

from click.testing import CliRunner

from reviewer import install as generic_install
from reviewer.entrypoints.cli import cli
from reviewer.install_codex import (
    CodexInstallResult,
    CodexPluginPlan,
    CodexPluginState,
    LegacyMigrationResult,
    MarketplaceState,
    SnapshotVerification,
    CodexInstallOptions,
)


def fake_result(tmp_path: Path, options: CodexInstallOptions) -> CodexInstallResult:
    state = CodexPluginState(
        tmp_path / "codex",
        MarketplaceState("rag-reviewer", tmp_path, "mimfort/rag_for_git"),
        None,
    )
    plan = CodexPluginPlan(state, options, "upgrade", ("codex", "upgrade"),
                           ("codex", "add"))
    verification = None if options.dry_run else SnapshotVerification(
        tmp_path, tmp_path / "plugin", "0.2.27+codex.123456789abc", ("ask", "finish-task")
    )
    return CodexInstallResult(
        plan, verification, tmp_path / "config.bak",
        LegacyMigrationResult(tmp_path / "legacy", ("ask",), ()), (),
    )


def test_install_codex_routes_mcp_and_plugin(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(
        "reviewer.install_codex.run_codex_install",
        lambda options, **kwargs: captured.append(options) or fake_result(tmp_path, options),
    )
    result = CliRunner().invoke(cli, ["install", "codex"])
    assert result.exit_code == 0, result.output
    assert captured[0].include_mcp is True
    assert "New Chat/new CLI session" in result.output
    assert "Reload Window" in result.output


def test_install_codex_no_skills_does_not_call_plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "reviewer.install_codex.run_codex_install",
        lambda options, **kwargs: (_ for _ in ()).throw(AssertionError("plugin called")),
    )
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    result = CliRunner().invoke(
        cli, ["install", "codex", "--no-skills", "--path", str(tmp_path / "config.toml")]
    )
    assert result.exit_code == 0, result.output


def test_install_skills_codex_is_plugin_only_and_supports_dry_run(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(
        "reviewer.install_codex.run_codex_install",
        lambda options, **kwargs: captured.append(options) or fake_result(tmp_path, options),
    )
    result = CliRunner().invoke(cli, ["install-skills", "codex", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert captured[0].include_mcp is False and captured[0].dry_run is True


def test_init_yes_never_invokes_codex(monkeypatch, tmp_path):
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(
        "reviewer.install_codex.run_codex_install",
        lambda options, **kwargs: (_ for _ in ()).throw(AssertionError("Codex invoked")),
    )
    result = CliRunner().invoke(cli, ["init", "--yes"])
    assert result.exit_code == 0, result.output


def test_interactive_init_uses_the_canonical_codex_flow(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(
        "reviewer.install.prompt_groups",
        lambda groups, current, yes: {field.key: field.default
                                     for group in groups for field in group.fields},
    )
    answers = iter([False, True])
    monkeypatch.setattr("click.confirm", lambda prompt, default=True: next(answers))
    monkeypatch.setattr("reviewer.entrypoints.cli._shutil.which", lambda name: "/opt/codex")
    monkeypatch.setattr(
        "reviewer.install_codex.run_codex_install",
        lambda options, **kwargs: captured.append(options) or fake_result(tmp_path, options),
    )
    result = CliRunner().invoke(cli, ["init", "--path", str(tmp_path / ".env")])
    assert result.exit_code == 0, result.output
    assert captured and captured[0].include_mcp is True


def test_install_all_reports_codex_failure_after_other_targets(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(generic_install, "_home", lambda: home)
    monkeypatch.setattr(generic_install, "detect_installed", lambda: [
        generic_install.CLIENTS["cursor"], generic_install.CLIENTS["codex"]
    ])
    monkeypatch.setattr(generic_install.shutil, "which", lambda name: "/opt/uvx")
    monkeypatch.setattr(
        "reviewer.install_codex.run_codex_install",
        lambda options, **kwargs: (_ for _ in ()).throw(RuntimeError("plugin failed")),
    )
    result = CliRunner().invoke(cli, ["install", "--all"])
    assert result.exit_code != 0
    assert "plugin failed" in result.output
    assert (home / ".cursor/mcp.json").is_file()
```

- [ ] **Step 2: Run CLI tests and verify failures**

Run:

```bash
pytest tests/install/test_codex_cli.py -v
```

Expected: Codex plugin route is not called and `install-skills codex --dry-run` is rejected.

- [ ] **Step 3: Add shared CLI helpers**

In `reviewer/entrypoints/cli.py`, add:

```python
def _run_codex_target(*, include_mcp: bool, dry_run: bool,
                      version: str = "latest", path_opt: str | None = None):
    from pathlib import Path
    from reviewer import install_codex

    if path_opt and include_mcp:
        raise click.ClickException(
            "Codex plugin lifecycle несовместим с --path; используйте --no-skills"
        )
    options = install_codex.CodexInstallOptions(
        include_mcp=include_mcp,
        dry_run=dry_run,
        mcp_version=version,
        mcp_path=Path(path_opt).expanduser() if path_opt else None,
    )
    try:
        return install_codex.run_codex_install(
            options, legacy_migrator=install_codex.migrate_legacy_skills
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


def _print_codex_result(result) -> None:
    if result.plan.options.dry_run:
        if result.mcp_preview is not None:
            click.echo("# Codex MCP config preview")
            click.echo(result.mcp_preview)
        click.echo(f"# Codex marketplace: {result.plan.marketplace_action}")
        click.echo("# " + " ".join(result.plan.marketplace_argv))
        click.echo("# " + " ".join(result.plan.plugin_argv))
        click.echo("# legacy migration: scan after verified plugin install")
        return
    click.echo(f"✓ Codex plugin: {result.verification.version}")
    click.echo(f"  skills: {len(result.verification.skills)}")
    if result.config_backup:
        click.echo(f"  config backup: {result.config_backup}")
    if result.migration.backup_root:
        click.echo(f"  legacy backup: {result.migration.backup_root}")
    for warning in result.warnings:
        click.echo(f"  предупреждение: {warning}")
    click.echo("Откройте New Chat/new CLI session; в IDE выполните Reload Window.")
```

- [ ] **Step 4: Route `install codex` and preserve MCP-only behavior**

Before the `for c in targets` loop, initialize the aggregate:

```python
    codex_errors: list[str] = []
    codex_mcp_only = no_skills and any(c.key == "codex" for c in targets)
```

Inside the loop, before generic `build_plan`, add:

```python
        if c.key == "codex" and not no_skills:
            try:
                result = _run_codex_target(
                    include_mcp=True, dry_run=dry_run, version=version,
                    path_opt=path_opt,
                )
                _print_codex_result(result)
            except click.ClickException as exc:
                if not all_clients:
                    raise
                codex_errors.append(f"Codex CLI: {exc.format_message()}")
            continue
```

Remove the old Codex-only status text saying TOML is not touched; after Task 3 an existing section
uses the same `updated` status as JSON clients. Keep generic flow for `--no-skills`.

Before the final general success message, add:

```python
    if codex_errors:
        raise click.ClickException("; ".join(codex_errors))
    if not dry_run and codex_mcp_only:
        click.echo("Codex MCP обновлён. Откройте New Chat/new CLI session; "
                   "в IDE выполните Reload Window.")
```

- [ ] **Step 5: Route `install-skills codex` and add dry-run**

Add the option:

```python
@click.option("--dry-run", is_flag=True,
              help="показать plugin plan без записи и сети")
```

Add `dry_run: bool` to the function signature. Change target selection and execution to:

```python
    capable = [c for c in inst.CLIENTS.values() if c.skills_fn]
    if list_clients:
        click.echo("Клиенты со скилами:")
        for c in capable:
            click.echo(f"  {c.key:<15} {c.label} → {c.skills_fn(_platform.system())}")
        click.echo("  codex           Codex CLI → plugin marketplace")
        return

    if all_clients:
        targets = [
            c for c in inst.detect_installed()
            if c.skills_fn is not None or c.key == "codex"
        ]
        if not targets:
            raise click.ClickException(
                "Не обнаружено клиентов со скилами. Укажите явно или см. --list."
            )
    elif client:
        key = client.lower()
        if key not in inst.CLIENTS:
            raise click.ClickException(
                f"Неизвестный клиент {client!r}. Список: reviewer install-skills --list"
            )
        targets = [inst.CLIENTS[key]]
    else:
        raise click.ClickException(
            "Укажите клиент (reviewer install-skills <client>), либо --all / --list."
        )

    if path_opt and any(c.key == "codex" for c in targets):
        raise click.ClickException("Codex plugin не поддерживает --path")
    if dry_run and any(c.key != "codex" for c in targets):
        raise click.ClickException("install-skills --dry-run поддерживается только для codex")

    for c in [target for target in targets if target.key == "codex"]:
        result = _run_codex_target(include_mcp=False, dry_run=dry_run)
        _print_codex_result(result)

    file_targets = [target for target in targets if target.key != "codex"]
    if not file_targets:
        return
    click.echo("Скачиваю скилы с GitHub…")
    tar, etag = inst.fetch_skills_archive()
    for c in file_targets:
        if c.skills_fn is None:
            raise click.ClickException(f"{c.label}: файловые скилы не поддерживаются")
        dest = Path(path_opt).expanduser() if path_opt else c.skills_fn(_platform.system())
        names = inst.extract_skills(tar, dest)
        inst.stamp_skills_dir(dest, source_etag=etag)
        click.echo(f"✓ {c.label}: {len(names)} скилов → {dest}")
        if c.note:
            click.echo(f"  прим.: {c.note}")
```

Delete the superseded old `capable`/target-selection/download loop rather than leaving two paths.

- [ ] **Step 6: Add the interactive `init` handoff**

After the existing check prompt/output in `init`, add:

```python
    if not yes and _shutil.which("codex") and click.confirm(
        "\nУстановить или обновить rag-reviewer для Codex?", default=True
    ):
        result = _run_codex_target(include_mcp=True, dry_run=False)
        _print_codex_result(result)
```

- [ ] **Step 7: Run CLI and installer tests**

Run:

```bash
pytest tests/install/test_codex_cli.py tests/install/test_install.py \
  tests/install/test_install_skills_cli.py tests/install/test_install_wizard.py -v
ruff check reviewer/entrypoints/cli.py tests/install/test_codex_cli.py
```

Expected: all entrypoint variants pass; `init --yes` never invokes Codex.

- [ ] **Step 8: Commit CLI wiring**

```bash
git add reviewer/entrypoints/cli.py tests/install/test_codex_cli.py
git commit -m "feat(cli): подключить canonical Codex plugin flow"
```

---

### Task 8: Documentation, three-OS CI, and final regression

**Files:**
- Create: `.github/workflows/codex-plugin.yml`
- Modify: `README.md`
- Modify: `README.ru.md`
- Modify: `AGENTS.md`
- Modify: `plugin/README.md`
- Modify: `tests/install/test_codex_plugin_payload.py`

**Interfaces:**
- Consumes: completed CLI and payload guard.
- Produces: canonical user documentation and cross-platform automated acceptance gate.

- [ ] **Step 1: Strengthen the real-repository inventory test**

Append to `tests/install/test_codex_plugin_payload.py`:

```python
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
```

- [ ] **Step 2: Add focused cross-platform CI**

Create `.github/workflows/codex-plugin.yml`:

```yaml
name: Codex Plugin

on:
  pull_request:
    branches: [main]
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      live_smoke:
        description: Run the real networked Codex marketplace smoke test
        required: true
        type: boolean
        default: false

permissions:
  contents: read

jobs:
  codex-plugin:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0
      - uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]"
      - run: python scripts/update_codex_plugin_manifest.py --check
      - run: >-
          pytest
          tests/install/test_codex_plugin_payload.py
          tests/install/test_codex_install.py
          tests/install/test_codex_cli.py
          tests/install/test_install.py
          -v

  live-smoke:
    if: ${{ github.event_name == 'workflow_dispatch' && inputs.live_smoke }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0
      - uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]"
      - run: pip install uv
      - run: npm install -g @openai/codex
      - run: reviewer install codex
        env:
          CODEX_HOME: ${{ runner.temp }}/codex-home
      - run: codex plugin list --json
        env:
          CODEX_HOME: ${{ runner.temp }}/codex-home
      - run: codex mcp list
        env:
          CODEX_HOME: ${{ runner.temp }}/codex-home
```

- [ ] **Step 3: Replace Codex installation docs with canonical commands**

Use this exact command block in README EN/RU and `AGENTS.md`:

```bash
uvx --from rag-reviewer@latest reviewer install codex
uvx --from rag-reviewer@latest reviewer install codex --dry-run
uvx --from rag-reviewer@latest reviewer install codex --no-skills
uvx --from rag-reviewer@latest reviewer install-skills codex
```

Document that the first command manages one global MCP plus the namespaced plugin, repeat runs
upgrade the marketplace/plugin, dry-run is read-only/offline, and `install-skills` leaves MCP
untouched. Delete the Codex manual `curl`/`tar` instructions and the POSIX `/bin/bash` Codex MCP
example from `AGENTS.md`.

- [ ] **Step 4: Replace hardcoded skill counts with dynamic wording**

In `README.md`, replace hardcoded counts with this paragraph:

```text
Every directory under plugin/skills/ that contains SKILL.md is registered under the
rag-reviewer namespace. _common and nested references are delivered as supporting files but are
not registered as skills.
```

In `README.ru.md` and `plugin/README.md`, use:

```text
Каждый каталог plugin/skills/ с файлом SKILL.md регистрируется в namespace rag-reviewer.
_common и вложенные references доставляются как вспомогательные файлы, но не регистрируются как
скиллы.
```

Keep the current explicit skill list in `README.md`, `README.ru.md`, and `plugin/README.md`, add
`finish-task`, and use the payload test above to require both top-level README files to contain
every dynamically discovered skill name.

- [ ] **Step 5: Document verification and recovery UX**

Add this verification block to all four documents:

```bash
codex plugin list --json
codex mcp list
```

Use this exact English paragraph in `README.md` and `AGENTS.md`:

```text
Success means `rag-reviewer` is installed and enabled and `codex mcp list` contains exactly one
`reviewer`. Identified legacy skills are moved to
`$CODEX_HOME/reviewer-legacy-backups/<timestamp>`; modified or ambiguous copies stay untouched.
Failures print the config backup path. Open a New Chat/new CLI session after installation; in an
IDE, also use Reload Window.
```

Use this exact Russian paragraph in `README.ru.md` and `plugin/README.md`:

```text
Успех означает, что `rag-reviewer` установлен и включён, а `codex mcp list` содержит ровно один
`reviewer`. Идентифицированные legacy skills перемещаются в
`$CODEX_HOME/reviewer-legacy-backups/<timestamp>`; изменённые и неоднозначные копии остаются на
месте. При ошибке печатается путь к backup конфига. После установки откройте New Chat/new CLI
session; в IDE также выполните Reload Window.
```

- [ ] **Step 6: Run documentation/payload guards and focused suite**

Run:

```bash
python scripts/update_codex_plugin_manifest.py
python scripts/update_codex_plugin_manifest.py --check
pytest tests/install tests/skills -v
ruff check reviewer scripts tests/install
```

Expected: metadata check passes, all install/skill guard tests pass, Ruff reports no errors.

- [ ] **Step 7: Run the complete project regression suite**

Run:

```bash
pytest -q
```

Expected: all non-integration tests pass with zero failures.

- [ ] **Step 8: Inspect final diff for secrets and machine paths**

Run:

```bash
git diff --check
rg -n "/Users/|C:\\\\Users\\\\|\.env|/bin/bash|source.*local.*absolute" \
  .agents plugin/.codex-plugin .codex-plugin reviewer/install_codex.py \
  README.md README.ru.md AGENTS.md plugin/README.md
```

Expected: `git diff --check` is silent; search returns no machine path, secret file, or Codex
`/bin/bash` launcher. The retained Claude `plugin/.mcp.json` is outside this Codex-manifest search
scope.

- [ ] **Step 9: Commit docs and CI**

```bash
git add .github/workflows/codex-plugin.yml README.md README.ru.md AGENTS.md \
  plugin/README.md plugin/.codex-plugin/plugin.json .codex-plugin/plugin.json \
  plugin/assets/icon.svg tests/install/test_codex_plugin_payload.py
git commit -m "docs(install): описать глобальный Codex plugin lifecycle"
```

---

## Final Acceptance Check

- [ ] Run `python scripts/update_codex_plugin_manifest.py --check` and confirm exit 0.
- [ ] Run `pytest tests/install tests/skills -v` and confirm all focused tests pass.
- [ ] Run `pytest -q` and confirm the complete non-integration suite passes.
- [ ] Run `ruff check reviewer scripts tests/install` and confirm no lint errors.
- [ ] Confirm `.agents/plugins/marketplace.json` uses relative `./plugin`.
- [ ] Confirm both Codex manifests omit `mcpServers` and share one version.
- [ ] Confirm `plugin/skills/finish-task/SKILL.md` is dynamically included and `_common` is not registered.
- [ ] Confirm no unrelated untracked/user files were staged.
