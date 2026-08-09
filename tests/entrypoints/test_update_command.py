from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import click
from click.testing import CliRunner
import pytest

import reviewer.entrypoints.cli as cli_mod
from reviewer.versioning import (
    InstallMode,
    InstallationInfo,
    UpgradeResult,
    VersionCheck,
    check_latest,
    detect_installation,
)
from reviewer.update_lifecycle import ComposeSyncResult


@pytest.fixture
def no_artifact_refresh(monkeypatch):
    monkeypatch.setattr(cli_mod, "_refresh_update_artifacts", lambda ctx: None)


def test_update_editable_preserves_output(monkeypatch, no_artifact_refresh):
    monkeypatch.setattr(
        cli_mod,
        "detect_installation",
        lambda: InstallationInfo(InstallMode.EDITABLE, "0.4.0", "/usr/bin/uv"),
    )

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0
    assert result.output == (
        "Режим: dev (editable) | Версия: 0.4.0\nДля обновления: git pull && pip install -e .\n"
    )


def test_update_uvx_current_preserves_output(monkeypatch, no_artifact_refresh):
    monkeypatch.setattr(
        cli_mod,
        "detect_installation",
        lambda: InstallationInfo(InstallMode.UVX, "0.4.0", "/usr/bin/uv"),
    )
    monkeypatch.setattr(
        cli_mod,
        "check_latest",
        lambda info: VersionCheck(info, "0.4.0", False),
    )

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0
    assert result.output == (
        "Режим: uvx (временная) | Версия: 0.4.0\n"
        "Версия актуальна: 0.4.0.\n"
        "MCP-сервер обновляется автоматически — в конфиге клиента прописан @latest.\n"
    )


def test_update_network_failure_preserves_output(monkeypatch, no_artifact_refresh):
    info = InstallationInfo(InstallMode.UVX, "0.4.0", "/usr/bin/uv")
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(cli_mod, "check_latest", lambda info: VersionCheck(info, None, False))

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0
    assert result.output == (
        "Режим: uvx (временная) | Версия: 0.4.0\n"
        "Не удалось получить информацию с PyPI. Проверьте сеть.\n"
    )


def test_update_uv_tool_upgrade_success_reports_package_and_artifacts(monkeypatch):
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv")
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(cli_mod, "check_latest", lambda info: VersionCheck(info, "0.5.0", True))
    monkeypatch.setattr(cli_mod, "upgrade_uv_tool", lambda info: UpgradeResult(0, ""))
    monkeypatch.setattr(
        cli_mod,
        "run_fresh_artifact_refresh",
        lambda: SimpleNamespace(returncode=0, stdout="artifacts refreshed\n", stderr=""),
    )

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0
    assert "✓ Python package обновлён." in result.output
    assert "artifacts refreshed" in result.output


def test_update_uv_tool_upgrade_failure_is_nonzero(monkeypatch):
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv")
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(cli_mod, "check_latest", lambda info: VersionCheck(info, "0.5.0", True))
    monkeypatch.setattr(
        cli_mod, "upgrade_uv_tool", lambda info: UpgradeResult(1, "не удалось обновить")
    )

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code != 0
    assert "Ошибка uv tool upgrade: не удалось обновить" in result.output


def test_update_uv_tool_does_not_upgrade_when_latest_version_is_unknown(
    monkeypatch, no_artifact_refresh
):
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv")
    upgrade = Mock()
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(cli_mod, "check_latest", lambda info: VersionCheck(info, None, False))
    monkeypatch.setattr(cli_mod, "upgrade_uv_tool", upgrade)

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0
    assert "Не удалось получить информацию с PyPI. Проверьте сеть." in result.output
    upgrade.assert_not_called()


def test_update_does_not_claim_invalid_current_version_is_current_or_upgrade(
    monkeypatch, no_artifact_refresh
):
    info = InstallationInfo(InstallMode.UV_TOOL, "не-версия", "/usr/bin/uv")
    upgrade = Mock()
    response = SimpleNamespace(read=lambda: b'{"info": {"version": "1.0"}}')
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(
        cli_mod,
        "check_latest",
        lambda installation: check_latest(
            installation,
            opener=lambda request, timeout: nullcontext(response),
        ),
    )
    monkeypatch.setattr(cli_mod, "upgrade_uv_tool", upgrade)

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0
    assert "Не удалось определить корректную текущую версию." in result.output
    assert "Версия актуальна" not in result.output
    assert "Доступна новая версия" not in result.output
    upgrade.assert_not_called()


def test_update_uvx_new_version_preserves_output(monkeypatch, no_artifact_refresh):
    info = InstallationInfo(InstallMode.UVX, "0.4.0", "/usr/bin/uv")
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(cli_mod, "check_latest", lambda info: VersionCheck(info, "0.5.0", True))

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0
    assert result.output == (
        "Режим: uvx (временная) | Версия: 0.4.0\n"
        "Доступна новая версия: 0.4.0 → 0.5.0\n"
        "MCP-сервер подхватит обновление автоматически при следующем запуске (@latest в конфиге).\n"
        "Для CLI: uvx --from rag-reviewer@latest reviewer <команда>\n"
    )


def test_update_uvx_does_not_upgrade_unrelated_persistent_tool(
    monkeypatch, tmp_path, no_artifact_refresh
):
    tool_dir = tmp_path / "uv-tools"
    (tool_dir / "rag-reviewer").mkdir(parents=True)
    uvx_prefix = tmp_path / "uv-cache" / "archive-v0" / "current"
    distribution_location = uvx_prefix / "lib" / "python3.12" / "site-packages"
    distribution_location.mkdir(parents=True)
    uv = str(tmp_path / "bin" / "uv")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=f"{tool_dir}\n"))
    upgrade = Mock()
    monkeypatch.setattr(
        cli_mod,
        "detect_installation",
        lambda: detect_installation(
            distribution=SimpleNamespace(
                version="0.4.0",
                read_text=lambda name: '{"dir_info": {"editable": false}}',
            ),
            which=lambda name: uv,
            run=run,
            current_prefix=uvx_prefix,
            distribution_location=distribution_location,
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "check_latest",
        lambda info: VersionCheck(info, "0.5.0", True),
    )
    monkeypatch.setattr(cli_mod, "upgrade_uv_tool", upgrade)

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0
    assert result.output == (
        "Режим: uvx (временная) | Версия: 0.4.0\n"
        "Доступна новая версия: 0.4.0 → 0.5.0\n"
        "MCP-сервер подхватит обновление автоматически при следующем запуске (@latest в конфиге).\n"
        "Для CLI: uvx --from rag-reviewer@latest reviewer <команда>\n"
    )
    upgrade.assert_not_called()


def test_update_uv_tool_upgrade_runs_artifacts_in_fresh_process(monkeypatch):
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.3", "/usr/bin/uv")
    refreshed = Mock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout="artifacts ok\n",
            stderr="",
        )
    )
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(
        cli_mod,
        "check_latest",
        lambda value: VersionCheck(value, "0.4.4", True),
    )
    monkeypatch.setattr(
        cli_mod,
        "upgrade_uv_tool",
        lambda value: UpgradeResult(0, ""),
    )
    monkeypatch.setattr(
        cli_mod,
        "run_fresh_artifact_refresh",
        refreshed,
        raising=False,
    )

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0, result.output
    assert "Доступна новая версия: 0.4.3 → 0.4.4" in result.output
    assert "artifacts ok" in result.output
    refreshed.assert_called_once_with()


def test_update_uv_tool_stops_before_artifacts_when_upgrade_fails(monkeypatch):
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.3", "/usr/bin/uv")
    refreshed = Mock()
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(
        cli_mod,
        "check_latest",
        lambda value: VersionCheck(value, "0.4.4", True),
    )
    monkeypatch.setattr(
        cli_mod,
        "upgrade_uv_tool",
        lambda value: UpgradeResult(1, "registry unavailable"),
    )
    monkeypatch.setattr(cli_mod, "run_fresh_artifact_refresh", refreshed)

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code != 0
    assert "registry unavailable" in result.output
    refreshed.assert_not_called()


def test_update_current_version_refreshes_artifacts_in_process(monkeypatch):
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.4", "/usr/bin/uv")
    refresh = Mock()
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(
        cli_mod,
        "check_latest",
        lambda value: VersionCheck(value, "0.4.4", False),
    )
    monkeypatch.setattr(
        cli_mod,
        "_refresh_update_artifacts",
        refresh,
        raising=False,
    )

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0, result.output
    refresh.assert_called_once()


def test_update_uvx_upgrade_tool_is_explicit(monkeypatch):
    info = InstallationInfo(InstallMode.UVX, "0.4.4", "/usr/bin/uv")
    upgrade = Mock(return_value=UpgradeResult(0, ""))
    refresh = Mock()
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(
        cli_mod,
        "check_latest",
        lambda value: VersionCheck(value, "0.4.4", False),
    )
    monkeypatch.setattr(cli_mod, "upgrade_uv_tool", upgrade)
    monkeypatch.setattr(cli_mod, "_refresh_update_artifacts", refresh)

    regular = CliRunner().invoke(cli_mod.cli, ["update"])
    bootstrap = CliRunner().invoke(cli_mod.cli, ["update", "--upgrade-tool"])

    assert regular.exit_code == 0, regular.output
    assert bootstrap.exit_code == 0, bootstrap.output
    upgrade.assert_called_once_with(info)
    assert refresh.call_count == 2


def test_update_editable_refreshes_artifacts_without_touching_source(monkeypatch):
    info = InstallationInfo(InstallMode.EDITABLE, "0.4.4", "/usr/bin/uv")
    refresh = Mock()
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(cli_mod, "_refresh_update_artifacts", refresh)

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0, result.output
    assert "git pull && pip install -e ." in result.output
    refresh.assert_called_once()


def test_update_hidden_artifact_phase_skips_installation_detection(monkeypatch):
    detect = Mock()
    refresh = Mock()
    monkeypatch.setattr(cli_mod, "detect_installation", detect)
    monkeypatch.setattr(cli_mod, "_refresh_update_artifacts", refresh)

    result = CliRunner().invoke(cli_mod.cli, ["update", "--refresh-artifacts"])

    assert result.exit_code == 0, result.output
    detect.assert_not_called()
    refresh.assert_called_once()


def test_update_uvx_bootstrap_with_newer_version_upgrades_tool_once(monkeypatch):
    info = InstallationInfo(InstallMode.UVX, "0.4.3", "/usr/bin/uv")
    upgrade = Mock(return_value=UpgradeResult(0, ""))
    refresh = Mock()
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(
        cli_mod,
        "check_latest",
        lambda value: VersionCheck(value, "0.4.4", True),
    )
    monkeypatch.setattr(cli_mod, "upgrade_uv_tool", upgrade)
    monkeypatch.setattr(cli_mod, "_refresh_update_artifacts", refresh)

    result = CliRunner().invoke(cli_mod.cli, ["update", "--upgrade-tool"])

    assert result.exit_code == 0, result.output
    upgrade.assert_called_once_with(info)
    refresh.assert_called_once()


def test_update_pypi_failure_still_refreshes_independent_artifacts(monkeypatch):
    info = InstallationInfo(InstallMode.UVX, "0.4.4", "/usr/bin/uv")
    refresh = Mock()
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(
        cli_mod,
        "check_latest",
        lambda value: VersionCheck(value, None, False),
    )
    monkeypatch.setattr(cli_mod, "_refresh_update_artifacts", refresh)

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0, result.output
    assert "Не удалось получить информацию с PyPI" in result.output
    refresh.assert_called_once()


def test_update_invalid_package_version_still_refreshes_artifacts(monkeypatch):
    info = InstallationInfo(InstallMode.UV_TOOL, "local", "/usr/bin/uv")
    refresh = Mock()
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(
        cli_mod,
        "check_latest",
        lambda value: VersionCheck(value, "0.4.4", False, current_valid=False),
    )
    monkeypatch.setattr(cli_mod, "_refresh_update_artifacts", refresh)

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0, result.output
    assert "Не удалось определить корректную текущую версию" in result.output
    refresh.assert_called_once()


def test_refresh_artifacts_updates_compose_and_skips_absent_clients(monkeypatch, tmp_path):
    target = tmp_path / "docker-compose.yml"
    install_call = Mock()
    monkeypatch.setattr(
        cli_mod,
        "download_compose",
        lambda: b"services: {}\n",
        raising=False,
    )
    monkeypatch.setattr(
        cli_mod,
        "sync_compose_file",
        lambda content: ComposeSyncResult("created", target),
        raising=False,
    )
    monkeypatch.setattr(
        cli_mod,
        "_has_detected_clients",
        lambda: False,
        raising=False,
    )
    monkeypatch.setattr(
        cli_mod,
        "_install_detected_clients",
        install_call,
        raising=False,
    )

    result = CliRunner().invoke(cli_mod.cli, ["update", "--refresh-artifacts"])

    assert result.exit_code == 0, result.output
    assert f"Compose создан: {target}" in result.output
    assert "AI-клиенты не обнаружены" in result.output
    assert "New Chat/new CLI session" in result.output
    assert "Reload Window" in result.output
    install_call.assert_not_called()


def test_refresh_artifacts_preserves_modified_compose_and_updates_clients(monkeypatch, tmp_path):
    target = tmp_path / "docker-compose.yml"
    install_call = Mock()
    monkeypatch.setattr(cli_mod, "download_compose", lambda: b"services: {}\n")
    monkeypatch.setattr(
        cli_mod,
        "sync_compose_file",
        lambda content: ComposeSyncResult("preserved", target),
    )
    monkeypatch.setattr(cli_mod, "_has_detected_clients", lambda: True)
    monkeypatch.setattr(cli_mod, "_install_detected_clients", install_call)

    result = CliRunner().invoke(cli_mod.cli, ["update", "--refresh-artifacts"])

    assert result.exit_code == 0, result.output
    assert "не перезаписан" in result.output
    install_call.assert_called_once()


@pytest.mark.parametrize(
    ("action", "status"),
    [
        ("adopted", "принят под управление"),
        ("current", "актуален"),
        ("updated", "обновлён"),
    ],
)
def test_refresh_artifacts_reports_compose_status(monkeypatch, tmp_path, action, status):
    target = tmp_path / "docker-compose.yml"
    monkeypatch.setattr(cli_mod, "download_compose", lambda: b"services: {}\n")
    monkeypatch.setattr(
        cli_mod,
        "sync_compose_file",
        lambda content: ComposeSyncResult(action, target),
    )
    monkeypatch.setattr(cli_mod, "_has_detected_clients", lambda: False)

    result = CliRunner().invoke(cli_mod.cli, ["update", "--refresh-artifacts"])

    assert result.exit_code == 0, result.output
    assert f"Compose {status}: {target}" in result.output


def test_refresh_artifacts_attempts_clients_after_compose_download_failure(monkeypatch):
    install_call = Mock()
    monkeypatch.setattr(
        cli_mod,
        "download_compose",
        lambda: (_ for _ in ()).throw(OSError("offline")),
    )
    monkeypatch.setattr(cli_mod, "_has_detected_clients", lambda: True)
    monkeypatch.setattr(cli_mod, "_install_detected_clients", install_call)

    result = CliRunner().invoke(cli_mod.cli, ["update", "--refresh-artifacts"])

    assert result.exit_code != 0
    assert "Compose: OSError" in result.output
    install_call.assert_called_once()


def test_refresh_artifacts_aggregates_integration_failure(monkeypatch, tmp_path):
    target = tmp_path / "docker-compose.yml"
    monkeypatch.setattr(cli_mod, "download_compose", lambda: b"services: {}\n")
    monkeypatch.setattr(
        cli_mod,
        "sync_compose_file",
        lambda content: ComposeSyncResult("current", target),
    )
    monkeypatch.setattr(cli_mod, "_has_detected_clients", lambda: True)
    monkeypatch.setattr(
        cli_mod,
        "_install_detected_clients",
        lambda ctx: (_ for _ in ()).throw(click.ClickException("Codex failed")),
    )

    result = CliRunner().invoke(cli_mod.cli, ["update", "--refresh-artifacts"])

    assert result.exit_code != 0
    assert "Integrations: Codex failed" in result.output


def test_refresh_artifacts_sanitizes_unexpected_integration_failure(monkeypatch, tmp_path):
    target = tmp_path / "docker-compose.yml"
    monkeypatch.setattr(cli_mod, "download_compose", lambda: b"services: {}\n")
    monkeypatch.setattr(
        cli_mod,
        "sync_compose_file",
        lambda content: ComposeSyncResult("current", target),
    )
    monkeypatch.setattr(cli_mod, "_has_detected_clients", lambda: True)
    monkeypatch.setattr(
        cli_mod,
        "_install_detected_clients",
        lambda ctx: (_ for _ in ()).throw(OSError("/secret/profile")),
    )

    result = CliRunner().invoke(cli_mod.cli, ["update", "--refresh-artifacts"])

    assert result.exit_code != 0
    assert "Integrations: OSError" in result.output
    assert "/secret/profile" not in result.output


def test_install_detected_clients_reuses_install_all_contract():
    ctx = Mock()

    cli_mod._install_detected_clients(ctx)

    ctx.invoke.assert_called_once_with(
        cli_mod.install,
        client=None,
        all_clients=True,
        list_clients=False,
        path_opt=None,
        pin=None,
        no_latest=False,
        no_skills=False,
        dry_run=False,
    )


def test_has_detected_clients_includes_native_claude_cli(monkeypatch):
    from reviewer import install as inst

    monkeypatch.setattr(inst, "detect_installed", lambda: [])
    monkeypatch.setattr(
        cli_mod._shutil,
        "which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )

    assert cli_mod._has_detected_clients() is True


def test_update_help_describes_unified_lifecycle_and_hides_internal_phase():
    result = CliRunner().invoke(cli_mod.cli, ["update", "--help"])

    assert result.exit_code == 0, result.output
    assert "AI-client integrations" in result.output
    assert "Compose" in result.output
    assert "--upgrade-tool" in result.output
    assert "--refresh-artifacts" not in result.output
