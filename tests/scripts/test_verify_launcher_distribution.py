from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
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
    roots: list[Path] = []

    def observe(command: Command) -> None:
        assert command.cwd.exists()
        roots.append(command.cwd.parent)
        calls.append(command)

    verify_distribution(wheel_dir, runner=observe)

    install = calls[0]
    assert install.argv[:4] == ("uv", "tool", "install", "--force")
    assert install.argv[-1] == str(wheel.resolve())
    assert len(set(roots)) == 1
    assert all(not call.cwd.resolve().is_relative_to(distribution._CHECKOUT_ROOT) for call in calls)
    assert {
        Path(install.env[name]).parent
        for name in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_CACHE_DIR")
    } == {roots[0]}
    assert len(
        {
            install.env[name]
            for name in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_CACHE_DIR")
        }
    ) == 3
    assert any(call.argv[:2] == ("uvx", "--from") for call in calls)
    assert all(not root.exists() for root in roots)


def test_distribution_check_rejects_temp_candidate_inside_checkout(monkeypatch):
    created_roots: list[Path] = []
    parent_arguments: list[Path | None] = []

    with TemporaryDirectory(
        prefix="reviewer-temp-candidate-",
        dir=distribution._CHECKOUT_ROOT,
    ) as raw_inside:
        wheel_dir = Path(raw_inside) / "dist"
        _wheel(wheel_dir)
        calls: list[Command] = []

        def temporary_directory(*, prefix, dir=None):
            parent_arguments.append(dir)
            parent = Path(raw_inside) if dir is None else Path(dir)

            @contextmanager
            def managed():
                with TemporaryDirectory(prefix=prefix, dir=parent) as raw:
                    created_roots.append(Path(raw))
                    yield raw

            return managed()

        monkeypatch.setattr(distribution, "TemporaryDirectory", temporary_directory)

        verify_distribution(wheel_dir, runner=_recording_runner(calls))

        assert parent_arguments == [None, distribution._CHECKOUT_ROOT.parent]
        assert all(
            not call.cwd.resolve().is_relative_to(distribution._CHECKOUT_ROOT)
            for call in calls
        )
        assert all(not root.exists() for root in created_roots)


def test_distribution_check_does_not_use_external_wheel_parent_for_temp(
    tmp_path,
    monkeypatch,
):
    wheel_dir = tmp_path / "недоступный-источник" / "dist"
    wheel_dir.parent.mkdir()
    wheel = _wheel(wheel_dir)
    system_temp = tmp_path / "системный-temp"
    system_temp.mkdir()
    created_roots: list[Path] = []
    parent_arguments: list[Path | None] = []
    calls: list[Command] = []

    def temporary_directory(*, prefix, dir=None):
        parent_arguments.append(dir)
        if dir is not None:
            raise AssertionError("temp нельзя создавать рядом с external wheel")

        @contextmanager
        def managed():
            with TemporaryDirectory(prefix=prefix, dir=system_temp) as raw:
                created_roots.append(Path(raw))
                yield raw

        return managed()

    monkeypatch.setattr(distribution, "TemporaryDirectory", temporary_directory)

    verify_distribution(wheel_dir, runner=_recording_runner(calls))

    assert parent_arguments == [None]
    assert calls[0].argv[-1] == str(wheel.resolve())
    assert all(not root.exists() for root in created_roots)


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
