"""Безопасный выбор между CLI и интерактивным launcher."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
import sys

from reviewer.launcher.models import LauncherResult

_FALSEY_ENV = {"", "0", "false", "no", "off"}
_CI_MARKERS = (
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "TF_BUILD",
    "BUILDKITE",
    "CIRCLECI",
    "JENKINS_URL",
    "TEAMCITY_VERSION",
)


def _safe_isatty(stream: object) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _in_ci(environ: Mapping[str, str]) -> bool:
    ci = environ.get("CI")
    if ci is not None and ci.strip().casefold() not in _FALSEY_ENV:
        return True
    return any(environ.get(name) for name in _CI_MARKERS)


def should_use_tui(
    argv: Sequence[str],
    *,
    stdin: object,
    stdout: object,
    environ: Mapping[str, str],
) -> bool:
    """Разрешает TUI только для пустой команды в интерактивном терминале."""
    return (
        not argv
        and _safe_isatty(stdin)
        and _safe_isatty(stdout)
        and environ.get("TERM", "").strip().casefold() != "dumb"
        and not _in_ci(environ)
    )


def _run_cli(argv: Sequence[str]) -> None:
    from reviewer.entrypoints.cli import cli

    cli.main(args=list(argv), prog_name="reviewer", standalone_mode=True)


def _run_tui() -> LauncherResult:
    from reviewer.launcher.app import run_launcher

    return run_launcher()


def main(argv: Sequence[str] | None = None) -> None:
    """Запускает CLI или интерактивный launcher по безопасному условию."""
    resolved = tuple(sys.argv[1:] if argv is None else argv)
    if not should_use_tui(
        resolved,
        stdin=sys.stdin,
        stdout=sys.stdout,
        environ=os.environ,
    ):
        _run_cli(resolved)
        return
    try:
        result = _run_tui()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception:
        print(
            "reviewer: интерактивный launcher недоступен; используйте reviewer --help",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    if result.argv is None:
        raise SystemExit(result.exit_code)
    _run_cli(result.argv)
