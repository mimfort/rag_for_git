from __future__ import annotations

from click.testing import CliRunner
import pytest

from reviewer.entrypoints.cli import cli
from reviewer.web import serve


def test_module_cli_uses_container_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.delenv("REVIEWER_WEB_HOST", raising=False)
    monkeypatch.delenv("REVIEWER_WEB_PORT", raising=False)
    monkeypatch.setattr(serve, "run_server", lambda host, port: calls.append((host, port)))

    serve.main([])

    assert calls == [("0.0.0.0", 8000)]


def test_module_cli_reads_env_and_arguments_override_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setenv("REVIEWER_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("REVIEWER_WEB_PORT", "8080")
    monkeypatch.setattr(serve, "run_server", lambda host, port: calls.append((host, port)))

    serve.main(["--host", "127.0.0.2", "--port", "9090"])

    assert calls == [("127.0.0.2", 9090)]


def test_module_cli_rejects_invalid_env_port(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("REVIEWER_WEB_PORT", "not-a-port")

    with pytest.raises(SystemExit) as exc_info:
        serve.main([])

    assert exc_info.value.code == 2
    assert "invalid int value" in capsys.readouterr().err


def test_run_server_builds_app_and_passes_host_port(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = object()
    app = object()
    calls: list[tuple[object, str, int]] = []
    monkeypatch.setattr("reviewer.config.settings.Settings", lambda: settings)
    monkeypatch.setattr(
        "reviewer.web.app.create_app",
        lambda actual: app if actual is settings else None,
    )
    monkeypatch.setattr(
        "uvicorn.run",
        lambda actual, *, host, port: calls.append((actual, host, port)),
    )

    serve.run_server("127.0.0.2", 9090)

    assert calls == [(app, "127.0.0.2", 9090)]
    assert "http://127.0.0.2:9090" in capsys.readouterr().out


def test_click_serve_delegates_to_shared_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(serve, "run_server", lambda host, port: calls.append((host, port)))

    result = CliRunner().invoke(cli, ["serve", "--host", "0.0.0.0", "--port", "8080"])

    assert result.exit_code == 0
    assert calls == [("0.0.0.0", 8080)]
