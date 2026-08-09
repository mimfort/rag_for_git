from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


STATE_NAME = ".reviewer-update.json"
COMPOSE_URL = (
    "https://raw.githubusercontent.com/mimfort/rag_for_git/main/docker-compose.yml"
)


@dataclass(frozen=True)
class ComposeSyncResult:
    action: Literal["created", "adopted", "current", "updated", "preserved"]
    path: Path


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
    python_executable: str = sys.executable,
    run: Callable = subprocess.run,
) -> RefreshProcessResult:
    if not python_executable:
        return RefreshProcessResult(127, "", "Python executable не найден")
    result = run(
        [
            python_executable,
            "-c",
            "from reviewer.entrypoints.launcher import main; main()",
            "update",
            "--refresh-artifacts",
        ],
        capture_output=True,
        text=True,
    )
    return RefreshProcessResult(
        result.returncode,
        result.stdout or "",
        result.stderr or "",
    )


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


def _write_state(path: Path, digest: str) -> None:
    content = json.dumps(
        {"docker_compose_sha256": digest}, indent=2, ensure_ascii=False
    ).encode("utf-8") + b"\n"
    _atomic_write(path, content)


def _read_recorded_hash(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = payload.get("docker_compose_sha256") if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value.startswith("sha256:") else None


def sync_compose_file(
    content: bytes,
    *,
    config_dir: Path | None = None,
) -> ComposeSyncResult:
    directory = config_dir or default_config_dir()
    target = directory / "docker-compose.yml"
    state_path = directory / STATE_NAME
    incoming_hash = _digest(content)
    if target.is_symlink():
        return ComposeSyncResult("preserved", target)
    if target.exists():
        existing = target.read_bytes()
        existing_hash = _digest(existing)
    else:
        existing_hash = None
    if existing_hash == incoming_hash:
        action = "current" if _read_recorded_hash(state_path) == incoming_hash else "adopted"
        _write_state(state_path, incoming_hash)
        return ComposeSyncResult(action, target)
    recorded_hash = _read_recorded_hash(state_path)
    if existing_hash is not None and recorded_hash == existing_hash:
        latest = target.read_bytes()
        if latest != existing:
            if latest == content:
                _write_state(state_path, incoming_hash)
                return ComposeSyncResult("current", target)
            return ComposeSyncResult("preserved", target)
        _atomic_write(target, content)
        _write_state(state_path, incoming_hash)
        return ComposeSyncResult("updated", target)
    if existing_hash is not None:
        return ComposeSyncResult("preserved", target)
    _atomic_write(target, content)
    _write_state(state_path, incoming_hash)
    return ComposeSyncResult("created", target)
