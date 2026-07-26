from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

from click.testing import CliRunner

import reviewer.entrypoints.cli as cli_mod
from reviewer.versioning import (
    InstallMode,
    InstallationInfo,
    UpgradeResult,
    VersionCheck,
    check_latest,
    detect_installation,
)


def test_update_editable_preserves_output(monkeypatch):
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


def test_update_uvx_current_preserves_output(monkeypatch):
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


def test_update_network_failure_preserves_output(monkeypatch):
    info = InstallationInfo(InstallMode.UVX, "0.4.0", "/usr/bin/uv")
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(cli_mod, "check_latest", lambda info: VersionCheck(info, None, False))

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0
    assert result.output == (
        "Режим: uvx (временная) | Версия: 0.4.0\n"
        "Не удалось получить информацию с PyPI. Проверьте сеть.\n"
    )


def test_update_uv_tool_upgrade_success_preserves_output(monkeypatch):
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv")
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(cli_mod, "check_latest", lambda info: VersionCheck(info, "0.5.0", True))
    monkeypatch.setattr(cli_mod, "upgrade_uv_tool", lambda info: UpgradeResult(0, ""))

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0
    assert result.output == (
        "Режим: uv tool (постоянная) | Версия: 0.4.0\n"
        "Доступна новая версия: 0.4.0 → 0.5.0\n"
        "Обновлено. Перезапустите MCP-сервер.\n"
    )


def test_update_uv_tool_upgrade_failure_preserves_output(monkeypatch):
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv")
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(cli_mod, "check_latest", lambda info: VersionCheck(info, "0.5.0", True))
    monkeypatch.setattr(
        cli_mod, "upgrade_uv_tool", lambda info: UpgradeResult(1, "не удалось обновить")
    )

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0
    assert result.output == (
        "Режим: uv tool (постоянная) | Версия: 0.4.0\n"
        "Доступна новая версия: 0.4.0 → 0.5.0\n"
        "Ошибка uv tool upgrade: не удалось обновить\n"
    )


def test_update_uv_tool_does_not_upgrade_when_latest_version_is_unknown(monkeypatch):
    info = InstallationInfo(InstallMode.UV_TOOL, "0.4.0", "/usr/bin/uv")
    upgrade = Mock()
    monkeypatch.setattr(cli_mod, "detect_installation", lambda: info)
    monkeypatch.setattr(cli_mod, "check_latest", lambda info: VersionCheck(info, None, False))
    monkeypatch.setattr(cli_mod, "upgrade_uv_tool", upgrade)

    result = CliRunner().invoke(cli_mod.cli, ["update"])

    assert result.exit_code == 0
    assert "Не удалось получить информацию с PyPI. Проверьте сеть." in result.output
    upgrade.assert_not_called()


def test_update_does_not_claim_invalid_current_version_is_current_or_upgrade(monkeypatch):
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


def test_update_uvx_new_version_preserves_output(monkeypatch):
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


def test_update_uvx_does_not_upgrade_unrelated_persistent_tool(monkeypatch, tmp_path):
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
