import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from reviewer.install_codex import (
    CodexInstallOptions,
    CodexPluginState,
    CommandResult,
    MarketplaceState,
    build_codex_plugin_plan,
    detect_codex_capabilities,
    find_codex_executable,
    marketplace_is_owned,
    payload_digest,
    read_codex_state,
    verify_marketplace_snapshot,
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
