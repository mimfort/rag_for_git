import json
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


def test_detects_editable_without_running_uv_tool_list():
    run = Mock()

    info = detect_installation(
        distribution=_Distribution(editable=True),
        which=lambda name: "/usr/bin/uv",
        run=run,
    )

    assert info.mode is InstallMode.EDITABLE
    assert info.current == "0.4.0"
    run.assert_not_called()


def test_detects_uv_tool_from_read_only_tool_list():
    run = Mock(return_value=SimpleNamespace(stdout="rag-reviewer v0.4.0\n"))

    info = detect_installation(
        distribution=_Distribution(editable=False),
        which=lambda name: "/usr/bin/uv",
        run=run,
    )

    assert info == InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv")
    run.assert_called_once_with(
        ["/usr/bin/uv", "tool", "list"], capture_output=True, text=True
    )


def test_detects_uvx_when_tool_is_not_installed():
    run = Mock(return_value=SimpleNamespace(stdout="another-tool v1.0.0\n- another\n"))

    info = detect_installation(
        distribution=_Distribution(editable=False),
        which=lambda name: "/usr/bin/uv",
        run=run,
    )

    assert info == InstallationInfo(InstallMode.UVX, "0.4.0", "/usr/bin/uv")


def test_does_not_detect_similarly_named_uv_tool():
    run = Mock(return_value=SimpleNamespace(stdout="rag-reviewer-extra v1.0.0\n"))

    info = detect_installation(
        distribution=_Distribution(editable=False),
        which=lambda name: "/usr/bin/uv",
        run=run,
    )

    assert info == InstallationInfo(InstallMode.UVX, "0.4.0", "/usr/bin/uv")


def test_falls_back_to_uvx_when_uv_tool_list_fails():
    def failing_run(*args, **kwargs):
        raise OSError("uv исчез")

    info = detect_installation(
        distribution=_Distribution(editable=False),
        which=lambda name: "/usr/bin/uv",
        run=failing_run,
    )

    assert info == InstallationInfo(InstallMode.UVX, "0.4.0", "/usr/bin/uv")


def test_falls_back_to_uvx_when_uv_tool_list_returns_error():
    run = Mock(
        return_value=SimpleNamespace(
            returncode=1,
            stdout="rag-reviewer v0.4.0\nошибка: список инструментов недоступен\n",
        )
    )

    info = detect_installation(
        distribution=_Distribution(editable=False),
        which=lambda name: "/usr/bin/uv",
        run=run,
    )

    assert info == InstallationInfo(InstallMode.UVX, "0.4.0", "/usr/bin/uv")


def test_ignores_whitespace_only_lines_in_uv_tool_list():
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="  \n\trag-reviewer v0.4.0\n"))

    info = detect_installation(
        distribution=_Distribution(editable=False),
        which=lambda name: "/usr/bin/uv",
        run=run,
    )

    assert info == InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv")


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
    run = Mock(
        return_value=SimpleNamespace(returncode=1, stderr="не удалось обновить\n".encode())
    )

    result = upgrade_uv_tool(
        InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv"),
        run=run,
    )

    assert result.returncode == 1
    assert result.stderr == "не удалось обновить"
    run.assert_called_once_with(
        ["/usr/bin/uv", "tool", "upgrade", "rag-reviewer"], capture_output=True
    )
