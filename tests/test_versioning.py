import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from reviewer.versioning import (
    InstallMode,
    InstallationInfo,
    check_latest,
    detect_installation,
    upgrade_uv_tool,
)


class _Distribution:
    version = "0.4.0"

    def __init__(self, *, editable: bool) -> None:
        self._editable = editable

    def read_text(self, name: str) -> str | None:
        assert name == "direct_url.json"
        return json.dumps({"dir_info": {"editable": self._editable}})


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


class _Opener:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.timeout: int | None = None

    def __call__(self, request, *, timeout: int):
        self.timeout = timeout
        return _Response(self.payload)


def test_detects_editable_without_running_uv_discovery():
    run = Mock()

    info = detect_installation(
        distribution=_Distribution(editable=True),
        which=lambda name: "/usr/bin/uv",
        run=run,
    )

    assert info.mode is InstallMode.EDITABLE
    assert info.current == "0.4.0"
    run.assert_not_called()


def test_unrelated_persistent_tool_does_not_turn_current_uvx_into_uv_tool(tmp_path):
    tool_dir = tmp_path / "uv-tools"
    (tool_dir / "rag-reviewer").mkdir(parents=True)
    uvx_prefix = tmp_path / "uv-cache" / "archive-v0" / "current"
    distribution_location = uvx_prefix / "lib" / "python3.12" / "site-packages"
    distribution_location.mkdir(parents=True)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=f"{tool_dir}\n"))

    info = detect_installation(
        distribution=_Distribution(editable=False),
        which=lambda name: str(tmp_path / "bin" / "uv"),
        run=run,
        current_prefix=uvx_prefix,
        distribution_location=distribution_location,
    )

    assert info.mode is InstallMode.UVX
    run.assert_called_once_with(
        [str(tmp_path / "bin" / "uv"), "tool", "dir"],
        capture_output=True,
        text=True,
        timeout=5,
    )


@pytest.mark.parametrize("matching_source", ["prefix", "distribution"])
def test_detects_current_uv_tool_environment_by_resolved_location(tmp_path, matching_source):
    tool_dir = tmp_path / "real-uv-tools"
    tool_environment = tool_dir / "rag-reviewer"
    tool_environment.mkdir(parents=True)
    outside_environment = tmp_path / "uv-cache" / "archive-v0" / "current"
    current_prefix = outside_environment
    distribution_location = outside_environment / "site-packages"
    if matching_source == "prefix":
        current_prefix = tool_environment / "Scripts" / ".."
    else:
        distribution_location = tool_environment / "lib" / "python3.12" / "site-packages"
        distribution_location.mkdir(parents=True)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=f"{tool_dir}\n"))

    info = detect_installation(
        distribution=_Distribution(editable=False),
        which=lambda name: str(tmp_path / "bin" / "uv"),
        run=run,
        current_prefix=current_prefix,
        distribution_location=distribution_location,
    )

    assert info == InstallationInfo(
        InstallMode.UV_TOOL,
        "0.4.0",
        str(tmp_path / "bin" / "uv"),
    )


def test_detects_uvx_when_uv_is_not_available(tmp_path):
    run = Mock()

    info = detect_installation(
        distribution=_Distribution(editable=False),
        which=lambda name: None,
        run=run,
        current_prefix=tmp_path / "uvx",
        distribution_location=tmp_path / "uvx" / "site-packages",
    )

    assert info == InstallationInfo(InstallMode.UVX, "0.4.0", None)
    run.assert_not_called()


def test_falls_back_to_uvx_when_uv_tool_dir_times_out(tmp_path):
    run = Mock(side_effect=subprocess.TimeoutExpired(["uv", "tool", "dir"], 5))

    info = detect_installation(
        distribution=_Distribution(editable=False),
        which=lambda name: str(tmp_path / "bin" / "uv"),
        run=run,
        current_prefix=tmp_path / "uvx",
        distribution_location=tmp_path / "uvx" / "site-packages",
    )

    assert info.mode is InstallMode.UVX
    assert run.call_args.kwargs["timeout"] == 5


def test_falls_back_to_uvx_when_uv_tool_dir_cannot_start(tmp_path):
    run = Mock(side_effect=OSError("uv исчез"))

    info = detect_installation(
        distribution=_Distribution(editable=False),
        which=lambda name: str(tmp_path / "bin" / "uv"),
        run=run,
        current_prefix=tmp_path / "uvx",
        distribution_location=tmp_path / "uvx" / "site-packages",
    )

    assert info.mode is InstallMode.UVX
    assert run.call_args.kwargs["timeout"] == 5


def test_falls_back_to_uvx_when_uv_tool_dir_returns_error(tmp_path):
    tool_dir = tmp_path / "uv-tools"
    tool_environment = tool_dir / "rag-reviewer"
    tool_environment.mkdir(parents=True)
    run = Mock(return_value=SimpleNamespace(returncode=1, stdout=f"{tool_dir}\n"))

    info = detect_installation(
        distribution=_Distribution(editable=False),
        which=lambda name: str(tmp_path / "bin" / "uv"),
        run=run,
        current_prefix=tool_environment,
        distribution_location=tool_environment / "site-packages",
    )

    assert info.mode is InstallMode.UVX
    assert run.call_args.kwargs["timeout"] == 5


@pytest.mark.parametrize(
    ("latest", "update_available"),
    [("0.4.0", False), ("0.4.1", True), ("0.5.0", True)],
)
def test_check_latest_compares_versions(latest: str, update_available: bool):
    result = check_latest(
        InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv"),
        opener=_Opener({"info": {"version": latest}}),
    )

    assert result.latest == latest
    assert result.update_available is update_available


def test_check_latest_is_read_only_and_uses_timeout():
    opener = _Opener({"info": {"version": "0.5.0"}})

    result = check_latest(
        InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv"),
        opener=opener,
        timeout=5,
    )

    assert result.latest == "0.5.0"
    assert result.update_available is True
    assert opener.timeout == 5


def test_check_latest_returns_no_version_when_pypi_fails():
    def failing_opener(request, *, timeout: int):
        raise OSError("сеть недоступна")

    result = check_latest(
        InstallationInfo(InstallMode.UVX, "0.4.0", None),
        opener=failing_opener,
    )

    assert result.latest is None
    assert result.update_available is False


def test_upgrade_uv_tool_returns_subprocess_result():
    run = Mock(return_value=SimpleNamespace(returncode=1, stderr="не удалось обновить\n".encode()))

    result = upgrade_uv_tool(
        InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv"),
        run=run,
    )

    assert result.returncode == 1
    assert result.stderr == "не удалось обновить"
    run.assert_called_once_with(
        ["/usr/bin/uv", "tool", "upgrade", "rag-reviewer"], capture_output=True
    )
