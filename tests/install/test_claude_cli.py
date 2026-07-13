from pathlib import Path

import click
import pytest
from click.testing import CliRunner

import reviewer.entrypoints.cli as cli_module
from reviewer import install as generic_install
from reviewer.entrypoints.cli import cli
from reviewer.install_claude import (
    CLAUDE_MARKETPLACE_NAME,
    CLAUDE_MARKETPLACE_SOURCE,
    CLAUDE_PLUGIN_ID,
    ClaudeInstallOptions,
    ClaudeInstallPlan,
    ClaudeInstallResult,
    ClaudeInstallState,
    ClaudeMarketplaceState,
    ClaudePluginState,
)
from reviewer.install_codex import CommandResult


def _plugin_result(tmp_path: Path, options: ClaudeInstallOptions) -> ClaudeInstallResult:
    executable = tmp_path / "claude"
    marketplace = ClaudeMarketplaceState(
        CLAUDE_MARKETPLACE_NAME, "git", CLAUDE_MARKETPLACE_SOURCE
    )
    plugin = ClaudePluginState(CLAUDE_PLUGIN_ID, "user", True)
    plan = ClaudeInstallPlan(
        ClaudeInstallState(executable, marketplace, plugin),
        options,
        (str(executable), "plugin", "marketplace", "add", CLAUDE_MARKETPLACE_SOURCE),
        (str(executable), "plugin", "install", CLAUDE_PLUGIN_ID),
    )
    return ClaudeInstallResult(plan, marketplace, plugin)


def _which_with_claude(tmp_path: Path):
    def which(name: str) -> str | None:
        if name == "claude":
            return str(tmp_path / "claude")
        if name == "uvx":
            return str(tmp_path / "uvx")
        return None

    return which


def test_install_claude_code_routes_global_plugin_and_allowlist(monkeypatch, tmp_path):
    home = tmp_path / "home"
    captured: list[ClaudeInstallOptions] = []

    def install_plugin(options: ClaudeInstallOptions) -> ClaudeInstallResult:
        captured.append(options)
        return _plugin_result(tmp_path, options)

    monkeypatch.setattr(generic_install, "_home", lambda: home)
    monkeypatch.setattr(generic_install.shutil, "which", _which_with_claude(tmp_path))
    monkeypatch.setattr("reviewer.install_claude.run_claude_install", install_plugin)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["install", "claude-code"])

        assert result.exit_code == 0, result.output
        assert captured == [ClaudeInstallOptions(dry_run=False)]
        assert "Claude Code plugin" in result.output
        assert not Path(".mcp.json").exists()
        settings = home / ".claude" / "settings.json"
        assert "mcp__reviewer__*" in settings.read_text(encoding="utf-8")


def test_install_all_collects_claude_failure_and_continues_targets(monkeypatch, tmp_path):
    home = tmp_path / "home"
    codex_calls: list[dict[str, object]] = []
    monkeypatch.setattr(generic_install, "_home", lambda: home)
    monkeypatch.setattr(
        generic_install,
        "detect_installed",
        lambda: [generic_install.CLIENTS["cursor"], generic_install.CLIENTS["codex"]],
    )
    monkeypatch.setattr(cli_module._shutil, "which", _which_with_claude(tmp_path))
    monkeypatch.setattr(
        "reviewer.install_claude.run_claude_install",
        lambda options: (_ for _ in ()).throw(RuntimeError("plugin failed")),
    )
    monkeypatch.setattr(
        cli_module,
        "_run_codex_target",
        lambda **kwargs: codex_calls.append(kwargs) or object(),
    )
    monkeypatch.setattr(cli_module, "_print_codex_result", lambda result: None)

    result = CliRunner().invoke(cli, ["install", "--all"])

    assert result.exit_code != 0
    assert "Claude Code CLI: plugin failed" in result.output
    assert (home / ".cursor" / "mcp.json").is_file()
    assert codex_calls == [
        {"include_mcp": True, "dry_run": False, "version": "latest", "path_opt": None}
    ]


def test_install_all_ignores_claude_without_executable(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(generic_install, "_home", lambda: home)
    monkeypatch.setattr(
        generic_install,
        "detect_installed",
        lambda: [generic_install.CLIENTS["cursor"]],
    )
    monkeypatch.setattr(
        cli_module._shutil,
        "which",
        lambda name: str(tmp_path / "uvx") if name == "uvx" else None,
    )
    monkeypatch.setattr(
        "reviewer.install_claude.run_claude_install",
        lambda options: (_ for _ in ()).throw(AssertionError("Claude was selected")),
    )

    result = CliRunner().invoke(cli, ["install", "--all"])

    assert result.exit_code == 0, result.output
    assert (home / ".cursor" / "mcp.json").is_file()


def test_install_all_collects_claude_mcp_setup_error(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(generic_install, "_home", lambda: home)
    monkeypatch.setattr(
        generic_install,
        "detect_installed",
        lambda: [generic_install.CLIENTS["cursor"]],
    )
    monkeypatch.setattr(cli_module._shutil, "which", _which_with_claude(tmp_path))
    monkeypatch.setattr(
        "reviewer.install_claude.find_claude_executable",
        lambda which: (_ for _ in ()).throw(RuntimeError("mcp unavailable")),
    )

    result = CliRunner().invoke(cli, ["install", "--all", "--no-skills"])

    assert result.exit_code != 0
    assert "Claude Code CLI: mcp unavailable" in result.output
    assert (home / ".cursor" / "mcp.json").is_file()


def test_install_claude_plugin_dry_run_has_no_config_write(monkeypatch, tmp_path):
    home = tmp_path / "home"
    captured: list[ClaudeInstallOptions] = []

    def install_plugin(options: ClaudeInstallOptions) -> ClaudeInstallResult:
        captured.append(options)
        return _plugin_result(tmp_path, options)

    monkeypatch.setattr(generic_install, "_home", lambda: home)
    monkeypatch.setattr("reviewer.install_claude.run_claude_install", install_plugin)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["install", "claude-code", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert captured == [ClaudeInstallOptions(dry_run=True)]
        assert not Path(".mcp.json").exists()
        assert not (home / ".claude" / "settings.json").exists()


def test_install_claude_rejects_path_for_all_native_modes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "reviewer.install_claude.run_claude_install",
        lambda options: (_ for _ in ()).throw(AssertionError("plugin called")),
    )

    for extra in ([], ["--no-skills"]):
        result = CliRunner().invoke(
            cli,
            ["install", "claude-code", "--path", str(tmp_path / "mcp.json"), *extra],
        )
        assert result.exit_code != 0
        assert "--path" in result.output


def test_install_all_rejects_path_before_any_target_when_claude_is_detected(
    monkeypatch, tmp_path
):
    native_calls: list[dict[str, object]] = []
    allowlist_calls: list[dict[str, object]] = []
    generic_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        generic_install,
        "detect_installed",
        lambda: [generic_install.CLIENTS["cursor"]],
    )
    monkeypatch.setattr(cli_module._shutil, "which", _which_with_claude(tmp_path))
    monkeypatch.setattr(
        cli_module,
        "_run_claude_target",
        lambda **kwargs: native_calls.append(kwargs) or object(),
    )
    monkeypatch.setattr(cli_module, "_print_claude_result", lambda result: None)
    monkeypatch.setattr(
        cli_module,
        "_apply_claude_allowlist",
        lambda *args, **kwargs: allowlist_calls.append(kwargs),
    )
    monkeypatch.setattr(
        generic_install,
        "build_plan",
        lambda *args, **kwargs: generic_calls.append(args)
        or (_ for _ in ()).throw(AssertionError("generic target reached")),
    )

    result = CliRunner().invoke(
        cli, ["install", "--all", "--path", str(tmp_path / "mcp.json")]
    )

    assert result.exit_code != 0
    assert "--path" in result.output
    assert native_calls == []
    assert allowlist_calls == []
    assert generic_calls == []


def test_install_all_rejects_path_before_detection_without_claude(monkeypatch, tmp_path):
    detected: list[object] = []
    native_calls: list[dict[str, object]] = []
    allowlist_calls: list[dict[str, object]] = []
    generic_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        generic_install,
        "detect_installed",
        lambda: detected.append(object())
        or (_ for _ in ()).throw(AssertionError("target detection reached")),
    )
    monkeypatch.setattr(
        cli_module._shutil,
        "which",
        lambda name: (_ for _ in ()).throw(AssertionError("executable detection reached")),
    )
    monkeypatch.setattr(
        cli_module,
        "_run_claude_target",
        lambda **kwargs: native_calls.append(kwargs) or object(),
    )
    monkeypatch.setattr(
        cli_module,
        "_apply_claude_allowlist",
        lambda *args, **kwargs: allowlist_calls.append(kwargs),
    )
    monkeypatch.setattr(
        generic_install,
        "build_plan",
        lambda *args, **kwargs: generic_calls.append(args)
        or (_ for _ in ()).throw(AssertionError("generic target reached")),
    )

    result = CliRunner().invoke(
        cli, ["install", "--all", "--path", str(tmp_path / "mcp.json")]
    )

    assert result.exit_code != 0
    assert "--path" in result.output
    assert detected == []
    assert native_calls == []
    assert allowlist_calls == []
    assert generic_calls == []


def test_install_all_path_conflict_precedes_list_handling(monkeypatch, tmp_path):
    detected: list[object] = []
    native_calls: list[dict[str, object]] = []
    allowlist_calls: list[dict[str, object]] = []
    generic_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        generic_install,
        "detect_installed",
        lambda: detected.append(object())
        or (_ for _ in ()).throw(AssertionError("target detection reached")),
    )
    monkeypatch.setattr(
        cli_module._shutil,
        "which",
        lambda name: (_ for _ in ()).throw(AssertionError("executable detection reached")),
    )
    monkeypatch.setattr(
        cli_module,
        "_run_claude_target",
        lambda **kwargs: native_calls.append(kwargs) or object(),
    )
    monkeypatch.setattr(
        cli_module,
        "_apply_claude_allowlist",
        lambda *args, **kwargs: allowlist_calls.append(kwargs),
    )
    monkeypatch.setattr(
        generic_install,
        "build_plan",
        lambda *args, **kwargs: generic_calls.append(args)
        or (_ for _ in ()).throw(AssertionError("generic target reached")),
    )

    result = CliRunner().invoke(
        cli,
        ["install", "--all", "--path", str(tmp_path / "mcp.json"), "--list"],
    )

    assert result.exit_code != 0
    assert "--path" in result.output
    assert "Поддерживаемые клиенты" not in result.output
    assert detected == []
    assert native_calls == []
    assert allowlist_calls == []
    assert generic_calls == []


@pytest.mark.parametrize(
    "extra",
    [pytest.param(["--pin", "1.2.3"], id="pin"), pytest.param(["--no-latest"], id="no-latest")],
)
def test_install_claude_plugin_rejects_static_manifest_overrides(monkeypatch, extra):
    monkeypatch.setattr(
        "reviewer.install_claude.run_claude_install",
        lambda options: (_ for _ in ()).throw(AssertionError("plugin called")),
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["install", "claude-code", *extra])

    assert result.exit_code != 0
    assert "--no-skills" in result.output


def test_install_claude_no_skills_routes_to_user_mcp_and_allowlist(monkeypatch, tmp_path):
    home = tmp_path / "home"
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(generic_install, "_home", lambda: home)
    monkeypatch.setattr(
        cli_module,
        "_run_claude_mcp_target",
        lambda **kwargs: captured.append(kwargs) or object(),
        raising=False,
    )
    monkeypatch.setattr(cli_module, "_print_claude_mcp_result", lambda result: None, raising=False)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["install", "claude-code", "--no-skills"])

        assert result.exit_code == 0, result.output
        assert captured == [{"dry_run": False, "version": "latest"}]
        assert not Path(".mcp.json").exists()
        assert (home / ".claude" / "settings.json").is_file()


class _FakeClaudeMcp:
    def __init__(self, executable: Path, state: str):
        self.executable = executable
        self.state = state
        self.calls: list[tuple[str, ...]] = []

    def _status(self) -> str:
        if self.state == "canonical":
            return (
                "reviewer:\n"
                " Scope: User config (available in all your projects)\n"
                " Status: ✔ Connected\n"
                " Type: stdio\n"
                " Command: uvx\n"
                " Args: --from rag-reviewer@latest reviewer-mcp\n"
            )
        if self.state == "wrong-user-server":
            return (
                "reviewer:\n"
                " Scope: User config (available in all your projects)\n"
                " Status: ✔ Connected\n"
                " Type: stdio\n"
                " Command: /opt/homebrew/bin/uvx\n"
                " Args: --from rag-reviewer@old reviewer-mcp\n"
            )
        raise AssertionError(f"unexpected state {self.state!r}")

    def __call__(self, argv: tuple[str, ...]) -> CommandResult:
        self.calls.append(argv)
        tail = argv[1:]
        if tail == ("mcp", "get", "reviewer"):
            if self.state == "missing":
                return CommandResult(argv, 1, "", 'No MCP server named "reviewer".')
            if self.state == "missing-with-configured-servers":
                return CommandResult(
                    argv,
                    1,
                    "",
                    'No MCP server named "reviewer". Configured servers: plugin:rag-reviewer:reviewer',
                )
            if self.state == "missing-with-add-guidance":
                return CommandResult(
                    argv,
                    1,
                    "",
                    'No MCP server named "reviewer". Run `claude mcp add` to add one.',
                )
            if self.state == "missing-with-extra-stream":
                return CommandResult(
                    argv,
                    1,
                    "Claude configuration is malformed",
                    'No MCP server named "reviewer".',
                )
            if self.state == "prefixed-diagnostic":
                return CommandResult(
                    argv,
                    1,
                    "",
                    'No MCP server named "reviewer". backend is unavailable',
                )
            if self.state == "broken-get":
                return CommandResult(argv, 1, "", "Claude configuration is malformed")
            return CommandResult(argv, 0, self._status(), "")
        if tail == ("mcp", "remove", "reviewer", "--scope", "user"):
            assert self.state == "wrong-user-server"
            self.state = "missing"
            return CommandResult(argv, 0, "Removed", "")
        if tail == (
            "mcp",
            "add",
            "--scope",
            "user",
            "reviewer",
            "--",
            "uvx",
            "--from",
            "rag-reviewer@latest",
            "reviewer-mcp",
        ):
            assert self.state in {
                "missing",
                "missing-with-configured-servers",
                "missing-with-add-guidance",
            }
            self.state = "canonical"
            return CommandResult(argv, 0, "Added", "")
        return CommandResult(argv, 2, "", f"unexpected argv: {argv}")


def test_claude_mcp_only_rejects_ambiguous_public_status_fields():
    fields = cli_module._claude_mcp_fields(
        "reviewer:\n"
        " Scope: User config (available in all your projects)\n"
        " Command: uvx\n"
        " Args: --from rag-reviewer@latest reviewer-mcp\n"
        " Command: /usr/local/bin/uvx\n"
    )

    assert not cli_module._claude_mcp_is_canonical(fields, "latest")


def test_claude_mcp_only_keeps_canonical_public_user_server(tmp_path):
    fake = _FakeClaudeMcp(tmp_path / "claude", "canonical")

    result = cli_module._run_claude_mcp_target(
        dry_run=False,
        version="latest",
        runner=fake,
        which=lambda name: str(fake.executable),
    )

    assert result.replaced is False
    assert fake.calls == [(str(fake.executable), "mcp", "get", "reviewer")]


def test_claude_mcp_only_adds_a_missing_public_user_server(tmp_path):
    fake = _FakeClaudeMcp(tmp_path / "claude", "missing")

    result = cli_module._run_claude_mcp_target(
        dry_run=False,
        version="latest",
        runner=fake,
        which=lambda name: str(fake.executable),
    )

    assert result.created is True
    assert result.replaced is False
    assert fake.calls == [
        (str(fake.executable), "mcp", "get", "reviewer"),
        (
            str(fake.executable),
            "mcp",
            "add",
            "--scope",
            "user",
            "reviewer",
            "--",
            "uvx",
            "--from",
            "rag-reviewer@latest",
            "reviewer-mcp",
        ),
        (str(fake.executable), "mcp", "get", "reviewer"),
    ]


def test_claude_mcp_only_accepts_missing_server_with_configured_servers_suffix(tmp_path):
    fake = _FakeClaudeMcp(tmp_path / "claude", "missing-with-configured-servers")

    result = cli_module._run_claude_mcp_target(
        dry_run=False,
        version="latest",
        runner=fake,
        which=lambda name: str(fake.executable),
    )

    assert result.created is True
    assert fake.calls[1][1:3] == ("mcp", "add")


def test_claude_mcp_only_accepts_missing_server_with_add_guidance(tmp_path):
    fake = _FakeClaudeMcp(tmp_path / "claude", "missing-with-add-guidance")

    result = cli_module._run_claude_mcp_target(
        dry_run=False,
        version="latest",
        runner=fake,
        which=lambda name: str(fake.executable),
    )

    assert result.created is True
    assert fake.calls[1][1:3] == ("mcp", "add")


def test_claude_mcp_only_stops_on_an_unexpected_get_failure(tmp_path):
    fake = _FakeClaudeMcp(tmp_path / "claude", "broken-get")

    with pytest.raises(click.ClickException, match="Claude Code MCP get"):
        cli_module._run_claude_mcp_target(
            dry_run=False,
            version="latest",
            runner=fake,
            which=lambda name: str(fake.executable),
        )

    assert fake.calls == [(str(fake.executable), "mcp", "get", "reviewer")]


def test_claude_mcp_only_rejects_a_prefixed_unexpected_get_diagnostic(tmp_path):
    fake = _FakeClaudeMcp(tmp_path / "claude", "prefixed-diagnostic")

    with pytest.raises(click.ClickException, match="Claude Code MCP get"):
        cli_module._run_claude_mcp_target(
            dry_run=False,
            version="latest",
            runner=fake,
            which=lambda name: str(fake.executable),
        )

    assert fake.calls == [(str(fake.executable), "mcp", "get", "reviewer")]


def test_claude_mcp_only_rejects_missing_response_with_an_extra_diagnostic_stream(tmp_path):
    fake = _FakeClaudeMcp(tmp_path / "claude", "missing-with-extra-stream")

    with pytest.raises(click.ClickException, match="Claude Code MCP get"):
        cli_module._run_claude_mcp_target(
            dry_run=False,
            version="latest",
            runner=fake,
            which=lambda name: str(fake.executable),
        )

    assert fake.calls == [(str(fake.executable), "mcp", "get", "reviewer")]


def test_claude_mcp_only_replaces_noncanonical_user_server_and_verifies(tmp_path):
    fake = _FakeClaudeMcp(tmp_path / "claude", "wrong-user-server")

    result = cli_module._run_claude_mcp_target(
        dry_run=False,
        version="latest",
        runner=fake,
        which=lambda name: str(fake.executable),
    )

    assert result.replaced is True
    assert fake.calls == [
        (str(fake.executable), "mcp", "get", "reviewer"),
        (str(fake.executable), "mcp", "remove", "reviewer", "--scope", "user"),
        (
            str(fake.executable),
            "mcp",
            "add",
            "--scope",
            "user",
            "reviewer",
            "--",
            "uvx",
            "--from",
            "rag-reviewer@latest",
            "reviewer-mcp",
        ),
        (str(fake.executable), "mcp", "get", "reviewer"),
    ]


def test_claude_mcp_only_dry_run_does_not_invoke_the_native_cli(tmp_path):
    fake = _FakeClaudeMcp(tmp_path / "claude", "missing")

    result = cli_module._run_claude_mcp_target(
        dry_run=True,
        version="latest",
        runner=fake,
        which=lambda name: str(fake.executable),
    )

    assert result.dry_run is True
    assert fake.calls == []
