# Unified Update Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `reviewer update` into the one-command package, client integration, skills, and no-clobber Compose update lifecycle.

**Architecture:** Keep package-version detection in `reviewer/versioning.py`, add a focused `reviewer/update_lifecycle.py` for managed Compose state and fresh-process execution, and let the Click command reuse `reviewer install --all` through `Context.invoke`. A hidden artifact phase prevents recursion after an in-place `uv tool` upgrade; an explicit `--upgrade-tool` lets the latest uvx process bootstrap the already-installed 0.4.3 tool safely.

**Tech Stack:** Python 3.11, Click, urllib, SHA-256/JSON sidecar state, uv tool/uvx, pytest, Ruff, bilingual Markdown guard tests.

---

## File Map

- Create `reviewer/update_lifecycle.py`: download and safely synchronize the canonical Compose file; launch the updated persistent CLI in a fresh process.
- Create `tests/test_update_lifecycle.py`: pure unit coverage for Compose ownership, download behavior, and subprocess dispatch.
- Modify `reviewer/entrypoints/cli.py`: two-phase update orchestration, explicit uvx bootstrap, detected-client refresh, phase error aggregation.
- Modify `tests/entrypoints/test_update_command.py`: CLI behavior for uv tool, uvx, editable, fresh-process, client refresh, and partial failures.
- Modify `reviewer/launcher/metadata.py`: describe update as the complete mutating lifecycle.
- Modify `tests/launcher/test_catalog.py`: lock the new launcher description.
- Modify `README.md` and `README.ru.md`: remove manual Compose downloads and document bootstrap/steady-state update behavior in parallel.
- Modify `tests/docs/test_readme_onboarding.py`: require the one-command lifecycle and reject onboarding `curl` instructions.
- Modify `pyproject.toml`, `uv.lock`, `plugin/.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, and `plugin/.codex-plugin/plugin.json`: publish the feature as 0.4.4 with synchronized manifests.

### Task 1: Managed Compose Synchronization

**Files:**
- Create: `reviewer/update_lifecycle.py`
- Create: `tests/test_update_lifecycle.py`

- [ ] **Step 1: Write failing tests for create, adopt, no-op, managed update, and modified preservation**

```python
import hashlib
import json

from reviewer.update_lifecycle import sync_compose_file


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def test_sync_compose_creates_missing_target_and_state(tmp_path):
    content = b"services:\n  db: {}\n"

    result = sync_compose_file(content, config_dir=tmp_path)

    assert result.action == "created"
    assert result.path.read_bytes() == content
    state = json.loads((tmp_path / ".reviewer-update.json").read_text())
    assert state == {"docker_compose_sha256": _digest(content)}


def test_sync_compose_adopts_exact_unmanaged_download(tmp_path):
    content = b"services:\n  db: {}\n"
    (tmp_path / "docker-compose.yml").write_bytes(content)

    result = sync_compose_file(content, config_dir=tmp_path)

    assert result.action == "adopted"
    assert json.loads((tmp_path / ".reviewer-update.json").read_text()) == {
        "docker_compose_sha256": _digest(content)
    }


def test_sync_compose_reports_current_managed_file(tmp_path):
    content = b"services:\n  db: {}\n"
    sync_compose_file(content, config_dir=tmp_path)

    result = sync_compose_file(content, config_dir=tmp_path)

    assert result.action == "current"


def test_sync_compose_updates_only_file_matching_recorded_hash(tmp_path):
    old = b"services:\n  db: {image: old}\n"
    new = b"services:\n  db: {image: new}\n"
    sync_compose_file(old, config_dir=tmp_path)

    result = sync_compose_file(new, config_dir=tmp_path)

    assert result.action == "updated"
    assert result.path.read_bytes() == new


def test_sync_compose_preserves_modified_managed_file_and_old_state(tmp_path):
    old = b"services:\n  db: {image: old}\n"
    new = b"services:\n  db: {image: new}\n"
    sync_compose_file(old, config_dir=tmp_path)
    target = tmp_path / "docker-compose.yml"
    target.write_bytes(b"services:\n  db: {ports: [custom]}\n")
    state_before = (tmp_path / ".reviewer-update.json").read_bytes()

    result = sync_compose_file(new, config_dir=tmp_path)

    assert result.action == "preserved"
    assert target.read_bytes() == b"services:\n  db: {ports: [custom]}\n"
    assert (tmp_path / ".reviewer-update.json").read_bytes() == state_before


def test_sync_compose_preserves_unmanaged_nonmatching_file(tmp_path):
    target = tmp_path / "docker-compose.yml"
    target.write_bytes(b"custom\n")

    result = sync_compose_file(b"canonical\n", config_dir=tmp_path)

    assert result.action == "preserved"
    assert target.read_bytes() == b"custom\n"
    assert not (tmp_path / ".reviewer-update.json").exists()
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv/bin/pytest tests/test_update_lifecycle.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'reviewer.update_lifecycle'`.

- [ ] **Step 3: Implement the minimal no-clobber state machine with atomic writes**

```python
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


COMPOSE_URL = (
    "https://raw.githubusercontent.com/mimfort/rag_for_git/main/docker-compose.yml"
)
STATE_NAME = ".reviewer-update.json"


@dataclass(frozen=True)
class ComposeSyncResult:
    action: Literal["created", "adopted", "current", "updated", "preserved"]
    path: Path


def default_config_dir() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    return (Path(root).expanduser() if root else Path.home() / ".config") / "rag-reviewer"


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(content)
        temporary = Path(tmp.name)
    temporary.replace(path)


def _read_recorded_hash(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = payload.get("docker_compose_sha256") if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value.startswith("sha256:") else None


def _write_state(path: Path, digest: str) -> None:
    content = json.dumps(
        {"docker_compose_sha256": digest}, indent=2, ensure_ascii=False
    ).encode("utf-8") + b"\n"
    _atomic_write(path, content)


def sync_compose_file(
    content: bytes,
    *,
    config_dir: Path | None = None,
) -> ComposeSyncResult:
    directory = config_dir or default_config_dir()
    target = directory / "docker-compose.yml"
    state_path = directory / STATE_NAME
    incoming_hash = _digest(content)

    if not target.exists():
        _atomic_write(target, content)
        _write_state(state_path, incoming_hash)
        return ComposeSyncResult("created", target)

    existing = target.read_bytes()
    existing_hash = _digest(existing)
    recorded_hash = _read_recorded_hash(state_path)
    if existing_hash == incoming_hash:
        action = "current" if recorded_hash == incoming_hash else "adopted"
        _write_state(state_path, incoming_hash)
        return ComposeSyncResult(action, target)
    if recorded_hash is None or recorded_hash != existing_hash:
        return ComposeSyncResult("preserved", target)

    _atomic_write(target, content)
    _write_state(state_path, incoming_hash)
    return ComposeSyncResult("updated", target)
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `.venv/bin/pytest tests/test_update_lifecycle.py -q`

Expected: `6 passed`.

- [ ] **Step 5: Commit the managed Compose state machine**

```bash
git add reviewer/update_lifecycle.py tests/test_update_lifecycle.py
git commit -m "feat(update): безопасно синхронизировать Compose"
```

### Task 2: Compose Download and Fresh-Process Dispatch

**Files:**
- Modify: `reviewer/update_lifecycle.py`
- Modify: `tests/test_update_lifecycle.py`

- [ ] **Step 1: Add failing tests for HTTPS download and subprocess output capture**

```python
from contextlib import nullcontext
from types import SimpleNamespace

from reviewer.update_lifecycle import download_compose, run_fresh_artifact_refresh


def test_download_compose_uses_canonical_url_and_no_cache_headers():
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["cache"] = request.headers["Cache-control"]
        seen["timeout"] = timeout
        return nullcontext(SimpleNamespace(read=lambda: b"services: {}\n"))

    assert download_compose(opener=opener, timeout=7) == b"services: {}\n"
    assert seen == {
        "url": (
            "https://raw.githubusercontent.com/mimfort/rag_for_git/"
            "main/docker-compose.yml"
        ),
        "cache": "no-cache, no-store",
        "timeout": 7,
    }


def test_run_fresh_artifact_refresh_invokes_reviewer_hidden_phase():
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="compose current\n", stderr="")

    result = run_fresh_artifact_refresh(
        which=lambda name: "/tools/reviewer" if name == "reviewer" else None,
        run=run,
    )

    assert calls == [
        (
            ["/tools/reviewer", "update", "--refresh-artifacts"],
            {"capture_output": True, "text": True},
        )
    ]
    assert result.returncode == 0
    assert result.stdout == "compose current\n"


def test_run_fresh_artifact_refresh_reports_missing_executable():
    result = run_fresh_artifact_refresh(which=lambda name: None)

    assert result.returncode == 127
    assert "reviewer не найден" in result.stderr
```

- [ ] **Step 2: Run the new tests and verify RED**

Run: `.venv/bin/pytest tests/test_update_lifecycle.py -q`

Expected: import fails for `download_compose` and `run_fresh_artifact_refresh`.

- [ ] **Step 3: Implement download and fresh-process helpers**

```python
import shutil
import subprocess
import urllib.request
from collections.abc import Callable


@dataclass(frozen=True)
class RefreshProcessResult:
    returncode: int
    stdout: str
    stderr: str


def download_compose(
    *,
    opener: Callable = urllib.request.urlopen,
    timeout: int = 30,
) -> bytes:
    request = urllib.request.Request(
        COMPOSE_URL,
        headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"},
    )
    with opener(request, timeout=timeout) as response:
        return response.read()


def run_fresh_artifact_refresh(
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable = subprocess.run,
) -> RefreshProcessResult:
    executable = which("reviewer")
    if executable is None:
        return RefreshProcessResult(127, "", "reviewer не найден в PATH")
    result = run(
        [executable, "update", "--refresh-artifacts"],
        capture_output=True,
        text=True,
    )
    return RefreshProcessResult(
        result.returncode,
        result.stdout or "",
        result.stderr or "",
    )
```

- [ ] **Step 4: Run lifecycle tests and Ruff**

Run: `.venv/bin/pytest tests/test_update_lifecycle.py -q && .venv/bin/ruff check reviewer/update_lifecycle.py tests/test_update_lifecycle.py`

Expected: all lifecycle tests pass and Ruff exits 0.

- [ ] **Step 5: Commit download and process dispatch**

```bash
git add reviewer/update_lifecycle.py tests/test_update_lifecycle.py
git commit -m "feat(update): запускать обновлённый CLI отдельным процессом"
```

### Task 3: Two-Phase CLI and Explicit uvx Bootstrap

**Files:**
- Modify: `reviewer/entrypoints/cli.py:51,1486-1530`
- Modify: `tests/entrypoints/test_update_command.py`

- [ ] **Step 1: Replace exact legacy-output tests with failing lifecycle assertions**

```python
def test_update_uv_tool_upgrade_runs_artifacts_in_fresh_process(monkeypatch):
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.3", "/usr/bin/uv")
    refreshed = Mock(return_value=SimpleNamespace(returncode=0, stdout="artifacts ok\n", stderr=""))
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(cli_mod, "check_latest", lambda value: VersionCheck(value, "0.4.4", True))
    monkeypatch.setattr(cli_mod, "upgrade_uv_tool", lambda value: UpgradeResult(0, ""))
    monkeypatch.setattr(cli_mod, "run_fresh_artifact_refresh", refreshed)

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0, result.output
    assert "Доступна новая версия: 0.4.3 → 0.4.4" in result.output
    assert "artifacts ok" in result.output
    refreshed.assert_called_once_with()


def test_update_uv_tool_stops_before_artifacts_when_upgrade_fails(monkeypatch):
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.3", "/usr/bin/uv")
    refreshed = Mock()
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(cli_mod, "check_latest", lambda value: VersionCheck(value, "0.4.4", True))
    monkeypatch.setattr(cli_mod, "upgrade_uv_tool", lambda value: UpgradeResult(1, "registry unavailable"))
    monkeypatch.setattr(cli_mod, "run_fresh_artifact_refresh", refreshed)

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code != 0
    assert "registry unavailable" in result.output
    refreshed.assert_not_called()


def test_update_current_version_refreshes_artifacts_in_process(monkeypatch):
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.4", "/usr/bin/uv")
    refresh = Mock()
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(cli_mod, "check_latest", lambda value: VersionCheck(value, "0.4.4", False))
    monkeypatch.setattr(cli_mod, "_refresh_update_artifacts", refresh)

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0, result.output
    refresh.assert_called_once()


def test_update_uvx_upgrade_tool_is_explicit(monkeypatch):
    info = InstallationInfo(InstallMode.UVX, "0.4.4", "/usr/bin/uv")
    upgrade = Mock(return_value=UpgradeResult(0, ""))
    refresh = Mock()
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(cli_mod, "check_latest", lambda value: VersionCheck(value, "0.4.4", False))
    monkeypatch.setattr(cli_mod, "upgrade_uv_tool", upgrade)
    monkeypatch.setattr(cli_mod, "_refresh_update_artifacts", refresh)

    regular = CliRunner().invoke(cli_mod.cli, ["update"])
    bootstrap = CliRunner().invoke(cli_mod.cli, ["update", "--upgrade-tool"])

    assert regular.exit_code == 0, regular.output
    assert bootstrap.exit_code == 0, bootstrap.output
    upgrade.assert_called_once_with(info)
    assert refresh.call_count == 2


def test_update_editable_refreshes_artifacts_without_touching_source(monkeypatch):
    info = InstallationInfo(InstallMode.EDITABLE, "0.4.4", "/usr/bin/uv")
    refresh = Mock()
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(cli_mod, "_refresh_update_artifacts", refresh)

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0, result.output
    assert "git pull && pip install -e ." in result.output
    refresh.assert_called_once()
```

- [ ] **Step 2: Run CLI tests and verify RED**

Run: `.venv/bin/pytest tests/entrypoints/test_update_command.py -q`

Expected: failures mention missing `--upgrade-tool`, `_refresh_update_artifacts`, and `run_fresh_artifact_refresh` dispatch.

- [ ] **Step 3: Implement hidden phase, bootstrap option, and fresh-process result handling**

```python
from reviewer.update_lifecycle import (
    download_compose,
    run_fresh_artifact_refresh,
    sync_compose_file,
)


@cli.command()
@click.option(
    "--upgrade-tool",
    is_flag=True,
    help="обновить существующую persistent uv tool installation из latest uvx",
)
@click.option("--refresh-artifacts", is_flag=True, hidden=True)
@click.pass_context
def update(ctx: click.Context, upgrade_tool: bool, refresh_artifacts: bool) -> None:
    """Обновить пакет, AI-client integrations, скилы и Compose-файл."""
    if refresh_artifacts:
        _refresh_update_artifacts(ctx)
        return

    installation = detect_installation()
    if installation.mode is InstallMode.EDITABLE:
        click.echo(f"Режим: dev (editable) | Версия: {installation.current}")
        click.echo("Исходники не изменены. Для обновления: git pull && pip install -e .")
        _refresh_update_artifacts(ctx)
        return

    mode = (
        "uv tool (постоянная)"
        if installation.mode is InstallMode.UV_TOOL
        else "uvx (временная)"
    )
    click.echo(f"Режим: {mode} | Версия: {installation.current}")
    tool_upgraded = False
    version_check = check_latest(installation)
    if version_check.latest is None:
        click.echo("Не удалось получить информацию с PyPI. Обновляю доступные artifacts.")
    elif not version_check.current_valid:
        click.echo("Не удалось определить корректную текущую версию; пакет не изменён.")
    elif version_check.update_available:
        click.echo(
            f"Доступна новая версия: {installation.current} → {version_check.latest}"
        )
        if installation.mode is InstallMode.UV_TOOL or upgrade_tool:
            result = upgrade_uv_tool(installation)
            if result.returncode != 0:
                raise click.ClickException(f"Ошибка uv tool upgrade: {result.stderr}")
            tool_upgraded = True
            click.echo("✓ Python package обновлён.")
            if installation.mode is InstallMode.UV_TOOL:
                fresh = run_fresh_artifact_refresh()
                if fresh.stdout:
                    click.echo(fresh.stdout, nl=not fresh.stdout.endswith("\n"))
                if fresh.returncode != 0:
                    detail = fresh.stderr.strip() or "неизвестная ошибка"
                    raise click.ClickException(
                        f"Пакет обновлён, artifact refresh завершился ошибкой: {detail}"
                    )
                return
    elif installation.current != "?":
        click.echo(f"Версия актуальна: {installation.current}.")

    if upgrade_tool and installation.mode is InstallMode.UVX and not tool_upgraded:
        result = upgrade_uv_tool(installation)
        if result.returncode != 0:
            raise click.ClickException(f"Ошибка uv tool upgrade: {result.stderr}")
        click.echo("✓ Persistent uv tool installation обновлена.")
    _refresh_update_artifacts(ctx)
```

- [ ] **Step 4: Run update CLI tests and fix output assertions without weakening behavior checks**

Run: `.venv/bin/pytest tests/entrypoints/test_update_command.py -q`

Expected: all update-command tests pass; the existing UVX safety test proves a regular uvx invocation never calls `upgrade_uv_tool`.

- [ ] **Step 5: Commit package/process orchestration**

```bash
git add reviewer/entrypoints/cli.py tests/entrypoints/test_update_command.py
git commit -m "feat(update): объединить package и artifact lifecycle"
```

### Task 4: Detected Integrations and Partial-Failure Reporting

**Files:**
- Modify: `reviewer/entrypoints/cli.py`
- Modify: `tests/entrypoints/test_update_command.py`

- [ ] **Step 1: Add failing artifact-phase tests**

```python
from reviewer.update_lifecycle import ComposeSyncResult


def test_refresh_artifacts_updates_compose_and_skips_absent_clients(monkeypatch, tmp_path):
    target = tmp_path / "docker-compose.yml"
    install_call = Mock()
    monkeypatch.setattr(cli_mod, "download_compose", lambda: b"services: {}\n")
    monkeypatch.setattr(
        cli_mod,
        "sync_compose_file",
        lambda content: ComposeSyncResult("created", target),
    )
    monkeypatch.setattr(cli_mod, "_has_detected_clients", lambda: False)
    monkeypatch.setattr(cli_mod, "_install_detected_clients", install_call)

    result = CliRunner().invoke(cli_mod.cli, ["update", "--refresh-artifacts"])

    assert result.exit_code == 0, result.output
    assert f"Compose создан: {target}" in result.output
    assert "AI-клиенты не обнаружены" in result.output
    install_call.assert_not_called()


def test_refresh_artifacts_preserves_modified_compose_and_updates_clients(monkeypatch, tmp_path):
    target = tmp_path / "docker-compose.yml"
    install_call = Mock()
    monkeypatch.setattr(cli_mod, "download_compose", lambda: b"services: {}\n")
    monkeypatch.setattr(
        cli_mod,
        "sync_compose_file",
        lambda content: ComposeSyncResult("preserved", target),
    )
    monkeypatch.setattr(cli_mod, "_has_detected_clients", lambda: True)
    monkeypatch.setattr(cli_mod, "_install_detected_clients", install_call)

    result = CliRunner().invoke(cli_mod.cli, ["update", "--refresh-artifacts"])

    assert result.exit_code == 0, result.output
    assert "не перезаписан" in result.output
    install_call.assert_called_once()


def test_refresh_artifacts_attempts_clients_after_compose_download_failure(monkeypatch):
    install_call = Mock()
    monkeypatch.setattr(cli_mod, "download_compose", lambda: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(cli_mod, "_has_detected_clients", lambda: True)
    monkeypatch.setattr(cli_mod, "_install_detected_clients", install_call)

    result = CliRunner().invoke(cli_mod.cli, ["update", "--refresh-artifacts"])

    assert result.exit_code != 0
    assert "Compose: OSError" in result.output
    install_call.assert_called_once()


def test_refresh_artifacts_aggregates_integration_failure(monkeypatch, tmp_path):
    target = tmp_path / "docker-compose.yml"
    monkeypatch.setattr(cli_mod, "download_compose", lambda: b"services: {}\n")
    monkeypatch.setattr(
        cli_mod,
        "sync_compose_file",
        lambda content: ComposeSyncResult("current", target),
    )
    monkeypatch.setattr(cli_mod, "_has_detected_clients", lambda: True)
    monkeypatch.setattr(
        cli_mod,
        "_install_detected_clients",
        lambda ctx: (_ for _ in ()).throw(click.ClickException("Codex failed")),
    )

    result = CliRunner().invoke(cli_mod.cli, ["update", "--refresh-artifacts"])

    assert result.exit_code != 0
    assert "Integrations: Codex failed" in result.output
```

- [ ] **Step 2: Run artifact-phase tests and verify RED**

Run: `.venv/bin/pytest tests/entrypoints/test_update_command.py -q`

Expected: failures mention missing `_has_detected_clients`, `_install_detected_clients`, and artifact status/error output.

- [ ] **Step 3: Implement reuse of the existing installer and aggregate phase errors**

```python
def _has_detected_clients() -> bool:
    from reviewer import install as inst

    return bool(inst.detect_installed()) or _shutil.which("claude") is not None


def _install_detected_clients(ctx: click.Context) -> None:
    ctx.invoke(
        install,
        client=None,
        all_clients=True,
        list_clients=False,
        path_opt=None,
        pin=None,
        no_latest=False,
        no_skills=False,
        dry_run=False,
    )


def _print_compose_result(result) -> None:
    messages = {
        "created": f"✓ Compose создан: {result.path}",
        "adopted": f"✓ Compose принят под управление: {result.path}",
        "current": f"✓ Compose актуален: {result.path}",
        "updated": f"✓ Compose обновлён: {result.path}",
        "preserved": (
            f"⚠ Compose не перезаписан: обнаружены пользовательские изменения в "
            f"{result.path}"
        ),
    }
    click.echo(messages[result.action])


def _refresh_update_artifacts(ctx: click.Context) -> None:
    errors: list[str] = []
    try:
        compose = sync_compose_file(download_compose())
        _print_compose_result(compose)
    except Exception as exc:  # noqa: BLE001 - independent phases must continue
        errors.append(f"Compose: {type(exc).__name__}")

    if _has_detected_clients():
        try:
            _install_detected_clients(ctx)
        except click.ClickException as exc:
            errors.append(f"Integrations: {exc.format_message()}")
    else:
        click.echo("AI-клиенты не обнаружены; integration refresh пропущен.")

    if errors:
        raise click.ClickException("Обновление завершено частично: " + "; ".join(errors))
    click.echo("Обновление завершено. Откройте New Chat/new CLI session; в IDE — Reload Window.")
```

- [ ] **Step 4: Run update/install regression tests**

Run: `.venv/bin/pytest tests/entrypoints/test_update_command.py tests/install/test_install.py tests/install/test_install_skills_cli.py tests/install/test_claude_install.py tests/install/test_codex_install.py -q`

Expected: all selected tests pass; generic and native lifecycle behavior remains covered by its existing contract tests.

- [ ] **Step 5: Commit integration refresh**

```bash
git add reviewer/entrypoints/cli.py tests/entrypoints/test_update_command.py
git commit -m "feat(update): обновлять integrations и скилы"
```

### Task 5: Launcher and Bilingual Documentation

**Files:**
- Modify: `reviewer/launcher/metadata.py:107-116`
- Modify: `tests/launcher/test_catalog.py:152-157`
- Modify: `README.md:25-55,102-135,236-287`
- Modify: `README.ru.md:25-55,102-135,239-290`
- Modify: `tests/docs/test_readme_onboarding.py:34-56,193-209`

- [ ] **Step 1: Write failing launcher and README contract tests**

```python
def test_update_metadata_discloses_complete_mutating_lifecycle():
    update = next(item for item in build_catalog(cli) if item.path == ("update",))

    assert update.effects == (Effect.READ, Effect.NETWORK, Effect.WRITE)
    assert "AI-client integrations" in update.details
    assert "Compose" in update.details
    assert "не перезаписывает пользовательские изменения" in update.details
```

Replace `test_quick_start_downloads_compose_and_indexes_before_checking` with:

```python
def test_quick_start_uses_unified_update_and_indexes_before_checking():
    for filename, heading in (
        ("README.md", "## Try reviewer"),
        ("README.ru.md", "## Попробовать reviewer"),
    ):
        section = _section(_read(filename), heading)
        assert "curl -o ~/.config/rag-reviewer/docker-compose.yml" not in section
        _assert_in_order(
            section,
            (
                "uv tool install rag-reviewer",
                "reviewer update",
                "docker compose -f ~/.config/rag-reviewer/docker-compose.yml up -d",
                "reviewer init",
                "reviewer index /path/to/repo --ref main",
                "reviewer check",
                "reviewer status /path/to/repo --branch main --json",
            ),
        )


def test_readmes_document_unified_update_contract():
    markers = (
        "uvx --refresh --from rag-reviewer@latest reviewer update --upgrade-tool",
        ".reviewer-update.json",
        "New Chat",
        "Reload Window",
    )
    for filename in ("README.md", "README.ru.md"):
        text = _read(filename)
        for marker in markers:
            assert marker in text, (filename, marker)
```

- [ ] **Step 2: Run documentation/launcher tests and verify RED**

Run: `.venv/bin/pytest tests/launcher/test_catalog.py tests/docs/test_readme_onboarding.py -q`

Expected: old metadata wording and manual `curl` onboarding fail the new assertions.

- [ ] **Step 3: Update launcher metadata**

```python
("update",): CommandPresentation(
    summary="Обновить reviewer и integrations",
    details=(
        "Обновляет persistent uv tool package, обнаруженные AI-client integrations, "
        "скилы и управляемый Compose; не перезаписывает пользовательские изменения Compose."
    ),
    effects=(Effect.READ, Effect.NETWORK, Effect.WRITE),
    scenarios=("Полное обновление rag-reviewer",),
    keywords=("pypi", "version", "upgrade", "skills", "compose"),
    special_action="check_update",
),
```

- [ ] **Step 4: Rewrite both onboarding routes and update sections in parallel**

Use these exact command sequences in both languages:

```bash
uv tool install rag-reviewer
reviewer update
docker compose -f ~/.config/rag-reviewer/docker-compose.yml up -d
reviewer init
```

```bash
# One-time bootstrap from 0.4.3
uvx --refresh --from rag-reviewer@latest reviewer update --upgrade-tool

# Every later update
reviewer update
```

Document that `.reviewer-update.json` records only the hash of reviewer-managed Compose content;
modified Compose is preserved with a warning; package/client/plugin/skills/Compose phases run in one
command; Docker services and volumes are not restarted or removed; users reopen New Chat/new CLI
session and Reload Window in IDEs.

- [ ] **Step 5: Run README and launcher tests**

Run: `.venv/bin/pytest tests/launcher/test_catalog.py tests/docs/test_readme_onboarding.py -q`

Expected: all selected tests pass with bilingual section order, links, and markers intact.

- [ ] **Step 6: Commit user-facing lifecycle documentation**

```bash
git add reviewer/launcher/metadata.py tests/launcher/test_catalog.py README.md README.ru.md tests/docs/test_readme_onboarding.py
git commit -m "docs(update): описать обновление одной командой"
```

### Task 6: Patch Release Metadata and Full Verification

**Files:**
- Modify: `pyproject.toml:3`
- Modify: `uv.lock`
- Modify: `plugin/.claude-plugin/plugin.json:3`
- Modify: `.codex-plugin/plugin.json:3`
- Modify: `plugin/.codex-plugin/plugin.json:3`

- [ ] **Step 1: Bump the Python and Claude plugin version to 0.4.4**

```toml
[project]
name = "rag-reviewer"
version = "0.4.4"
```

```json
{
  "name": "rag-reviewer",
  "version": "0.4.4",
  "description": "Agentic PR review: hybrid RAG + code graph via MCP, review skills for Claude Code"
}
```

- [ ] **Step 2: Regenerate lock and Codex manifests with repository scripts**

Run: `uv lock && python scripts/update_codex_plugin_manifest.py`

Expected: `uv.lock` records local package version 0.4.4 and both Codex manifest versions match the regular expression `0\.4\.4\+codex\.[0-9a-f]{12}`.

- [ ] **Step 3: Verify generated metadata before committing**

Run: `uv lock --check && python scripts/update_codex_plugin_manifest.py --check`

Expected: both commands exit 0 without a diff.

- [ ] **Step 4: Run focused and full verification**

Run: `.venv/bin/pytest tests/test_update_lifecycle.py tests/entrypoints/test_update_command.py tests/docs/test_readme_onboarding.py tests/launcher/test_catalog.py -q`

Expected: all focused tests pass.

Run: `.venv/bin/ruff check .`

Expected: Ruff exits 0.

Run: `.venv/bin/pytest -q`

Expected: the full non-integration suite passes with no external or localhost socket access.

- [ ] **Step 5: Build and smoke-test the wheel in an isolated uvx environment**

Run: `uv build`

Expected: a 0.4.4 wheel and source distribution are created in `dist/`.

Run: `uvx --refresh --from dist/rag_reviewer-0.4.4-py3-none-any.whl reviewer update --help`

Expected: help exits 0 and lists `--upgrade-tool`; it does not run update or touch user configuration.

- [ ] **Step 6: Commit release metadata**

```bash
git add pyproject.toml uv.lock plugin/.claude-plugin/plugin.json .codex-plugin/plugin.json plugin/.codex-plugin/plugin.json
git commit -m "chore(release): версия 0.4.4 и пересборка манифестов"
```

### Task 7: Deliver Through dev and main

**Files:**
- No additional code files.

- [ ] **Step 1: Review the complete branch diff and recent commits**

Run: `git status --short && git diff origin/dev...HEAD --check && git log --oneline origin/dev..HEAD`

Expected: only intended commits are listed and the worktree is clean.

- [ ] **Step 2: Push a feature branch and open a PR to dev**

Run: `git switch -c feat/unified-update && git push -u origin feat/unified-update`

Run: `gh pr create --base dev --head feat/unified-update --title "feat(update): обновление одной командой" --body "Обновляет package, integrations, skills и Compose через reviewer update без перезаписи пользовательского Compose."`

Expected: GitHub returns the PR URL. Prepare the body outside the repository or use `gh pr create --body` so no transient file is committed.

- [ ] **Step 3: Wait for required checks and merge to dev**

Run: `gh pr checks "$(gh pr view --json number --jq .number)" --watch`

Expected: all required checks pass.

Run: `gh pr merge "$(gh pr view --json number --jq .number)" --merge --delete-branch`

Expected: the PR is merged into `dev` without force-push.

- [ ] **Step 4: Open and merge the dev-to-main release PR**

Run: `gh pr create --base main --head dev --title "chore(release): rag-reviewer 0.4.4" --body "Release unified update lifecycle from dev."`

Run: `gh pr checks "$(gh pr view dev --json number --jq .number)" --watch`

Expected: all required checks pass.

Run: `gh pr merge "$(gh pr view dev --json number --jq .number)" --merge`

Expected: `main` contains version 0.4.4 and triggers publish/plugin workflows.

- [ ] **Step 5: Verify release workflows and PyPI artifact**

Run: `gh run list --branch main --limit 10`

Expected: Publish to PyPI, Codex plugin, HOL scanner, and tests associated with the main merge are successful.

Run: `uvx --refresh --from rag-reviewer==0.4.4 reviewer update --help`

Expected: the command resolves from PyPI and help lists `--upgrade-tool`.

- [ ] **Step 6: Return the local checkout to updated dev**

Run: `git switch dev && git pull --ff-only origin dev && git status --short --branch`

Expected: `dev` matches `origin/dev` and the worktree is clean.
