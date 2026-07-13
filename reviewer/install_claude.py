"""Native Claude Code plugin lifecycle used by ``reviewer install``.

The adapter intentionally owns no Claude configuration files.  It uses only
the public Claude CLI marketplace/plugin commands, then verifies their JSON
state before reporting a successful global installation.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from reviewer.install_codex import Runner, subprocess_runner

CLAUDE_MARKETPLACE_NAME = "rag-reviewer-marketplace"
CLAUDE_PLUGIN_ID = "rag-reviewer@rag-reviewer-marketplace"
CLAUDE_MARKETPLACE_SOURCE = "https://github.com/mimfort/rag_for_git.git"
CLAUDE_SPARSE = (".claude-plugin", "plugin")


@dataclass(frozen=True)
class ClaudeCapabilities:
    executable: Path


@dataclass(frozen=True)
class ClaudeMarketplaceState:
    name: str
    source: str
    url: str | None


@dataclass(frozen=True)
class ClaudePluginState:
    plugin_id: str
    scope: str
    enabled: bool


@dataclass(frozen=True)
class ClaudeInstallState:
    executable: Path
    marketplace: ClaudeMarketplaceState | None
    plugin: ClaudePluginState | None


@dataclass(frozen=True)
class ClaudeInstallOptions:
    dry_run: bool = False


@dataclass(frozen=True)
class ClaudeInstallPlan:
    state: ClaudeInstallState
    options: ClaudeInstallOptions
    marketplace_argv: tuple[str, ...]
    marketplace_update_argv: tuple[str, ...]
    plugin_argv: tuple[str, ...]


class ClaudeInstallError(RuntimeError):
    def __init__(self, phase: str, argv: tuple[str, ...], detail: str):
        self.phase = phase
        self.argv = argv
        self.detail = detail
        rendered_argv = " ".join(argv) if argv else "<none>"
        super().__init__(f"{phase}: {detail}; argv={rendered_argv}")


@dataclass(frozen=True)
class ClaudeInstallResult:
    plan: ClaudeInstallPlan
    marketplace: ClaudeMarketplaceState | None
    plugin: ClaudePluginState | None


def find_claude_executable(
    which: Callable[[str], str | None] = shutil.which,
) -> Path:
    found = which("claude")
    if not found:
        raise RuntimeError("Claude Code CLI не найден; установите или обновите Claude Code")
    return Path(found).resolve()


def _require_help(runner: Runner, argv: tuple[str, ...], tokens: tuple[str, ...]) -> None:
    response = runner(argv)
    text = response.stdout + response.stderr
    missing = [token for token in tokens if token not in text]
    if response.returncode or missing:
        detail = (
            f"Claude Code CLI не поддерживает {' '.join(argv[1:-1])}: "
            f"отсутствуют {missing}"
        )
        if response.returncode and response.stderr.strip():
            detail = f"{detail}: {response.stderr.strip()}"
        raise ClaudeInstallError("capability detection", argv, detail)


def detect_claude_capabilities(executable: Path, runner: Runner) -> ClaudeCapabilities:
    exe = str(executable)
    _require_help(
        runner,
        (exe, "plugin", "marketplace", "add", "--help"),
        ("--scope", "--sparse"),
    )
    _require_help(
        runner,
        (exe, "plugin", "marketplace", "update", "--help"),
        (),
    )
    _require_help(
        runner,
        (exe, "plugin", "marketplace", "list", "--help"),
        ("--json",),
    )
    _require_help(
        runner,
        (exe, "plugin", "install", "--help"),
        ("--scope",),
    )
    _require_help(runner, (exe, "plugin", "update", "--help"), ("--scope",))
    _require_help(runner, (exe, "plugin", "list", "--help"), ("--json",))
    return ClaudeCapabilities(executable)


def _json_array(runner: Runner, argv: tuple[str, ...], phase: str) -> list[dict[str, Any]]:
    response = runner(argv)
    if response.returncode:
        raise ClaudeInstallError(phase, argv, response.stderr.strip() or "command failed")
    try:
        data: Any = json.loads(response.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeInstallError(phase, argv, f"invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ClaudeInstallError(phase, argv, "expected JSON array")
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ClaudeInstallError(phase, argv, f"entry {index} must be an object")
        rows.append(item)
    return rows


def _required_string(
    item: dict[str, Any], field: str, *, phase: str, argv: tuple[str, ...], label: str
) -> str:
    if field not in item:
        raise ClaudeInstallError(phase, argv, f"{label} missing field {field}")
    value = item[field]
    if not isinstance(value, str):
        raise ClaudeInstallError(phase, argv, f"{label}.{field} must be a string")
    return value


def _required_boolean(
    item: dict[str, Any], field: str, *, phase: str, argv: tuple[str, ...], label: str
) -> bool:
    if field not in item:
        raise ClaudeInstallError(phase, argv, f"{label} missing field {field}")
    value = item[field]
    if not isinstance(value, bool):
        raise ClaudeInstallError(phase, argv, f"{label}.{field} must be a boolean")
    return value


def _optional_string(
    item: dict[str, Any], field: str, *, phase: str, argv: tuple[str, ...], label: str
) -> str | None:
    if field not in item:
        return None
    value = item[field]
    if value is not None and not isinstance(value, str):
        raise ClaudeInstallError(phase, argv, f"{label}.{field} must be a string")
    return value


def read_claude_state(executable: Path, runner: Runner) -> ClaudeInstallState:
    exe = str(executable)
    marketplace_argv = (exe, "plugin", "marketplace", "list", "--json")
    plugin_argv = (exe, "plugin", "list", "--json")
    marketplaces = _json_array(runner, marketplace_argv, "marketplace list")
    plugins = _json_array(runner, plugin_argv, "plugin list")

    marketplace: ClaudeMarketplaceState | None = None
    for index, item in enumerate(marketplaces):
        label = f"marketplace list: entries[{index}]"
        name = _required_string(
            item, "name", phase="marketplace list", argv=marketplace_argv, label=label
        )
        if name != CLAUDE_MARKETPLACE_NAME:
            continue
        source = _required_string(
            item, "source", phase="marketplace list", argv=marketplace_argv, label=label
        )
        url = _optional_string(
            item, "url", phase="marketplace list", argv=marketplace_argv, label=label
        )
        if marketplace is not None:
            raise ClaudeInstallError(
                "marketplace list",
                marketplace_argv,
                f"multiple {CLAUDE_MARKETPLACE_NAME!r} entries",
            )
        marketplace = ClaudeMarketplaceState(name, source, url)

    plugin: ClaudePluginState | None = None
    for index, item in enumerate(plugins):
        label = f"plugin list: entries[{index}]"
        plugin_id = _required_string(
            item, "id", phase="plugin list", argv=plugin_argv, label=label
        )
        if plugin_id != CLAUDE_PLUGIN_ID:
            continue
        scope = _required_string(
            item, "scope", phase="plugin list", argv=plugin_argv, label=label
        )
        enabled = _required_boolean(
            item, "enabled", phase="plugin list", argv=plugin_argv, label=label
        )
        if scope != "user":
            continue
        if plugin is not None:
            raise ClaudeInstallError(
                "plugin list", plugin_argv, f"multiple {CLAUDE_PLUGIN_ID!r} entries"
            )
        plugin = ClaudePluginState(plugin_id, scope, enabled)

    return ClaudeInstallState(executable, marketplace, plugin)


def marketplace_is_owned(marketplace: ClaudeMarketplaceState | None) -> bool:
    return (
        marketplace is not None
        and marketplace.name == CLAUDE_MARKETPLACE_NAME
        and marketplace.source == "git"
        and marketplace.url == CLAUDE_MARKETPLACE_SOURCE
    )


def build_claude_install_plan(
    state: ClaudeInstallState, options: ClaudeInstallOptions
) -> ClaudeInstallPlan:
    exe = str(state.executable)
    marketplace_argv = (
        exe,
        "plugin",
        "marketplace",
        "add",
        CLAUDE_MARKETPLACE_SOURCE,
        "--scope",
        "user",
        "--sparse",
        *CLAUDE_SPARSE,
    )
    marketplace_update_argv = (
        exe,
        "plugin",
        "marketplace",
        "update",
        CLAUDE_MARKETPLACE_NAME,
    )
    plugin_action = "update" if state.plugin is not None else "install"
    plugin_argv = (
        exe,
        "plugin",
        plugin_action,
        CLAUDE_PLUGIN_ID,
        "--scope",
        "user",
    )
    return ClaudeInstallPlan(
        state,
        options,
        marketplace_argv,
        marketplace_update_argv,
        plugin_argv,
    )


def _checked(runner: Runner, argv: tuple[str, ...], phase: str) -> None:
    response = runner(argv)
    if response.returncode:
        detail = response.stderr.strip() or response.stdout.strip() or "command failed"
        raise ClaudeInstallError(phase, argv, detail)


def _verify_marketplace(state: ClaudeInstallState, argv: tuple[str, ...]) -> None:
    marketplace = state.marketplace
    if marketplace is None:
        raise ClaudeInstallError(
            "marketplace ownership verification",
            argv,
            f"marketplace {CLAUDE_MARKETPLACE_NAME!r} is missing after mutation",
        )
    if (
        marketplace.name != CLAUDE_MARKETPLACE_NAME
        or marketplace.source != "git"
        or marketplace.url != CLAUDE_MARKETPLACE_SOURCE
    ):
        raise ClaudeInstallError(
            "marketplace ownership verification",
            argv,
            "marketplace after mutation does not have the canonical git HTTPS source",
        )


def _verify_plugin(state: ClaudeInstallState, argv: tuple[str, ...]) -> None:
    plugin = state.plugin
    if plugin is None:
        raise ClaudeInstallError(
            "post-install verification",
            argv,
            f"plugin {CLAUDE_PLUGIN_ID!r} is not installed",
        )
    if plugin.scope != "user" or not plugin.enabled:
        raise ClaudeInstallError(
            "post-install verification",
            argv,
            f"plugin must be user-scoped and enabled; scope={plugin.scope!r}, "
            f"enabled={plugin.enabled!r}",
        )


def run_claude_install(
    options: ClaudeInstallOptions,
    *,
    runner: Runner = subprocess_runner,
    which: Callable[[str], str | None] = shutil.which,
) -> ClaudeInstallResult:
    executable = find_claude_executable(which)
    if options.dry_run:
        plan = build_claude_install_plan(
            ClaudeInstallState(executable, None, None), options
        )
        return ClaudeInstallResult(plan, None, None)

    detect_claude_capabilities(executable, runner)
    state = read_claude_state(executable, runner)
    plan = build_claude_install_plan(state, options)
    _checked(runner, plan.marketplace_argv, "marketplace add")
    try:
        added_marketplace = read_claude_state(executable, runner)
    except ClaudeInstallError as exc:
        raise ClaudeInstallError(
            "marketplace ownership verification", exc.argv, exc.detail
        ) from exc
    _verify_marketplace(added_marketplace, plan.marketplace_argv)
    _checked(runner, plan.marketplace_update_argv, "marketplace update")
    try:
        refreshed_marketplace = read_claude_state(executable, runner)
    except ClaudeInstallError as exc:
        raise ClaudeInstallError(
            "marketplace ownership verification", exc.argv, exc.detail
        ) from exc
    _verify_marketplace(refreshed_marketplace, plan.marketplace_update_argv)
    _checked(runner, plan.plugin_argv, f"plugin {plan.plugin_argv[2]}")
    try:
        refreshed = read_claude_state(executable, runner)
    except ClaudeInstallError as exc:
        raise ClaudeInstallError("post-install verification", exc.argv, exc.detail) from exc
    _verify_marketplace(refreshed, plan.marketplace_argv)
    _verify_plugin(refreshed, (str(executable), "plugin", "list", "--json"))
    return ClaudeInstallResult(plan, refreshed.marketplace, refreshed.plugin)
