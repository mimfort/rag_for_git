import json
import os
import shutil
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import reviewer.install as generic_install
import reviewer.install_codex as codex_install

from reviewer.install_codex import (
    CodexInstallError,
    CodexInstallOptions,
    CodexPluginState,
    CommandResult,
    MarketplaceState,
    PluginState,
    build_codex_plugin_plan,
    detect_codex_capabilities,
    find_codex_executable,
    find_owned_legacy_skills,
    marketplace_is_owned,
    migrate_legacy_skills,
    payload_digest,
    read_codex_state,
    run_codex_install,
    verify_marketplace_snapshot,
)
from tests.install.fake_codex import FakeCodex


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
                            {
                                "name": "rag-reviewer",
                                "root": str(tmp_path),
                                "marketplaceSource": {
                                    "sourceType": "git",
                                    "source": "mimfort/rag_for_git",
                                    "ref": "main",
                                    "sparsePaths": [".agents/plugins", "plugin"],
                                },
                                "new": 1,
                            }
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
    assert state.marketplace.source == "mimfort/rag_for_git"
    assert state.marketplace.ref == "main"
    assert state.marketplace.sparse_paths == (".agents/plugins", "plugin")
    assert state.plugin is not None and state.plugin.enabled is True


def test_read_state_hydrates_codex_marketplace_metadata_from_global_config(tmp_path):
    exe = tmp_path / "codex"
    config = tmp_path / "home" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        "[marketplaces.rag-reviewer]\n"
        'source_type = "git"\n'
        'source = "https://github.com/mimfort/rag_for_git.git"\n'
        'ref = "main"\n'
        'sparse_paths = [".agents/plugins", "plugin"]\n',
        encoding="utf-8",
    )

    state = read_codex_state(
        exe,
        state_runner(
            exe,
            {
                "marketplaces": [
                    {
                        "name": "rag-reviewer",
                        "root": str(tmp_path),
                        "marketplaceSource": {
                            "sourceType": "git",
                            "source": "https://github.com/mimfort/rag_for_git.git",
                        },
                    }
                ]
            },
            {"installed": []},
        ),
        config_path=config,
    )

    assert state.marketplace is not None
    assert marketplace_is_owned(state.marketplace)


def test_read_state_keeps_complete_json_metadata_without_global_marketplace_entry(tmp_path):
    exe = tmp_path / "codex"
    config = tmp_path / "home" / "config.toml"
    config.parent.mkdir()
    config.write_text("[other]\nvalue = true\n", encoding="utf-8")

    state = read_codex_state(
        exe,
        state_runner(
            exe,
            {
                "marketplaces": [
                    {
                        "name": "rag-reviewer",
                        "root": str(tmp_path),
                        "marketplaceSource": {
                            "sourceType": "git",
                            "source": "https://github.com/mimfort/rag_for_git.git",
                            "ref": "main",
                            "sparsePaths": [".agents/plugins", "plugin"],
                        },
                    }
                ]
            },
            {"installed": []},
        ),
        config_path=config,
    )

    assert state.marketplace is not None
    assert marketplace_is_owned(state.marketplace)


def test_run_codex_install_uses_global_marketplace_metadata_with_real_list_shape(
    tmp_path, monkeypatch
):
    repo = Path(__file__).resolve().parents[2]
    codex_home = tmp_path / "home"
    fake = FakeCodex(tmp_path / "codex", repo, codex_home)
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")

    result = run_codex_install(
        CodexInstallOptions(codex_home=codex_home),
        runner=fake,
        which=lambda name: str(fake.executable),
    )

    assert result.verification is not None
    assert fake.installed is not None
    assert fake.installed["marketplaceName"] == "rag-reviewer"
    assert (
        str(fake.executable),
        "plugin",
        "add",
        "rag-reviewer@rag-reviewer",
        "--json",
    ) in fake.calls
    assert tomllib.loads(codex_home.joinpath("config.toml").read_text(encoding="utf-8"))[
        "marketplaces"
    ]["rag-reviewer"] == {
        "source_type": "git",
        "source": "https://github.com/mimfort/rag_for_git.git",
        "ref": "main",
        "sparse_paths": [".agents/plugins", "plugin"],
    }
    marketplace_list = fake(
        (str(fake.executable), "plugin", "marketplace", "list", "--json")
    )
    assert json.loads(marketplace_list.stdout)["marketplaces"] == [
        {
            "name": "rag-reviewer",
            "root": str(repo),
            "marketplaceSource": {
                "sourceType": "git",
                "source": "https://github.com/mimfort/rag_for_git.git",
            },
        }
    ]


@pytest.mark.parametrize(
    ("source_type", "source", "ref", "sparse_paths"),
    [
        pytest.param(
            "local",
            "https://github.com/mimfort/rag_for_git.git",
            "main",
            (".agents/plugins", "plugin"),
            id="non-git-source-type",
        ),
        pytest.param(
            "git",
            "https://github.com/someone/else.git",
            "main",
            (".agents/plugins", "plugin"),
            id="foreign-url",
        ),
        pytest.param(
            "git",
            "https://github.com/mimfort/rag_for_git.git",
            "other",
            (".agents/plugins", "plugin"),
            id="wrong-ref",
        ),
        pytest.param(
            "git",
            "https://github.com/mimfort/rag_for_git.git",
            "main",
            ("plugin", ".agents/plugins"),
            id="reversed-sparse-paths",
        ),
    ],
)
def test_run_codex_install_rejects_unowned_global_marketplace_metadata_before_mcp_and_plugin_add(
    tmp_path, monkeypatch, source_type, source, ref, sparse_paths
):
    repo = Path(__file__).resolve().parents[2]
    codex_home = tmp_path / "home"
    config = codex_home / "config.toml"
    config.parent.mkdir()
    config.write_text(
        "[fake_codex]\n"
        "marketplace = true\n\n"
        "[marketplaces.rag-reviewer]\n"
        f"source_type = {json.dumps(source_type)}\n"
        f"source = {json.dumps(source)}\n"
        f"ref = {json.dumps(ref)}\n"
        f"sparse_paths = {json.dumps(list(sparse_paths))}\n",
        encoding="utf-8",
    )
    original = config.read_bytes()
    fake = FakeCodex(tmp_path / "codex", repo, codex_home)
    applied = []
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    monkeypatch.setattr(
        "reviewer.install.apply_plan", lambda plan: applied.append(plan)
    )

    with pytest.raises(RuntimeError, match="source/ref/sparse"):
        run_codex_install(
            CodexInstallOptions(codex_home=codex_home),
            runner=fake,
            which=lambda name: str(fake.executable),
        )

    assert config.read_bytes() == original
    assert applied == []
    assert not any(
        call[1:4]
        in {
            ("plugin", "marketplace", "add"),
            ("plugin", "marketplace", "upgrade"),
        }
        and call[-1:] != ("--help",)
        for call in fake.calls
    )
    assert not any(
        call[1:3] == ("plugin", "add") and call[-1:] != ("--help",)
        for call in fake.calls
    )


@pytest.mark.parametrize(
    "legacy_extra",
    [
        pytest.param({}, id="missing-current-metadata"),
        pytest.param(
            {"ref": "main", "sparsePaths": [".agents/plugins", "plugin"]},
            id="forged-top-level-ref-sparse",
        ),
    ],
)
def test_legacy_marketplace_source_never_authorizes_upgrade(tmp_path, legacy_extra):
    exe = tmp_path / "codex"
    marketplace = {
        "name": "rag-reviewer",
        "root": str(tmp_path),
        "source": "mimfort/rag_for_git",
        **legacy_extra,
    }
    state = read_codex_state(
        exe,
        state_runner(
            exe,
            {"marketplaces": [marketplace]},
            {"installed": []},
        ),
    )

    assert state.marketplace is not None
    assert state.marketplace.source == "mimfort/rag_for_git"
    assert state.marketplace.ref is None
    assert state.marketplace.sparse_paths is None
    assert marketplace_is_owned(state.marketplace) is False
    with pytest.raises(RuntimeError, match="source/ref/sparse"):
        build_codex_plugin_plan(state, CodexInstallOptions())


def test_read_state_rejects_explicit_null_marketplace_source(tmp_path):
    exe = tmp_path / "codex"
    with pytest.raises(RuntimeError) as exc_info:
        read_codex_state(
            exe,
            state_runner(
                exe,
                {
                    "marketplaces": [
                        {
                            "name": "rag-reviewer",
                            "root": str(tmp_path),
                            "marketplaceSource": None,
                        }
                    ]
                },
                {"installed": []},
            ),
        )
    assert str(exc_info.value) == (
        "marketplace list: marketplaces[0].marketplaceSource must be an object"
    )


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
            {
                "marketplaces": [
                    {
                        "name": "rag-reviewer",
                        "root": "/tmp/plugin",
                        "marketplaceSource": [],
                    }
                ]
            },
            {"installed": []},
            "marketplace list: marketplaces[0].marketplaceSource must be an object",
        ),
        (
            {
                "marketplaces": [
                    {
                        "name": "rag-reviewer",
                        "root": "/tmp/plugin",
                        "marketplaceSource": {
                            "sourceType": "git",
                            "source": "mimfort/rag_for_git",
                            "ref": 1,
                            "sparsePaths": [".agents/plugins", "plugin"],
                        },
                    }
                ]
            },
            {"installed": []},
            "marketplace list: marketplaces[0].marketplaceSource.ref must be a string",
        ),
        (
            {
                "marketplaces": [
                    {
                        "name": "rag-reviewer",
                        "root": "/tmp/plugin",
                        "marketplaceSource": {
                            "sourceType": "git",
                            "source": "mimfort/rag_for_git",
                            "ref": "main",
                            "sparsePaths": [".agents/plugins", 1],
                        },
                    }
                ]
            },
            {"installed": []},
            "marketplace list: marketplaces[0].marketplaceSource.sparsePaths[1] must be a string",
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
        MarketplaceState(
            "rag-reviewer",
            tmp_path,
            "mimfort/rag_for_git",
            "main",
            (".agents/plugins", "plugin"),
        ),
        None,
    )
    assert build_codex_plugin_plan(owned, CodexInstallOptions()).marketplace_action == "upgrade"


def make_snapshot(root: Path, version: str) -> Path:
    plugin = root / "plugin"
    (root / ".agents/plugins").mkdir(parents=True)
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / "assets").mkdir()
    (plugin / "skills/ask/references").mkdir(parents=True)
    (plugin / "skills/_common").mkdir(parents=True)
    (plugin / "hooks").mkdir()
    (root / ".agents/plugins/marketplace.json").write_text(
        json.dumps(
            {
                "name": "rag-reviewer",
                "plugins": [
                    {
                        "name": "rag-reviewer",
                        "source": {"source": "local", "path": "./plugin"},
                    }
                ],
            }
        )
    )
    (plugin / ".codex-plugin/plugin.json").write_text(
        json.dumps(
            {
                "name": "rag-reviewer",
                "version": version,
                "skills": "./skills/",
                "repository": "https://github.com/mimfort/rag_for_git",
                "interface": {"composerIcon": "./assets/icon.svg"},
            }
        )
    )
    (plugin / "assets/icon.svg").write_text("<svg/>")
    (plugin / "skills/ask/SKILL.md").write_text("ask")
    (plugin / "skills/ask/references/example.md").write_text("reference")
    (plugin / "skills/_common/shared.md").write_text("shared")
    (plugin / "hooks/hooks.json").write_text("{}")
    return plugin


def finalize_snapshot(plugin: Path, base_version: str = "0.2.27") -> None:
    digest = payload_digest(plugin)
    manifest_path = plugin / ".codex-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = f"{base_version}+codex.{digest}"
    manifest_path.write_text(json.dumps(manifest))


def symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")


def test_snapshot_verifies_dynamic_skills_and_common(tmp_path):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.initial")
    finalize_snapshot(plugin)
    verified = verify_marketplace_snapshot(tmp_path, "0.2.27")
    assert verified.skills == ("ask",)
    assert (verified.plugin_root / "skills/_common/shared.md").is_file()


def test_snapshot_rejects_bundled_mcp(tmp_path):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.000000000000")
    manifest_path = plugin / ".codex-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["mcpServers"] = "./.mcp.json"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="mcpServers"):
        verify_marketplace_snapshot(tmp_path, "0.2.27")


def test_snapshot_rejects_bad_hash(tmp_path):
    make_snapshot(tmp_path, "0.2.27+codex.000000000000")
    with pytest.raises(RuntimeError, match="payload hash"):
        verify_marketplace_snapshot(tmp_path, "0.2.27")


def test_marketplace_conflict_is_not_owned(tmp_path):
    state = MarketplaceState("rag-reviewer", tmp_path, "someone/else")
    assert marketplace_is_owned(state) is False


def test_marketplace_ownership_requires_exact_source_ref_and_sparse(tmp_path):
    exact = MarketplaceState(
        "rag-reviewer",
        tmp_path,
        "mimfort/rag_for_git",
        "main",
        (".agents/plugins", "plugin"),
    )
    assert marketplace_is_owned(exact) is True

    ambiguous = (
        MarketplaceState("rag-reviewer", tmp_path),
        MarketplaceState(
            "rag-reviewer",
            tmp_path,
            "mimfort/rag_for_git",
            None,
            (".agents/plugins", "plugin"),
        ),
        MarketplaceState("rag-reviewer", tmp_path, "mimfort/rag_for_git", "main", None),
        MarketplaceState(
            "rag-reviewer",
            tmp_path,
            "mimfort/rag_for_git",
            "other",
            (".agents/plugins", "plugin"),
        ),
        MarketplaceState(
            "rag-reviewer",
            tmp_path,
            "mimfort/rag_for_git",
            "main",
            ("plugin", ".agents/plugins"),
        ),
    )
    assert all(not marketplace_is_owned(state) for state in ambiguous)


def test_marketplace_ownership_does_not_trust_snapshot_identity(tmp_path):
    make_snapshot(tmp_path, "0.2.27+codex.000000000000")
    assert marketplace_is_owned(MarketplaceState("rag-reviewer", tmp_path)) is False


def test_plan_rejects_ambiguous_marketplace_metadata(tmp_path):
    state = CodexPluginState(
        tmp_path / "codex",
        MarketplaceState("rag-reviewer", tmp_path, "mimfort/rag_for_git"),
        None,
    )
    with pytest.raises(RuntimeError, match="source/ref/sparse"):
        build_codex_plugin_plan(state, CodexInstallOptions())


def test_plan_rejects_foreign_marketplace(tmp_path):
    state = CodexPluginState(
        tmp_path / "codex",
        MarketplaceState("rag-reviewer", tmp_path, "someone/else"),
        None,
    )
    with pytest.raises(RuntimeError, match="другим source/root"):
        build_codex_plugin_plan(state, CodexInstallOptions())


def test_snapshot_rejects_symlinked_root(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    plugin = make_snapshot(root, "0.2.27+codex.initial")
    finalize_snapshot(plugin)
    alias = tmp_path / "snapshot-alias"
    symlink_or_skip(alias, root, directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        verify_marketplace_snapshot(alias, "0.2.27")


def test_snapshot_rejects_symlinked_marketplace_manifest(tmp_path):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.initial")
    finalize_snapshot(plugin)
    manifest = tmp_path / ".agents/plugins/marketplace.json"
    external = tmp_path.parent / f"{tmp_path.name}-marketplace.json"
    external.write_bytes(manifest.read_bytes())
    manifest.unlink()
    symlink_or_skip(manifest, external)

    with pytest.raises(RuntimeError, match="symlink"):
        verify_marketplace_snapshot(tmp_path, "0.2.27")


def test_snapshot_rejects_symlink_anywhere_in_marketplace_metadata(tmp_path):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.initial")
    finalize_snapshot(plugin)
    external = tmp_path.parent / f"{tmp_path.name}-metadata.txt"
    external.write_text("external")
    symlink_or_skip(tmp_path / ".agents/plugins/extra", external)

    with pytest.raises(RuntimeError, match="symlink"):
        verify_marketplace_snapshot(tmp_path, "0.2.27")


def test_snapshot_rejects_symlinked_payload_file(tmp_path):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.initial")
    finalize_snapshot(plugin)
    icon = plugin / "assets/icon.svg"
    external = tmp_path.parent / f"{tmp_path.name}-icon.svg"
    external.write_bytes(icon.read_bytes())
    icon.unlink()
    symlink_or_skip(icon, external)

    with pytest.raises(RuntimeError, match="symlink"):
        verify_marketplace_snapshot(tmp_path, "0.2.27")


def test_snapshot_rejects_symlinked_payload_directory(tmp_path):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.initial")
    finalize_snapshot(plugin)
    external = tmp_path.parent / f"{tmp_path.name}-skill"
    external.mkdir()
    (external / "SKILL.md").write_text("external")
    symlink_or_skip(plugin / "skills/external", external, directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        verify_marketplace_snapshot(tmp_path, "0.2.27")


def test_snapshot_rejects_windows_reparse_point(tmp_path, monkeypatch):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.initial")
    finalize_snapshot(plugin)
    junction = plugin / "skills/ask"
    real_lstat = Path.lstat

    def lstat_with_reparse_point(path):
        result = real_lstat(path)
        if path == junction:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_file_attributes=0x400,
            )
        return result

    monkeypatch.setattr(Path, "lstat", lstat_with_reparse_point)

    with pytest.raises(RuntimeError, match="reparse point"):
        verify_marketplace_snapshot(tmp_path, "0.2.27")


def test_payload_digest_rejects_symlinked_payload_directory(tmp_path):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.initial")
    external = tmp_path.parent / f"{tmp_path.name}-payload"
    external.mkdir()
    (external / "data.txt").write_text("external")
    symlink_or_skip(plugin / "linked-payload", external, directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        payload_digest(plugin)


def test_snapshot_requires_canonical_marketplace_plugin_name(tmp_path):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.initial")
    finalize_snapshot(plugin)
    marketplace_path = tmp_path / ".agents/plugins/marketplace.json"
    marketplace = json.loads(marketplace_path.read_text())
    marketplace["plugins"][0]["name"] = "someone-else"
    marketplace_path.write_text(json.dumps(marketplace))

    with pytest.raises(RuntimeError, match="marketplace plugin name"):
        verify_marketplace_snapshot(tmp_path, "0.2.27")


def test_snapshot_requires_canonical_skills_path(tmp_path):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.initial")
    manifest_path = plugin / ".codex-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["skills"] = "skills/"
    manifest_path.write_text(json.dumps(manifest))
    finalize_snapshot(plugin)

    with pytest.raises(RuntimeError, match="skills must be ./skills/"):
        verify_marketplace_snapshot(tmp_path, "0.2.27")


@pytest.mark.parametrize(
    "icon_kind",
    ["existing-hooks-file", "absolute-in-root", "embedded-nul", "non-string"],
)
def test_snapshot_requires_canonical_composer_icon(tmp_path, icon_kind):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.initial")
    icon_values = {
        "existing-hooks-file": "./hooks/hooks.json",
        "absolute-in-root": str(plugin / "assets/icon.svg"),
        "embedded-nul": "./assets/icon.svg\0suffix",
        "non-string": ["./assets/icon.svg"],
    }
    manifest_path = plugin / ".codex-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["interface"]["composerIcon"] = icon_values[icon_kind]
    manifest_path.write_text(json.dumps(manifest))
    finalize_snapshot(plugin)

    with pytest.raises(RuntimeError) as exc_info:
        verify_marketplace_snapshot(tmp_path, "0.2.27")
    assert str(exc_info.value) == (
        "Codex manifest composerIcon must be ./assets/icon.svg"
    )


def test_snapshot_normalizes_path_resolution_failure(tmp_path, monkeypatch):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.initial")
    finalize_snapshot(plugin)
    real_resolve = Path.resolve

    def resolve_with_failure(path, *args, **kwargs):
        if path == plugin:
            raise OSError("resolution denied")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_with_failure)

    with pytest.raises(RuntimeError) as exc_info:
        verify_marketplace_snapshot(tmp_path, "0.2.27")
    assert str(exc_info.value) == (
        "plugin source: cannot resolve path: resolution denied"
    )


def test_snapshot_rejects_non_object_interface_with_runtime_error(tmp_path):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.initial")
    manifest_path = plugin / ".codex-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["interface"] = []
    manifest_path.write_text(json.dumps(manifest))
    finalize_snapshot(plugin)

    with pytest.raises(RuntimeError, match="interface must be an object"):
        verify_marketplace_snapshot(tmp_path, "0.2.27")


def test_snapshot_rejects_non_string_version_with_runtime_error(tmp_path):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.initial")
    manifest_path = plugin / ".codex-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = 1
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RuntimeError, match="version must be a string"):
        verify_marketplace_snapshot(tmp_path, "0.2.27")


def test_snapshot_rejects_invalid_utf8_with_runtime_error(tmp_path):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.initial")
    finalize_snapshot(plugin)
    (plugin / ".codex-plugin/plugin.json").write_bytes(b"\xff")

    with pytest.raises(RuntimeError, match="Codex manifest"):
        verify_marketplace_snapshot(tmp_path, "0.2.27")


def test_snapshot_rejects_missing_skills_directory_with_runtime_error(tmp_path):
    plugin = make_snapshot(tmp_path, "0.2.27+codex.initial")
    shutil.rmtree(plugin / "skills")
    finalize_snapshot(plugin)

    with pytest.raises(RuntimeError, match="skills directory is missing"):
        verify_marketplace_snapshot(tmp_path, "0.2.27")


def test_run_codex_install_fresh_updates_mcp_and_plugin(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    codex_home = tmp_path / "Codex Home"
    fake = FakeCodex(tmp_path / "bin/codex", repo, codex_home)
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[other]\nvalue = 1\n")
    monkeypatch.setattr(
        "reviewer.install.shutil.which",
        lambda name: "C:/Program Files/uv/uvx.exe" if name == "uvx" else None,
    )
    result = run_codex_install(
        CodexInstallOptions(codex_home=codex_home),
        runner=fake,
        which=lambda name: str(fake.executable),
    )
    assert result.verification is not None
    assert result.verification.skills
    assert "[other]" in config.read_text()
    assert "C:/Program Files/uv/uvx.exe" in config.read_text()
    assert fake.installed is not None and fake.installed["enabled"] is True


def test_fake_codex_state_writes_lf_when_text_output_is_translated(tmp_path, monkeypatch):
    fake = FakeCodex(tmp_path / "bin/codex", tmp_path, tmp_path / "home")

    def write_text_with_crlf(path, data, encoding=None, errors=None, newline=None):
        path.write_bytes(data.replace("\n", "\r\n").encode(encoding or "utf-8"))

    monkeypatch.setattr(Path, "write_text", write_text_with_crlf)

    fake.marketplace = True

    assert b"\r\n" not in fake.config_path.read_bytes()


def test_fake_codex_writes_and_removes_global_marketplace_metadata(tmp_path):
    fake = FakeCodex(tmp_path / "bin/codex", tmp_path, tmp_path / "home")

    fake.marketplace = True

    configured = tomllib.loads(fake.config_path.read_text(encoding="utf-8"))
    assert configured["fake_codex"] == {"marketplace": True}
    assert configured["marketplaces"]["rag-reviewer"] == {
        "source_type": "git",
        "source": "https://github.com/mimfort/rag_for_git.git",
        "ref": "main",
        "sparse_paths": [".agents/plugins", "plugin"],
    }

    fake.marketplace = False

    configured = tomllib.loads(fake.config_path.read_text(encoding="utf-8"))
    assert configured["fake_codex"] == {"marketplace": False}
    assert "marketplaces" not in configured


def test_run_codex_install_dry_run_has_no_mutating_calls(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    codex_home = tmp_path / "home"
    fake = FakeCodex(tmp_path / "codex", repo, codex_home)
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    result = run_codex_install(
        CodexInstallOptions(dry_run=True, codex_home=codex_home),
        runner=fake,
        which=lambda name: str(fake.executable),
    )
    assert result.verification is None
    assert result.mcp_preview is not None
    assert "[mcp_servers.reviewer]" in result.mcp_preview
    mutating = []
    for call in fake.calls:
        tail = call[1:]
        if tail[-1:] == ("--help",):
            continue
        if tail[:3] in {
            ("plugin", "marketplace", "add"),
            ("plugin", "marketplace", "upgrade"),
        } or tail[:2] == ("plugin", "add"):
            mutating.append(call)
    assert mutating == []


def test_plugin_add_failure_restores_exact_config(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    codex_home = tmp_path / "home"
    fake = FakeCodex(tmp_path / "codex", repo, codex_home)
    fake.fail = ("plugin", "add")
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True)
    original = "# exact\n[other]\nvalue = 'keep'\n"
    config.write_text(original)
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    with pytest.raises(RuntimeError, match="plugin add"):
        run_codex_install(
            CodexInstallOptions(codex_home=codex_home),
            runner=fake,
            which=lambda name: str(fake.executable),
        )
    assert config.read_text() == original
    assert list(codex_home.glob("config.toml.rag-reviewer.*.bak"))


def test_invalid_marketplace_snapshot_restores_config(tmp_path, monkeypatch):
    empty_snapshot = tmp_path / "invalid snapshot"
    empty_snapshot.mkdir()
    codex_home = tmp_path / "home"
    fake = FakeCodex(tmp_path / "codex", empty_snapshot, codex_home)
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True)
    original = "[other]\nvalue = 1\n"
    config.write_text(original)
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    with pytest.raises(RuntimeError, match="snapshot verification"):
        run_codex_install(
            CodexInstallOptions(codex_home=codex_home),
            runner=fake,
            which=lambda name: str(fake.executable),
        )
    assert config.read_text() == original


def test_marketplace_add_failure_restores_config(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    codex_home = tmp_path / "home"
    fake = FakeCodex(tmp_path / "codex", repo, codex_home)
    fake.fail = ("plugin", "marketplace", "add")
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True)
    original = "[other]\nvalue = 1\n"
    config.write_text(original)
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    with pytest.raises(RuntimeError, match="marketplace add"):
        run_codex_install(
            CodexInstallOptions(codex_home=codex_home),
            runner=fake,
            which=lambda name: str(fake.executable),
        )
    assert config.read_text() == original


def test_mcp_write_failure_restores_config_and_skips_plugin_add(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    codex_home = tmp_path / "home"
    fake = FakeCodex(tmp_path / "codex", repo, codex_home)
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True)
    original = "[other]\nvalue = 1\n"
    config.write_text(original)
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    monkeypatch.setattr(
        "reviewer.install.apply_plan",
        lambda plan: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(RuntimeError, match="MCP config update"):
        run_codex_install(
            CodexInstallOptions(codex_home=codex_home),
            runner=fake,
            which=lambda name: str(fake.executable),
        )
    assert config.read_text() == original
    assert not any(
        call[1:3] == ("plugin", "add") and call[-1:] != ("--help",)
        for call in fake.calls
    )


def test_post_verification_failure_restores_previous_selection(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    exe = tmp_path / "codex"
    codex_home = tmp_path / "home"
    fake = FakeCodex(exe, repo, codex_home)
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True)
    original = "[plugins]\nselected = 'old'\n"
    config.write_text(original)
    marketplace = MarketplaceState(
        "rag-reviewer",
        repo,
        "mimfort/rag_for_git",
        "main",
        (".agents/plugins", "plugin"),
    )
    old_plugin = PluginState(
        "rag-reviewer", "rag-reviewer", "0.2.26+codex.old", True, True
    )
    wrong_plugin = PluginState(
        "rag-reviewer", "rag-reviewer", "wrong-version", True, True
    )
    states = iter(
        [
            CodexPluginState(exe, marketplace, old_plugin),
            CodexPluginState(exe, marketplace, old_plugin),
            CodexPluginState(exe, marketplace, wrong_plugin),
            CodexPluginState(exe, marketplace, old_plugin),
        ]
    )
    monkeypatch.setattr(
        "reviewer.install_codex.read_codex_state",
        lambda executable, runner, **kwargs: next(states),
    )
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    with pytest.raises(RuntimeError, match="installed version"):
        run_codex_install(
            CodexInstallOptions(codex_home=codex_home),
            runner=fake,
            which=lambda name: str(exe),
        )
    assert config.read_text() == original


def test_ambiguous_refreshed_marketplace_stops_before_mcp_and_plugin(
    tmp_path, monkeypatch
):
    repo = Path(__file__).resolve().parents[2]
    codex_home = tmp_path / "home"
    fake = FakeCodex(tmp_path / "codex", repo, codex_home)
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True)
    original = b"[other]\nvalue = 1\n"
    config.write_bytes(original)
    applied = []

    def ambiguous_runner(argv: tuple[str, ...]) -> CommandResult:
        response = fake(argv)
        if fake.marketplace and argv[1:] == (
            "plugin",
            "marketplace",
            "list",
            "--json",
        ):
            fake.config_path.write_bytes(
                fake._without_table(
                    fake.config_path.read_text(encoding="utf-8"),
                    fake._MARKETPLACE_HEADER,
                ).encode("utf-8")
            )
            response = CommandResult(
                argv,
                0,
                json.dumps(
                    {
                        "marketplaces": [
                            {
                                "name": "rag-reviewer",
                                "root": str(repo),
                                "source": "mimfort/rag_for_git",
                            }
                        ]
                    }
                ),
                "",
            )
        return response

    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    monkeypatch.setattr(
        "reviewer.install.apply_plan", lambda plan: applied.append(plan)
    )

    with pytest.raises(CodexInstallError) as exc_info:
        run_codex_install(
            CodexInstallOptions(codex_home=codex_home),
            runner=ambiguous_runner,
            which=lambda name: str(fake.executable),
        )

    error = exc_info.value
    assert error.phase == "marketplace ownership verification"
    assert error.argv[1:4] == ("plugin", "marketplace", "add")
    assert "неполные или чужие source/ref/sparse metadata" in error.detail
    assert config.read_bytes() == original
    assert applied == []
    assert not any(
        call[1:3] == ("plugin", "add") and call[-1:] != ("--help",)
        for call in fake.calls
    )


def test_rollback_state_mismatch_reports_backup(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    exe = tmp_path / "codex"
    codex_home = tmp_path / "home"
    fake = FakeCodex(exe, repo, codex_home)
    fake.fail = ("plugin", "add")
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True)
    original = b"# exact bytes\n[plugins]\nselected = 'old'\n"
    config.write_bytes(original)
    marketplace = MarketplaceState(
        "rag-reviewer",
        repo,
        "mimfort/rag_for_git",
        "main",
        (".agents/plugins", "plugin"),
    )
    old_plugin = PluginState(
        "rag-reviewer", "rag-reviewer", "0.2.26+codex.old", True, True
    )
    wrong_plugin = PluginState(
        "rag-reviewer", "rag-reviewer", "wrong-version", True, True
    )
    states = iter(
        [
            CodexPluginState(exe, marketplace, old_plugin),
            CodexPluginState(exe, marketplace, old_plugin),
            CodexPluginState(exe, marketplace, wrong_plugin),
        ]
    )
    monkeypatch.setattr(
        "reviewer.install_codex.read_codex_state",
        lambda executable, runner, **kwargs: next(states),
    )
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")

    with pytest.raises(RuntimeError) as exc_info:
        run_codex_install(
            CodexInstallOptions(codex_home=codex_home),
            runner=fake,
            which=lambda name: str(exe),
        )

    message = str(exc_info.value)
    assert "config rollback failed" in message
    assert "config rollback не восстановил предыдущую plugin selection" in message
    assert "config.toml.rag-reviewer." in message
    assert ".bak" in message
    assert config.read_bytes() == original


def test_default_runner_receives_effective_codex_home_without_global_mutation(
    tmp_path, monkeypatch
):
    ambient_home = tmp_path / "ambient"
    effective_home = tmp_path / "effective"
    captured: list[dict[str, object]] = []

    def fake_run(argv, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("CODEX_HOME", str(ambient_home))
    monkeypatch.setattr("reviewer.install_codex.subprocess.run", fake_run)

    runner = codex_install._runner_for_codex_home(
        codex_install.subprocess_runner, effective_home
    )
    runner(("codex", "plugin", "--help"))

    assert captured[0]["env"] is not os.environ
    assert captured[0]["env"]["CODEX_HOME"] == str(effective_home)
    assert os.environ["CODEX_HOME"] == str(ambient_home)


def test_distinct_mcp_target_and_effective_home_restore_exact_bytes(
    tmp_path, monkeypatch
):
    repo = Path(__file__).resolve().parents[2]
    effective_home = tmp_path / "effective-home"
    effective_config = effective_home / "config.toml"
    mcp_path = tmp_path / "mcp-only.toml"
    effective_original = b"[other]\nvalue = 'effective'\n"
    mcp_original = b"[other]\nvalue = 'mcp'\n"
    effective_home.mkdir()
    effective_config.write_bytes(effective_original)
    mcp_path.write_bytes(mcp_original)
    fake = FakeCodex(tmp_path / "codex", repo, effective_home)
    fake.fail = ("plugin", "add")
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")

    with pytest.raises(CodexInstallError, match="plugin add"):
        run_codex_install(
            CodexInstallOptions(codex_home=effective_home, mcp_path=mcp_path),
            runner=fake,
            which=lambda name: str(fake.executable),
        )

    assert effective_config.read_bytes() == effective_original
    assert mcp_path.read_bytes() == mcp_original
    assert list(effective_home.glob("config.toml.rag-reviewer.*.bak"))
    assert list(tmp_path.glob("mcp-only.toml.rag-reviewer.*.bak"))


def test_distinct_mcp_target_exposes_both_transaction_backups(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    effective_home = tmp_path / "effective-home"
    effective_config = effective_home / "config.toml"
    mcp_path = tmp_path / "mcp-only.toml"
    effective_original = b"[other]\nvalue = 'effective'\n"
    mcp_original = b"[other]\nvalue = 'mcp'\n"
    effective_home.mkdir()
    effective_config.write_bytes(effective_original)
    mcp_path.write_bytes(mcp_original)
    fake = FakeCodex(tmp_path / "codex", repo, effective_home)
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")

    result = run_codex_install(
        CodexInstallOptions(codex_home=effective_home, mcp_path=mcp_path),
        runner=fake,
        which=lambda name: str(fake.executable),
    )

    assert result.config_backup == result.config_backups[0]
    assert len(result.config_backups) == 2
    assert {path.read_bytes() for path in result.config_backups} == {
        effective_original,
        mcp_original,
    }


def test_plugin_mcp_clobber_is_verified_and_rolled_back(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    codex_home = tmp_path / "home"
    config = codex_home / "config.toml"
    codex_home.mkdir()
    original = b"[other]\nvalue = 1\n"
    config.write_bytes(original)
    fake = FakeCodex(tmp_path / "codex", repo, codex_home)
    fake.clobber_mcp_on_plugin_add = True
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")

    with pytest.raises(CodexInstallError) as exc_info:
        run_codex_install(
            CodexInstallOptions(codex_home=codex_home),
            runner=fake,
            which=lambda name: str(fake.executable),
        )

    assert exc_info.value.phase == "MCP config verification"
    assert config.read_bytes() == original
    assert any(
        call[1:3] == ("plugin", "add") and call[-1:] != ("--help",)
        for call in fake.calls
    )


def test_mcp_verification_tolerates_codex_plugin_table_spacing(tmp_path):
    """Codex `plugin add` keeps MCP settings but inserts a blank separator."""
    config = tmp_path / "config.toml"
    plan = generic_install.build_plan(
        generic_install.CLIENTS["codex"], path_override=str(config)
    )
    config.write_text(
        plan.content
        + '\n[plugins."rag-reviewer@rag-reviewer"]\n'
        + "enabled = true\n",
        encoding="utf-8",
    )

    codex_install._verify_mcp_config(generic_install, config, "latest")


def test_mcp_verification_preserves_additive_reviewer_options(tmp_path):
    config = tmp_path / "config.toml"
    plan = generic_install.build_plan(
        generic_install.CLIENTS["codex"], path_override=str(config)
    )
    config.write_text(
        plan.content
        + "startup_timeout_sec = 30.0\n\n"
        + "[mcp_servers.reviewer.tools]\n"
        + 'approval_mode = "approve"\n',
        encoding="utf-8",
    )

    codex_install._verify_mcp_config(generic_install, config, "latest")


@pytest.mark.parametrize(
    "mutation",
    ("command", "args", "disabled"),
    ids=("command", "args", "disabled"),
)
def test_mcp_verification_rejects_unusable_reviewer_entry(tmp_path, mutation):
    config = tmp_path / "config.toml"
    plan = generic_install.build_plan(
        generic_install.CLIENTS["codex"], path_override=str(config)
    )
    expected = tomllib.loads(plan.content)["mcp_servers"]["reviewer"]
    if mutation == "command":
        content = (
            '[mcp_servers.reviewer]\ncommand = "wrong"\n'
            f"args = {json.dumps(expected['args'])}\n"
        )
    elif mutation == "args":
        content = (
            f"[mcp_servers.reviewer]\ncommand = {json.dumps(expected['command'])}\n"
            'args = ["--from", "rag-reviewer@latest", "wrong-mcp"]\n'
        )
    else:
        content = plan.content + "enabled = false\n"
    config.write_text(content, encoding="utf-8")

    with pytest.raises(CodexInstallError, match="missing or differs"):
        codex_install._verify_mcp_config(generic_install, config, "latest")


@pytest.mark.parametrize("mode", ("noop", "partial"))
def test_rollback_rejects_nonexact_config_restore(tmp_path, monkeypatch, mode):
    repo = Path(__file__).resolve().parents[2]
    codex_home = tmp_path / "home"
    config = codex_home / "config.toml"
    codex_home.mkdir()
    original = b"[other]\nvalue = 1\n"
    config.write_bytes(original)
    fake = FakeCodex(tmp_path / "codex", repo, codex_home)
    fake.fail = ("plugin", "add")
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")

    def incomplete_restore(snapshot):
        if mode == "partial":
            snapshot.path.write_bytes(snapshot.content + b"# partial restore\n")

    monkeypatch.setattr("reviewer.install_codex._restore_config", incomplete_restore)

    with pytest.raises(RuntimeError, match="config rollback failed") as exc_info:
        run_codex_install(
            CodexInstallOptions(codex_home=codex_home, include_mcp=False),
            runner=fake,
            which=lambda name: str(fake.executable),
        )

    assert "config.toml.rag-reviewer." in str(exc_info.value)


def test_rollback_removes_new_effective_and_mcp_config_files(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    effective_home = tmp_path / "effective-home"
    mcp_path = tmp_path / "mcp-only.toml"
    fake = FakeCodex(tmp_path / "codex", repo, effective_home)
    fake.fail = ("plugin", "add")
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")

    with pytest.raises(CodexInstallError, match="plugin add"):
        run_codex_install(
            CodexInstallOptions(codex_home=effective_home, mcp_path=mcp_path),
            runner=fake,
            which=lambda name: str(fake.executable),
        )

    assert not (effective_home / "config.toml").exists()
    assert not mcp_path.exists()


def test_malformed_refreshed_marketplace_has_ownership_error_phase(
    tmp_path, monkeypatch
):
    repo = Path(__file__).resolve().parents[2]
    codex_home = tmp_path / "home"
    config = codex_home / "config.toml"
    codex_home.mkdir()
    original = b"[other]\nvalue = 1\n"
    config.write_bytes(original)
    fake = FakeCodex(tmp_path / "codex", repo, codex_home)
    fake.malformed_marketplace_after_mutation = True
    applied = []
    monkeypatch.setattr("reviewer.install.shutil.which", lambda name: "/opt/uvx")
    monkeypatch.setattr("reviewer.install.apply_plan", lambda plan: applied.append(plan))

    with pytest.raises(CodexInstallError) as exc_info:
        run_codex_install(
            CodexInstallOptions(codex_home=codex_home),
            runner=fake,
            which=lambda name: str(fake.executable),
        )

    error = exc_info.value
    assert error.phase == "marketplace ownership verification"
    assert error.argv[1:4] == ("plugin", "marketplace", "add")
    assert "marketplaceSource must be an object" in error.detail
    assert config.read_bytes() == original
    assert applied == []
    assert not any(
        call[1:3] == ("plugin", "add") and call[-1:] != ("--help",)
        for call in fake.calls
    )


def test_snapshot_backup_names_are_unique_and_exclusive(tmp_path):
    config = tmp_path / "config.toml"
    original = b"# original bytes\n"
    config.write_bytes(original)

    first = codex_install._snapshot_config(config)
    first.backup_path.write_bytes(b"first backup sentinel")
    second = codex_install._snapshot_config(config)

    assert first.backup_path != second.backup_path
    assert first.backup_path.read_bytes() == b"first backup sentinel"
    assert second.backup_path.read_bytes() == original


def copy_skill(plugin_root: Path, skills_root: Path, name: str) -> None:
    import shutil

    shutil.copytree(plugin_root / "skills" / name, skills_root / name)


def test_legacy_candidates_require_stamp_or_exact_payload(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    plugin = repo / "plugin"
    skills = tmp_path / "skills"
    skills.mkdir()
    copy_skill(plugin, skills, "ask")
    copy_skill(plugin, skills, "solve-task")
    (skills / "solve-task/SKILL.md").write_text("locally modified")

    candidates = find_owned_legacy_skills(skills, plugin)

    assert [(item.name, item.reason) for item in candidates] == [
        ("ask", "exact payload match")
    ]
    assert (skills / "solve-task").is_dir()


def test_legacy_migration_moves_owned_and_keeps_ambiguous(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    plugin = repo / "plugin"
    skills = tmp_path / "skills"
    skills.mkdir()
    copy_skill(plugin, skills, "ask")
    copy_skill(plugin, skills, "solve-task")
    (skills / "solve-task/SKILL.md").write_text("modified")

    result = migrate_legacy_skills(skills, plugin)

    assert result.moved == ("ask",)
    assert result.backup_root is not None
    assert (result.backup_root / "skills/ask/SKILL.md").is_file()
    assert (skills / "solve-task/SKILL.md").read_text() == "modified"
    assert result.warnings


def test_legacy_migration_restores_already_moved_directories(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    plugin = repo / "plugin"
    skills = tmp_path / "skills"
    skills.mkdir()
    copy_skill(plugin, skills, "ask")
    copy_skill(plugin, skills, "finish-task")
    original_replace = Path.replace

    def flaky_replace(self: Path, target: Path) -> Path:
        if self.parent == skills and self.name == "finish-task":
            raise OSError("injected move failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    result = migrate_legacy_skills(skills, plugin)

    assert result.moved == ()
    assert (skills / "ask/SKILL.md").is_file()
    assert (skills / "finish-task/SKILL.md").is_file()
    assert any("восстановлена" in warning for warning in result.warnings)
