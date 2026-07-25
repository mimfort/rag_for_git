from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import verify_launcher_distribution as distribution
from scripts.verify_launcher_distribution import Command, run_command, verify_distribution


def _recording_runner(calls: list[Command]):
    def run(command: Command) -> None:
        calls.append(command)

    return run


def _wheel(wheel_dir: Path, name: str = "rag_reviewer-0.4.0-py3-none-any.whl") -> Path:
    wheel_dir.mkdir(exist_ok=True)
    wheel = wheel_dir / name
    wheel.write_bytes(b"wheel")
    return wheel


def test_distribution_check_uses_isolated_uv_dirs_and_outside_checkout(tmp_path):
    wheel_dir = tmp_path / "dist"
    wheel = _wheel(wheel_dir)
    calls: list[Command] = []

    verify_distribution(wheel_dir, runner=_recording_runner(calls))

    install = calls[0]
    assert install.argv[:4] == ("uv", "tool", "install", "--force")
    assert install.argv[-1] == str(wheel.resolve())
    assert install.env["UV_TOOL_DIR"].startswith(str(tmp_path))
    assert install.env["UV_TOOL_BIN_DIR"].startswith(str(tmp_path))
    assert all(call.cwd != Path.cwd() for call in calls)
    assert any(call.argv[:2] == ("uvx", "--from") for call in calls)


def test_distribution_check_uses_windows_executable_suffix(tmp_path, monkeypatch):
    wheel_dir = tmp_path / "dist"
    _wheel(wheel_dir)
    calls: list[Command] = []
    monkeypatch.setattr(
        distribution,
        "os",
        SimpleNamespace(name="nt", environ=os.environ),
    )

    verify_distribution(wheel_dir, runner=_recording_runner(calls))

    assert calls[1].argv[0].endswith("reviewer.exe")


@pytest.mark.parametrize("wheel_count", [0, 2])
def test_distribution_check_requires_exactly_one_wheel(tmp_path, wheel_count):
    wheel_dir = tmp_path / "dist"
    wheel_dir.mkdir()
    for index in range(wheel_count):
        _wheel(wheel_dir, f"rag_reviewer-{index}-py3-none-any.whl")

    with pytest.raises(
        RuntimeError,
        match=rf"ожидался один wheel, найдено: {wheel_count}",
    ):
        verify_distribution(wheel_dir, runner=lambda command: None)


def test_run_command_passes_tokenized_argv_without_shell(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))

    monkeypatch.setattr(distribution.subprocess, "run", fake_run)
    command = Command(
        argv=("uvx", "--from", "/tmp/package.whl", "reviewer", "--help"),
        cwd=tmp_path,
        env={"UV_CACHE_DIR": str(tmp_path / "cache")},
    )

    run_command(command)

    assert calls == [
        (
            ["uvx", "--from", "/tmp/package.whl", "reviewer", "--help"],
            {
                "cwd": tmp_path,
                "env": {"UV_CACHE_DIR": str(tmp_path / "cache")},
                "check": True,
            },
        )
    ]
