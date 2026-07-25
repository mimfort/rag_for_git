import json
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata
from pathlib import Path


class InstallMode(StrEnum):
    EDITABLE = "editable"
    UV_TOOL = "uv_tool"
    UVX = "uvx"


@dataclass(frozen=True)
class InstallationInfo:
    mode: InstallMode
    current: str
    uv_executable: str | None


@dataclass(frozen=True)
class VersionCheck:
    installation: InstallationInfo
    latest: str | None
    update_available: bool


@dataclass(frozen=True)
class UpgradeResult:
    returncode: int
    stderr: str


_UV_DISCOVERY_TIMEOUT = 5


def _version_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)


def _resolved_path(value: str | Path | None) -> Path | None:
    """Нормализовать путь с учётом символических ссылок без требования существования."""
    if value is None:
        return None
    try:
        return Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _belongs_to_environment(value: str | Path | None, environment: Path) -> bool:
    """Проверить принадлежность пути окружению по границам компонентов."""
    path = _resolved_path(value)
    if path is None:
        return False
    try:
        path.relative_to(environment)
    except ValueError:
        return False
    return True


def detect_installation(
    *,
    distribution: object | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable = subprocess.run,
    current_prefix: str | Path | None = None,
    distribution_location: str | Path | None = None,
    timeout: int = _UV_DISCOVERY_TIMEOUT,
) -> InstallationInfo:
    """Определить способ установки без изменения окружения."""
    if distribution is None:
        try:
            current = metadata.version("rag-reviewer")
        except Exception:
            current = "?"
    else:
        current = distribution.version

    try:
        dist = distribution or metadata.Distribution.from_name("rag-reviewer")
    except Exception:
        dist = None

    try:
        direct_url_text = dist.read_text("direct_url.json") if dist else None
        direct_url = json.loads(direct_url_text) if direct_url_text else {}
        editable = direct_url.get("dir_info", {}).get("editable", False)
    except Exception:
        editable = False

    uv = which("uv")
    if editable:
        return InstallationInfo(InstallMode.EDITABLE, current, uv)

    if not uv:
        return InstallationInfo(InstallMode.UVX, current, None)

    if current_prefix is None:
        current_prefix = sys.prefix
    if distribution_location is None and dist is not None:
        try:
            distribution_location = dist.locate_file("")
        except Exception:
            distribution_location = None

    try:
        tool_dir_result = run(
            [uv, "tool", "dir"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return InstallationInfo(InstallMode.UVX, current, uv)

    if getattr(tool_dir_result, "returncode", 1) != 0:
        return InstallationInfo(InstallMode.UVX, current, uv)
    tool_dir = _resolved_path(getattr(tool_dir_result, "stdout", "").strip())
    if tool_dir is None:
        return InstallationInfo(InstallMode.UVX, current, uv)
    tool_environment = _resolved_path(tool_dir / "rag-reviewer")
    if tool_environment is None or not tool_environment.is_dir():
        return InstallationInfo(InstallMode.UVX, current, uv)

    is_tool = any(
        _belongs_to_environment(candidate, tool_environment)
        for candidate in (current_prefix, distribution_location)
    )
    mode = InstallMode.UV_TOOL if is_tool else InstallMode.UVX
    return InstallationInfo(mode, current, uv)


def check_latest(
    info: InstallationInfo,
    *,
    opener: Callable = urllib.request.urlopen,
    timeout: int = 10,
) -> VersionCheck:
    """Получить версию PyPI только HTTP-запросом."""
    try:
        request = urllib.request.Request(
            "https://pypi.org/pypi/rag-reviewer/json",
            headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"},
        )
        with opener(request, timeout=timeout) as response:
            latest = json.loads(response.read())["info"]["version"]
    except Exception:
        latest = None

    update_available = (
        latest is not None
        and info.current != "?"
        and _version_tuple(latest) > _version_tuple(info.current)
    )
    return VersionCheck(info, latest, update_available)


def upgrade_uv_tool(
    info: InstallationInfo,
    *,
    run: Callable = subprocess.run,
) -> UpgradeResult:
    """Обновить установленный через uv tool пакет."""
    if info.uv_executable is None:
        raise RuntimeError("uv не найден в PATH")
    result = run([info.uv_executable, "tool", "upgrade", "rag-reviewer"], capture_output=True)
    return UpgradeResult(result.returncode, result.stderr.decode().strip())
