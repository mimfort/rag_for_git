from __future__ import annotations

import argparse
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

_CHECKOUT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]


def run_command(command: Command) -> None:
    subprocess.run(
        list(command.argv),
        cwd=command.cwd,
        env=dict(command.env),
        check=True,
    )


def verify_distribution(
    wheel_dir: Path,
    *,
    runner: Callable[[Command], None] = run_command,
) -> None:
    wheels = sorted(wheel_dir.resolve().glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"ожидался один wheel, найдено: {len(wheels)}")
    wheel = wheels[0]
    temporary_parent = None
    if not wheel_dir.resolve().is_relative_to(_CHECKOUT_ROOT):
        temporary_parent = wheel_dir.resolve().parent

    with TemporaryDirectory(prefix="reviewer-launcher-", dir=temporary_parent) as raw:
        root = Path(raw)
        tool_dir = root / "tools"
        bin_dir = root / "bin"
        outside = root / "outside-checkout"
        outside.mkdir()
        env = {
            **os.environ,
            "UV_TOOL_DIR": str(tool_dir),
            "UV_TOOL_BIN_DIR": str(bin_dir),
            "UV_CACHE_DIR": str(root / "cache"),
        }
        runner(Command(("uv", "tool", "install", "--force", str(wheel)), outside, env))
        executable = bin_dir / ("reviewer.exe" if os.name == "nt" else "reviewer")
        runner(Command((str(executable), "--help"), outside, env))
        runner(Command(("uvx", "--from", str(wheel), "reviewer", "--help"), outside, env))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Проверить установку launcher из собранного wheel через uv и uvx.",
    )
    parser.add_argument("wheel_dir", type=Path, help="Каталог ровно с одним wheel")
    args = parser.parse_args(argv)

    def run_and_report(command: Command) -> None:
        run_command(command)
        print(f"Подтверждено: {' '.join(command.argv)}")

    verify_distribution(args.wheel_dir, runner=run_and_report)


if __name__ == "__main__":
    main()
