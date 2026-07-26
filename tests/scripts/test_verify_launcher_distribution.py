from __future__ import annotations

import os
import subprocess
import sys
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


def test_distribution_check_runs_standalone_uvx_before_persistent_install(tmp_path):
    wheel_dir = tmp_path / "dist"
    wheel = _wheel(wheel_dir)
    calls: list[Command] = []
    persistent_install_seen = False
    uvx_saw_persistent_install = False

    def observe(command: Command) -> None:
        nonlocal persistent_install_seen, uvx_saw_persistent_install
        calls.append(command)
        if command.argv[:3] == ("uv", "tool", "install"):
            persistent_install_seen = True
        if command.argv[0] == "uvx":
            uvx_saw_persistent_install = persistent_install_seen
            assert all(
                not Path(command.env[name]).exists()
                for name in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_CACHE_DIR")
            )

    verify_distribution(wheel_dir, runner=observe)

    uvx, install, installed, tui_smoke, mcp_smoke = calls
    assert uvx.argv == (
        "uvx",
        "--isolated",
        "--from",
        str(wheel.resolve()),
        "reviewer",
        "--help",
    )
    assert not uvx_saw_persistent_install
    assert all(
        uvx.env[name] != install.env[name]
        for name in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_CACHE_DIR")
    )
    assert uvx.argv[0] != installed.argv[0]
    assert Path(installed.argv[0]).parent == Path(install.env["UV_TOOL_BIN_DIR"])
    tool_python = (
        Path(install.env["UV_TOOL_DIR"])
        / "rag-reviewer"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    assert tui_smoke.argv == (
        str(tool_python),
        "-I",
        "-c",
        distribution.TUI_SMOKE_SCRIPT,
    )
    assert mcp_smoke.argv == (
        str(tool_python),
        "-I",
        "-c",
        distribution.MCP_IMPORT_SMOKE_SCRIPT,
    )


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

    uvx, install, _installed, tui_smoke, mcp_smoke = calls
    assert install.argv[:4] == ("uv", "tool", "install", "--force")
    assert install.argv[-1] == str(wheel.resolve())
    assert len(set(roots)) == 1
    assert all(not call.cwd.resolve().is_relative_to(distribution._CHECKOUT_ROOT) for call in calls)
    for command in (uvx, install):
        assert {
            Path(command.env[name]).parent
            for name in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_CACHE_DIR")
        } == {roots[0]}
        assert (
            len({command.env[name] for name in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_CACHE_DIR")})
            == 3
        )
    for command in calls:
        assert "PYTHONPATH" not in command.env
        assert "PYTHONHOME" not in command.env
    outside = calls[0].cwd
    assert tui_smoke.cwd == outside
    assert mcp_smoke.cwd == outside
    assert all(not root.exists() for root in roots)


def test_distribution_check_skips_inside_and_unwritable_temp_candidates(
    tmp_path,
    monkeypatch,
):
    created_roots: list[Path] = []
    parent_arguments: list[Path | None] = []
    unwritable_parent = distribution._CHECKOUT_ROOT.parent
    safe_parent = tmp_path / "безопасный-temp"
    safe_parent.mkdir()
    candidates = (None, unwritable_parent, safe_parent)

    with TemporaryDirectory(
        prefix="reviewer-temp-candidate-",
        dir=distribution._CHECKOUT_ROOT,
    ) as raw_inside:
        wheel_dir = Path(raw_inside) / "dist"
        _wheel(wheel_dir)
        calls: list[Command] = []

        def temporary_directory(*, prefix, dir=None):
            parent_arguments.append(dir)
            if dir == unwritable_parent:
                raise PermissionError("первый fallback недоступен")
            parent = Path(raw_inside) if dir is None else Path(dir)

            @contextmanager
            def managed():
                with TemporaryDirectory(prefix=prefix, dir=parent) as raw:
                    created_roots.append(Path(raw))
                    yield raw

            return managed()

        monkeypatch.setattr(
            distribution,
            "_temporary_parent_candidates",
            lambda: candidates,
            raising=False,
        )
        monkeypatch.setattr(distribution, "TemporaryDirectory", temporary_directory)

        verify_distribution(wheel_dir, runner=_recording_runner(calls))

        assert parent_arguments == list(candidates)
        assert all(
            not call.cwd.resolve().is_relative_to(distribution._CHECKOUT_ROOT) for call in calls
        )
        assert all(not root.exists() for root in created_roots)


def test_distribution_check_reports_exhausted_temp_candidates(tmp_path, monkeypatch):
    created_roots: list[Path] = []
    parent_arguments: list[Path | None] = []
    candidates = (
        None,
        distribution._CHECKOUT_ROOT.parent,
        tmp_path / "ещё-один-fallback",
    )

    with TemporaryDirectory(
        prefix="reviewer-exhaustion-candidate-",
        dir=distribution._CHECKOUT_ROOT,
    ) as raw_inside:
        wheel_dir = Path(raw_inside) / "dist"
        _wheel(wheel_dir)

        def temporary_directory(*, prefix, dir=None):
            parent_arguments.append(dir)
            if dir is not None:
                raise PermissionError(f"недоступен parent: {dir}")

            @contextmanager
            def managed():
                with TemporaryDirectory(prefix=prefix, dir=raw_inside) as raw:
                    created_roots.append(Path(raw))
                    yield raw

            return managed()

        monkeypatch.setattr(
            distribution,
            "_temporary_parent_candidates",
            lambda: candidates,
            raising=False,
        )
        monkeypatch.setattr(distribution, "TemporaryDirectory", temporary_directory)

        with pytest.raises(
            RuntimeError,
            match="не удалось создать временный каталог вне checkout",
        ) as error:
            verify_distribution(wheel_dir, runner=lambda command: None)

        assert isinstance(error.value.__cause__, PermissionError)
        assert parent_arguments == list(candidates)
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
    assert calls[0].argv[3] == str(wheel.resolve())
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

    assert calls[2].argv[0].endswith("reviewer.exe")
    assert calls[3].argv[0].endswith("rag-reviewer/Scripts/python.exe")
    assert calls[4].argv[0].endswith("rag-reviewer/Scripts/python.exe")


def test_distribution_check_uses_posix_tool_python(tmp_path):
    wheel_dir = tmp_path / "dist"
    _wheel(wheel_dir)
    calls: list[Command] = []

    verify_distribution(wheel_dir, runner=_recording_runner(calls))

    assert calls[3].argv[0].endswith("rag-reviewer/bin/python")
    assert calls[4].argv[0].endswith("rag-reviewer/bin/python")


@pytest.mark.parametrize(
    "payload_name",
    [
        pytest.param("TUI_SMOKE_SCRIPT", id="tui"),
        pytest.param("MCP_IMPORT_SMOKE_SCRIPT", id="mcp-import"),
    ],
)
def test_distribution_smoke_payloads_pass_in_fresh_process_outside_checkout(
    tmp_path,
    payload_name,
):
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in {"PYTHONHOME", "PYTHONPATH"}
    }

    subprocess.run(
        [sys.executable, "-I", "-c", getattr(distribution, payload_name)],
        cwd=tmp_path,
        env=env,
        check=True,
    )


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
