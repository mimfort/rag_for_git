import json
import tomllib
from pathlib import Path

import pytest

from reviewer.install_claude import (
    CLAUDE_MARKETPLACE_NAME,
    CLAUDE_MARKETPLACE_SOURCE,
    CLAUDE_PLUGIN_ID,
    ClaudeInstallError,
    ClaudeInstallOptions,
    detect_claude_capabilities,
    find_claude_executable,
    run_claude_install,
)
from reviewer.install_codex import CommandResult
from tests.install.fake_claude import FakeClaude

ROOT = Path(__file__).resolve().parents[2]


class MappingRunner:
    def __init__(self, responses: dict[tuple[str, ...], CommandResult]):
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...]) -> CommandResult:
        self.calls.append(argv)
        return self.responses[argv]


def command_result(
    argv: tuple[str, ...], stdout: str = "", returncode: int = 0
) -> CommandResult:
    return CommandResult(argv, returncode, stdout, "failure" if returncode else "")


def test_find_claude_requires_an_executable(tmp_path):
    executable = (tmp_path / "Claude Dir" / "claude").resolve()

    assert find_claude_executable(lambda name: str(executable)) == executable
    with pytest.raises(RuntimeError, match="Claude Code CLI не найден"):
        find_claude_executable(lambda name: None)


def test_capability_detection_uses_required_public_commands(tmp_path):
    executable = tmp_path / "claude"
    commands = {
        (str(executable), "plugin", "marketplace", "add", "--help"): command_result(
            (str(executable), "plugin", "marketplace", "add", "--help"),
            "--scope --sparse",
        ),
        (str(executable), "plugin", "marketplace", "update", "--help"): command_result(
            (str(executable), "plugin", "marketplace", "update", "--help")
        ),
        (str(executable), "plugin", "marketplace", "list", "--help"): command_result(
            (str(executable), "plugin", "marketplace", "list", "--help"), "--json"
        ),
        (str(executable), "plugin", "install", "--help"): command_result(
            (str(executable), "plugin", "install", "--help"), "--scope"
        ),
        (str(executable), "plugin", "update", "--help"): command_result(
            (str(executable), "plugin", "update", "--help"), "--scope"
        ),
        (str(executable), "plugin", "list", "--help"): command_result(
            (str(executable), "plugin", "list", "--help"), "--json"
        ),
    }
    runner = MappingRunner(commands)

    capabilities = detect_claude_capabilities(executable, runner)

    assert capabilities.executable == executable
    assert runner.calls == list(commands)


def test_old_claude_without_sparse_marketplaces_is_actionable(tmp_path):
    executable = tmp_path / "claude"
    argv = (str(executable), "plugin", "marketplace", "add", "--help")

    with pytest.raises(ClaudeInstallError, match="не поддерживает"):
        detect_claude_capabilities(
            executable, MappingRunner({argv: command_result(argv, "--scope")})
        )


def test_run_claude_install_adds_canonical_global_plugin(tmp_path):
    fake = FakeClaude(tmp_path / "claude", tmp_path / "claude-config")

    result = run_claude_install(
        ClaudeInstallOptions(),
        runner=fake,
        which=lambda name: str(fake.executable),
    )

    mutations = [
        call
        for call in fake.calls
        if call[1:4]
        in {
            ("plugin", "marketplace", "add"),
            ("plugin", "install", "rag-reviewer@rag-reviewer-marketplace"),
        }
    ]
    assert mutations[-2:] == [
        (
            str(fake.executable),
            "plugin",
            "marketplace",
            "add",
            "https://github.com/mimfort/rag_for_git.git",
            "--scope",
            "user",
            "--sparse",
            ".claude-plugin",
            "plugin",
        ),
        (
            str(fake.executable),
            "plugin",
            "install",
            "rag-reviewer@rag-reviewer-marketplace",
            "--scope",
            "user",
        ),
    ]
    assert result.marketplace is not None
    assert result.marketplace.name == CLAUDE_MARKETPLACE_NAME
    assert result.marketplace.source == "git"
    assert result.marketplace.url == CLAUDE_MARKETPLACE_SOURCE
    assert result.plugin is not None
    assert result.plugin.enabled is True
    assert fake.config_path.is_file()


def test_run_claude_install_is_repeatable(tmp_path):
    fake = FakeClaude(tmp_path / "claude", tmp_path / "claude-config")
    first = run_claude_install(
        ClaudeInstallOptions(), runner=fake, which=lambda name: str(fake.executable)
    )
    fake.calls.clear()

    second = run_claude_install(
        ClaudeInstallOptions(), runner=fake, which=lambda name: str(fake.executable)
    )

    assert first.plugin is not None and second.plugin is not None
    assert second.plugin == first.plugin
    assert (
        str(fake.executable),
        "plugin",
        "marketplace",
        "update",
        CLAUDE_MARKETPLACE_NAME,
    ) in fake.calls
    assert (
        str(fake.executable),
        "plugin",
        "update",
        CLAUDE_PLUGIN_ID,
        "--scope",
        "user",
    ) in fake.calls
    assert not any(
        call[1:4] == ("plugin", "marketplace", "add")
        and call[-1:] != ("--help",)
        for call in fake.calls
    )
    assert not any(
        call[1:3] == ("plugin", "install") and call[-1:] != ("--help",)
        for call in fake.calls
    )


def test_run_claude_install_installs_a_user_plugin_when_only_project_scope_exists(tmp_path):
    fake = FakeClaude(tmp_path / "claude", tmp_path / "claude-config")
    fake.marketplace = {
        "name": CLAUDE_MARKETPLACE_NAME,
        "source": "git",
        "url": CLAUDE_MARKETPLACE_SOURCE,
    }
    fake.plugin = {"id": CLAUDE_PLUGIN_ID, "scope": "project", "enabled": True}

    result = run_claude_install(
        ClaudeInstallOptions(), runner=fake, which=lambda name: str(fake.executable)
    )

    assert (
        str(fake.executable),
        "plugin",
        "marketplace",
        "update",
        CLAUDE_MARKETPLACE_NAME,
    ) in fake.calls
    assert (
        str(fake.executable),
        "plugin",
        "install",
        CLAUDE_PLUGIN_ID,
        "--scope",
        "user",
    ) in fake.calls
    assert result.plugin is not None and result.plugin.scope == "user"


def test_fake_claude_install_is_a_noop_for_an_existing_user_plugin(tmp_path):
    fake = FakeClaude(tmp_path / "claude", tmp_path / "claude-config")
    plugin = {"id": CLAUDE_PLUGIN_ID, "scope": "user", "enabled": False}
    fake.plugin = plugin

    response = fake(
        (
            str(fake.executable),
            "plugin",
            "install",
            CLAUDE_PLUGIN_ID,
            "--scope",
            "user",
        )
    )

    assert response.returncode == 0
    assert response.stdout == "Plugin already installed"
    assert fake.plugin == plugin


@pytest.mark.parametrize(
    "after_add",
    [
        pytest.param(
            {
                "name": CLAUDE_MARKETPLACE_NAME,
                "source": "directory",
                "url": CLAUDE_MARKETPLACE_SOURCE,
            },
            id="non-git-marketplace",
        ),
        pytest.param(
            {
                "name": CLAUDE_MARKETPLACE_NAME,
                "source": "git",
                "url": "http://github.com/mimfort/rag_for_git.git",
            },
            id="non-https-marketplace",
        ),
    ],
)
def test_run_claude_install_rejects_noncanonical_marketplace_after_mutation(
    tmp_path, after_add
):
    fake = FakeClaude(tmp_path / "claude", tmp_path / "claude-config")
    fake.marketplace_after_add = after_add

    with pytest.raises(ClaudeInstallError) as exc_info:
        run_claude_install(
            ClaudeInstallOptions(), runner=fake, which=lambda name: str(fake.executable)
        )

    assert exc_info.value.phase == "marketplace ownership verification"
    assert exc_info.value.argv[1:4] == ("plugin", "marketplace", "add")
    assert not any(
        call[1:3] == ("plugin", "install") and call[-1:] != ("--help",)
        for call in fake.calls
    )


@pytest.mark.parametrize(
    "after_install",
    [
        pytest.param(
            {"id": CLAUDE_PLUGIN_ID, "scope": "user", "enabled": False},
            id="disabled-plugin",
        ),
        pytest.param(
            {"id": "rag-reviewer@other-marketplace", "scope": "user", "enabled": True},
            id="foreign-plugin",
        ),
    ],
)
def test_run_claude_install_rejects_disabled_or_foreign_plugin_after_mutation(
    tmp_path, after_install
):
    fake = FakeClaude(tmp_path / "claude", tmp_path / "claude-config")
    fake.plugin_after_install = after_install

    with pytest.raises(ClaudeInstallError) as exc_info:
        run_claude_install(
            ClaudeInstallOptions(), runner=fake, which=lambda name: str(fake.executable)
        )

    assert exc_info.value.phase == "post-install verification"
    assert exc_info.value.argv[1:3] == ("plugin", "list")


@pytest.mark.parametrize(
    ("failure", "phase"),
    [
        pytest.param(
            ("plugin", "marketplace", "add"), "marketplace add", id="marketplace"
        ),
        pytest.param(("plugin", "install"), "plugin install", id="plugin"),
    ],
)
def test_run_claude_install_reports_failed_mutation(tmp_path, failure, phase):
    fake = FakeClaude(tmp_path / "claude", tmp_path / "claude-config")
    fake.fail = failure

    with pytest.raises(ClaudeInstallError) as exc_info:
        run_claude_install(
            ClaudeInstallOptions(), runner=fake, which=lambda name: str(fake.executable)
        )

    assert exc_info.value.phase == phase
    assert "injected failure" in exc_info.value.detail


@pytest.mark.parametrize(
    ("failure", "phase", "expected_argv"),
    [
        pytest.param(
            ("plugin", "marketplace", "update"),
            "marketplace update",
            ("plugin", "marketplace", "update", CLAUDE_MARKETPLACE_NAME),
            id="marketplace-update",
        ),
        pytest.param(
            ("plugin", "update"),
            "plugin update",
            ("plugin", "update", CLAUDE_PLUGIN_ID, "--scope", "user"),
            id="plugin-update",
        ),
    ],
)
def test_run_claude_install_reports_failed_update(tmp_path, failure, phase, expected_argv):
    fake = FakeClaude(tmp_path / "claude", tmp_path / "claude-config")
    fake.marketplace = {
        "name": CLAUDE_MARKETPLACE_NAME,
        "source": "git",
        "url": CLAUDE_MARKETPLACE_SOURCE,
    }
    fake.plugin = {"id": CLAUDE_PLUGIN_ID, "scope": "user", "enabled": True}
    fake.fail = failure

    with pytest.raises(ClaudeInstallError) as exc_info:
        run_claude_install(
            ClaudeInstallOptions(), runner=fake, which=lambda name: str(fake.executable)
        )

    assert exc_info.value.phase == phase
    assert exc_info.value.argv[1:] == expected_argv
    assert "injected failure" in exc_info.value.detail


def test_run_claude_install_dry_run_does_not_mutate(tmp_path):
    fake = FakeClaude(tmp_path / "claude", tmp_path / "claude-config")

    result = run_claude_install(
        ClaudeInstallOptions(dry_run=True),
        runner=fake,
        which=lambda name: str(fake.executable),
    )

    assert result.plugin is None
    assert result.plan.marketplace_argv[4] == CLAUDE_MARKETPLACE_SOURCE
    assert result.plan.plugin_argv[3] == CLAUDE_PLUGIN_ID
    assert not fake.config_path.exists()
    assert fake.calls == []


def test_run_claude_install_requires_an_executable():
    with pytest.raises(RuntimeError, match="Claude Code CLI не найден"):
        run_claude_install(ClaudeInstallOptions(), which=lambda name: None)


def test_plugin_mcp_configuration_uses_portable_uvx_argv():
    payload = json.loads((ROOT / "plugin/.mcp.json").read_text(encoding="utf-8"))

    assert payload["mcpServers"]["reviewer"] == {
        "command": "uvx",
        "args": ["--from", "rag-reviewer@latest", "reviewer-mcp"],
    }


def test_claude_plugin_manifest_version_matches_project_release():
    manifest = json.loads(
        (ROOT / "plugin/.claude-plugin/plugin.json").read_text(encoding="utf-8")
    )
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert manifest["version"] == project["project"]["version"]
