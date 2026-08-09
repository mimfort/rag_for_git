from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
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


def find_uv_tool_python(
    uv_executable: str | None,
    *,
    run: Callable = subprocess.run,
    system: str | None = None,
) -> str | None:
    if uv_executable is None:
        return None
    try:
        result = run(
            [uv_executable, "tool", "dir"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    environment = Path(result.stdout.strip()) / "rag-reviewer"
    if (system or platform.system()) == "Windows":
        python = environment / "Scripts" / "python.exe"
    else:
        python = environment / "bin" / "python"
    return str(python) if python.is_file() else None


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
            "-I",
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


def _atomic_create(path: Path, content: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        temporary = Path(tmp.name)
    try:
        os.link(temporary, path)
    except FileExistsError:
        return False
    finally:
        temporary.unlink(missing_ok=True)
    return True


@contextmanager
def _update_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".reviewer-update.lock"
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock_file.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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
    with _update_lock(directory):
        return _sync_compose_file_unlocked(content, directory)


def _sync_compose_file_unlocked(content: bytes, directory: Path) -> ComposeSyncResult:
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
    if not _atomic_create(target, content):
        return ComposeSyncResult("preserved", target)
    _write_state(state_path, incoming_hash)
    return ComposeSyncResult("created", target)
