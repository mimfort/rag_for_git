import json

import pytest

from reviewer.install_codex import (
    CodexInstallOptions,
    CodexPluginState,
    CommandResult,
    MarketplaceState,
    build_codex_plugin_plan,
    detect_codex_capabilities,
    find_codex_executable,
    read_codex_state,
)


class MappingRunner:
    def __init__(self, responses: dict[tuple[str, ...], CommandResult]):
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...]) -> CommandResult:
        self.calls.append(argv)
        return self.responses[argv]


def result(argv: tuple[str, ...], stdout: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(argv, returncode, stdout, "failure" if returncode else "")


def state_runner(exe, marketplace_data: object, plugin_data: object) -> MappingRunner:
    marketplace_argv = (str(exe), "plugin", "marketplace", "list", "--json")
    plugin_argv = (str(exe), "plugin", "list", "--available", "--json")
    return MappingRunner(
        {
            marketplace_argv: result(marketplace_argv, json.dumps(marketplace_data)),
            plugin_argv: result(plugin_argv, json.dumps(plugin_data)),
        }
    )


def test_find_codex_requires_an_absolute_executable(tmp_path):
    executable = (tmp_path / "Codex Dir" / "codex").resolve()
    assert find_codex_executable(lambda name: str(executable)) == executable
    with pytest.raises(RuntimeError, match="Codex CLI не найден"):
        find_codex_executable(lambda name: None)


def test_capability_detection_is_feature_based(tmp_path):
    exe = tmp_path / "codex"
    commands = {
        (str(exe), "plugin", "--help"): result(
            (str(exe), "plugin", "--help"), "Commands: add marketplace list"
        ),
        (str(exe), "plugin", "marketplace", "add", "--help"): result(
            (str(exe), "plugin", "marketplace", "add", "--help"),
            "--json --sparse --ref",
        ),
        (str(exe), "plugin", "marketplace", "upgrade", "--help"): result(
            (str(exe), "plugin", "marketplace", "upgrade", "--help"), "--json"
        ),
        (str(exe), "plugin", "add", "--help"): result(
            (str(exe), "plugin", "add", "--help"), "--json"
        ),
        (str(exe), "plugin", "list", "--help"): result(
            (str(exe), "plugin", "list", "--help"), "--json --available"
        ),
    }
    capabilities = detect_codex_capabilities(exe, MappingRunner(commands))
    assert capabilities.executable == exe


def test_old_codex_without_plugin_marketplace_is_actionable(tmp_path):
    exe = tmp_path / "codex"
    argv = (str(exe), "plugin", "--help")
    runner = MappingRunner({argv: result(argv, "Commands: list")})
    with pytest.raises(RuntimeError, match="не поддерживает"):
        detect_codex_capabilities(exe, runner)


def test_read_state_accepts_extra_json_fields(tmp_path):
    exe = tmp_path / "codex"
    marketplace_argv = (str(exe), "plugin", "marketplace", "list", "--json")
    plugin_argv = (str(exe), "plugin", "list", "--available", "--json")
    runner = MappingRunner(
        {
            marketplace_argv: result(
                marketplace_argv,
                json.dumps(
                    {
                        "marketplaces": [
                            {"name": "rag-reviewer", "root": str(tmp_path), "new": 1}
                        ]
                    }
                ),
            ),
            plugin_argv: result(
                plugin_argv,
                json.dumps(
                    {
                        "installed": [
                            {
                                "name": "rag-reviewer",
                                "marketplaceName": "rag-reviewer",
                                "version": "0.2.27+codex.123456789abc",
                                "installed": True,
                                "enabled": True,
                                "extra": "ignored",
                            }
                        ],
                        "available": [],
                    }
                ),
            ),
        }
    )
    state = read_codex_state(exe, runner)
    assert state.marketplace is not None and state.marketplace.root == tmp_path
    assert state.plugin is not None and state.plugin.enabled is True


@pytest.mark.parametrize(
    ("marketplace_data", "plugin_data", "message"),
    [
        ({}, {"installed": []}, "marketplace list: missing field marketplaces"),
        (
            {"marketplaces": []},
            {},
            "plugin list: missing field installed",
        ),
        (
            {"marketplaces": {}},
            {"installed": []},
            "marketplace list: marketplaces must be an array",
        ),
        (
            {"marketplaces": []},
            {"installed": {}},
            "plugin list: installed must be an array",
        ),
    ],
)
def test_read_state_requires_top_level_arrays(
    tmp_path, marketplace_data, plugin_data, message
):
    with pytest.raises(RuntimeError) as exc_info:
        read_codex_state(
            tmp_path / "codex",
            state_runner(tmp_path / "codex", marketplace_data, plugin_data),
        )
    assert str(exc_info.value) == message


@pytest.mark.parametrize(
    ("marketplace_data", "plugin_data", "message"),
    [
        (
            {"marketplaces": [None]},
            {"installed": []},
            "marketplace list: marketplaces[0] must be an object",
        ),
        (
            {"marketplaces": []},
            {"installed": [None]},
            "plugin list: installed[0] must be an object",
        ),
    ],
)
def test_read_state_requires_object_entries(
    tmp_path, marketplace_data, plugin_data, message
):
    with pytest.raises(RuntimeError) as exc_info:
        read_codex_state(
            tmp_path / "codex",
            state_runner(tmp_path / "codex", marketplace_data, plugin_data),
        )
    assert str(exc_info.value) == message


@pytest.mark.parametrize(
    ("marketplace_data", "plugin_data", "message"),
    [
        (
            {"marketplaces": [{"root": "/tmp/plugin"}]},
            {"installed": []},
            "marketplace list: marketplaces[0] missing field name",
        ),
        (
            {"marketplaces": [{"name": "other"}]},
            {"installed": []},
            "marketplace list: marketplaces[0] missing field root",
        ),
        (
            {"marketplaces": [{"name": 1, "root": "/tmp/plugin"}]},
            {"installed": []},
            "marketplace list: marketplaces[0].name must be a string",
        ),
        (
            {"marketplaces": [{"name": "other", "root": 1}]},
            {"installed": []},
            "marketplace list: marketplaces[0].root must be a string",
        ),
        (
            {
                "marketplaces": [
                    {"name": "other", "root": "/tmp/plugin", "source": 1}
                ]
            },
            {"installed": []},
            "marketplace list: marketplaces[0].source must be a string",
        ),
        (
            {"marketplaces": []},
            {
                "installed": [
                    {
                        "marketplaceName": "rag-reviewer",
                        "version": "1.0.0",
                        "installed": True,
                        "enabled": True,
                    }
                ]
            },
            "plugin list: installed[0] missing field name",
        ),
        (
            {"marketplaces": []},
            {
                "installed": [
                    {
                        "name": 1,
                        "marketplaceName": "rag-reviewer",
                        "version": "1.0.0",
                        "installed": True,
                        "enabled": True,
                    }
                ]
            },
            "plugin list: installed[0].name must be a string",
        ),
        (
            {"marketplaces": []},
            {
                "installed": [
                    {
                        "name": "other",
                        "marketplaceName": 1,
                        "version": "1.0.0",
                        "installed": True,
                        "enabled": True,
                    }
                ]
            },
            "plugin list: installed[0].marketplaceName must be a string",
        ),
        (
            {"marketplaces": []},
            {
                "installed": [
                    {
                        "name": "other",
                        "marketplaceName": "rag-reviewer",
                        "version": 1,
                        "installed": True,
                        "enabled": True,
                    }
                ]
            },
            "plugin list: installed[0].version must be a string",
        ),
        (
            {"marketplaces": []},
            {
                "installed": [
                    {
                        "name": "rag-reviewer",
                        "marketplaceName": "rag-reviewer",
                        "version": "1.0.0",
                        "installed": "false",
                        "enabled": True,
                    }
                ]
            },
            "plugin list: installed[0].installed must be a boolean",
        ),
        (
            {"marketplaces": []},
            {
                "installed": [
                    {
                        "name": "rag-reviewer",
                        "marketplaceName": "rag-reviewer",
                        "version": "1.0.0",
                        "installed": True,
                        "enabled": "false",
                    }
                ]
            },
            "plugin list: installed[0].enabled must be a boolean",
        ),
    ],
)
def test_read_state_validates_required_entry_fields_and_types(
    tmp_path, marketplace_data, plugin_data, message
):
    with pytest.raises(RuntimeError) as exc_info:
        read_codex_state(
            tmp_path / "codex",
            state_runner(tmp_path / "codex", marketplace_data, plugin_data),
        )
    assert str(exc_info.value) == message


@pytest.mark.parametrize(
    ("marketplace_data", "plugin_data", "message"),
    [
        (
            {
                "marketplaces": [
                    {"name": "rag-reviewer", "root": "/tmp/rag-reviewer"},
                    {"name": "other", "root": 1},
                ]
            },
            {"installed": []},
            "marketplace list: marketplaces[1].root must be a string",
        ),
        (
            {"marketplaces": []},
            {
                "installed": [
                    {
                        "name": "rag-reviewer",
                        "marketplaceName": "rag-reviewer",
                        "version": "1.0.0",
                        "installed": True,
                        "enabled": True,
                    },
                    {
                        "name": "other",
                        "marketplaceName": "other",
                        "version": "1.0.0",
                        "installed": True,
                        "enabled": "false",
                    },
                ]
            },
            "plugin list: installed[1].enabled must be a boolean",
        ),
    ],
)
def test_read_state_validates_entries_after_the_matching_entry(
    tmp_path, marketplace_data, plugin_data, message
):
    with pytest.raises(RuntimeError) as exc_info:
        read_codex_state(
            tmp_path / "codex",
            state_runner(tmp_path / "codex", marketplace_data, plugin_data),
        )
    assert str(exc_info.value) == message


def test_plan_chooses_add_for_fresh_and_upgrade_for_owned_marketplace(tmp_path):
    exe = tmp_path / "codex"
    fresh = read_codex_state(
        exe,
        MappingRunner(
            {
                (str(exe), "plugin", "marketplace", "list", "--json"): result(
                    (str(exe), "plugin", "marketplace", "list", "--json"),
                    '{"marketplaces": []}',
                ),
                (str(exe), "plugin", "list", "--available", "--json"): result(
                    (str(exe), "plugin", "list", "--available", "--json"),
                    '{"installed": [], "available": []}',
                ),
            }
        ),
    )
    fresh_plan = build_codex_plugin_plan(fresh, CodexInstallOptions())
    assert fresh_plan.marketplace_action == "add"
    assert "--sparse" in fresh_plan.marketplace_argv
    owned = CodexPluginState(
        exe,
        MarketplaceState("rag-reviewer", tmp_path, "mimfort/rag_for_git"),
        None,
    )
    assert (
        build_codex_plugin_plan(owned, CodexInstallOptions()).marketplace_action
        == "upgrade"
    )
