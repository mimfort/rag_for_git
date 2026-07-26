from __future__ import annotations

import argparse
import os
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
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


def _temporary_parent_candidates() -> tuple[Path | None, ...]:
    candidates: list[Path | None] = [None, _CHECKOUT_ROOT.parent]
    for variable in ("XDG_RUNTIME_DIR", "LOCALAPPDATA", "APPDATA"):
        if value := os.environ.get(variable):
            candidates.append(Path(value))
    try:
        candidates.append(Path.home())
    except RuntimeError:
        pass
    return tuple(dict.fromkeys(candidates))


@contextmanager
def _temporary_root() -> Iterator[Path]:
    last_error: OSError | None = None
    for parent in _temporary_parent_candidates():
        candidate = ExitStack()
        try:
            raw = candidate.enter_context(
                TemporaryDirectory(prefix="reviewer-launcher-", dir=parent)
            )
            root = Path(raw).resolve()
        except OSError as error:
            last_error = error
            candidate.close()
            continue
        if root.is_relative_to(_CHECKOUT_ROOT):
            candidate.close()
            continue
        with candidate:
            yield root
        return
    raise RuntimeError("не удалось создать временный каталог вне checkout") from last_error


def verify_distribution(
    wheel_dir: Path,
    *,
    runner: Callable[[Command], None] = run_command,
) -> None:
    wheels = sorted(wheel_dir.resolve().glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"ожидался один wheel, найдено: {len(wheels)}")
    wheel = wheels[0]

    with _temporary_root() as root:
        uvx_env = {
            **os.environ,
            "UV_TOOL_DIR": str(root / "uvx-tools"),
            "UV_TOOL_BIN_DIR": str(root / "uvx-bin"),
            "UV_CACHE_DIR": str(root / "uvx-cache"),
        }
        tool_dir = root / "tools"
        bin_dir = root / "bin"
        outside = root / "outside-checkout"
        outside.mkdir()
        install_env = {
            **os.environ,
            "UV_TOOL_DIR": str(tool_dir),
            "UV_TOOL_BIN_DIR": str(bin_dir),
            "UV_CACHE_DIR": str(root / "cache"),
        }
        runner(
            Command(
                ("uvx", "--isolated", "--from", str(wheel), "reviewer", "--help"),
                outside,
                uvx_env,
            )
        )
        runner(
            Command(
                ("uv", "tool", "install", "--force", str(wheel)),
                outside,
                install_env,
            )
        )
        executable = bin_dir / ("reviewer.exe" if os.name == "nt" else "reviewer")
        runner(Command((str(executable), "--help"), outside, install_env))


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
