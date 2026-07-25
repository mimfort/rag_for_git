import json
import shutil
import subprocess
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata


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


def _version_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)


def detect_installation(
    *,
    distribution: object | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable = subprocess.run,
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

    is_tool = False
    if uv:
        tool_list = run([uv, "tool", "list"], capture_output=True, text=True)
        is_tool = "rag-reviewer" in tool_list.stdout

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
    result = run(
        [info.uv_executable, "tool", "upgrade", "rag-reviewer"], capture_output=True
    )
    return UpgradeResult(result.returncode, result.stderr.decode().strip())
