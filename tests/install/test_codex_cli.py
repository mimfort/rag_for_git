from pathlib import Path

from click.testing import CliRunner

from reviewer import install as generic_install
from reviewer.entrypoints.cli import cli
from reviewer.install_codex import (
    CodexInstallOptions,
    CodexInstallResult,
    CodexPluginPlan,
    CodexPluginState,
    LegacyMigrationResult,
    MarketplaceState,
    SnapshotVerification,
)


def fake_result(tmp_path: Path, options: CodexInstallOptions) -> CodexInstallResult:
    state = CodexPluginState(
        tmp_path / "codex",
        MarketplaceState("rag-reviewer", tmp_path, "mimfort/rag_for_git"),
        None,
    )
    plan = CodexPluginPlan(
        state,
        options,
        "upgrade",
        ("codex", "upgrade"),
        ("codex", "add"),
    )
    verification = None if options.dry_run else SnapshotVerification(
        tmp_path,
        tmp_path / "plugin",
        "0.2.27+codex.123456789abc",
        ("ask", "finish-task"),
    )
    return CodexInstallResult(
        plan,
        verification,
        tmp_path / "config.bak",
        LegacyMigrationResult(tmp_path / "legacy", ("ask",), ()),
        (),
    )


def test_install_codex_routes_mcp_and_plugin(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(
        "reviewer.install_codex.run_codex_install",
        lambda options, **kwargs: captured.append(options) or fake_result(tmp_path, options),
    )
    result = CliRunner().invoke(cli, ["install", "codex"])
    assert result.exit_code == 0, result.output
    assert captured[0].include_mcp is True
    assert "New Chat/new CLI session" in result.output
    assert "Reload Window" in result.output


def test_install_codex_no_skills_does_not_call_plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "reviewer.install_codex.run_codex_install",
        lambda options, **kwargs: (_ for _ in ()).throw(AssertionError("plugin called")),
    )
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    result = CliRunner().invoke(
        cli, ["install", "codex", "--no-skills", "--path", str(tmp_path / "config.toml")]
    )
    assert result.exit_code == 0, result.output


def test_install_skills_codex_is_plugin_only_and_supports_dry_run(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(
        "reviewer.install_codex.run_codex_install",
        lambda options, **kwargs: captured.append(options) or fake_result(tmp_path, options),
    )
    result = CliRunner().invoke(cli, ["install-skills", "codex", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert captured[0].include_mcp is False and captured[0].dry_run is True


def test_init_yes_never_invokes_codex(monkeypatch, tmp_path):
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(
        "reviewer.install_codex.run_codex_install",
        lambda options, **kwargs: (_ for _ in ()).throw(AssertionError("Codex invoked")),
    )
    result = CliRunner().invoke(cli, ["init", "--scope", "global", "--yes"])
    assert result.exit_code == 0, result.output


def test_interactive_init_uses_the_canonical_codex_flow(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(
        "reviewer.install.prompt_groups",
        lambda groups, current, yes: {
            field.key: field.default for group in groups for field in group.fields
        },
    )
    # VCS setup = no, board setup = no, write = yes, reviewer check = no,
    # Codex install = yes
    answers = iter([False, False, True, False, True])
    monkeypatch.setattr("click.confirm", lambda prompt, default=True: next(answers))
    monkeypatch.setattr("reviewer.entrypoints.cli._shutil.which", lambda name: "/opt/codex")
    monkeypatch.setattr(
        "reviewer.install_codex.run_codex_install",
        lambda options, **kwargs: captured.append(options) or fake_result(tmp_path, options),
    )
    result = CliRunner().invoke(
        cli,
        ["init", "--scope", "global", "--path", str(tmp_path / ".env")],
    )
    assert result.exit_code == 0, result.output
    assert captured and captured[0].include_mcp is True


def test_install_all_reports_codex_failure_after_other_targets(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(generic_install, "_home", lambda: home)
    monkeypatch.setattr(generic_install, "detect_installed", lambda: [
        generic_install.CLIENTS["cursor"], generic_install.CLIENTS["codex"]
    ])
    monkeypatch.setattr(generic_install.shutil, "which", lambda name: "/opt/uvx")
    monkeypatch.setattr(
        "reviewer.install_codex.run_codex_install",
        lambda options, **kwargs: (_ for _ in ()).throw(RuntimeError("plugin failed")),
    )
    result = CliRunner().invoke(cli, ["install", "--all"])
    assert result.exit_code != 0
    assert "plugin failed" in result.output
    assert (home / ".cursor/mcp.json").is_file()
