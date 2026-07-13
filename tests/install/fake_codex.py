import json
import tomllib
from pathlib import Path

from reviewer.install_codex import CommandResult


class FakeCodex:
    """Файловый Codex fake: его состояние живёт в config.toml effective home."""

    _STATE_HEADER = "[fake_codex]"
    _MARKETPLACE_HEADER = "[marketplaces.rag-reviewer]"
    _MARKETPLACE_CONFIG = (
        "[marketplaces.rag-reviewer]\n"
        'source_type = "git"\n'
        'source = "https://github.com/mimfort/rag_for_git.git"\n'
        'ref = "main"\n'
        'sparse_paths = [".agents/plugins", "plugin"]\n'
    )

    def __init__(self, executable: Path, marketplace_root: Path, codex_home: Path):
        self.executable = executable
        self.marketplace_root = marketplace_root
        self.codex_home = codex_home
        self.calls: list[tuple[str, ...]] = []
        self.fail: tuple[str, ...] | None = None
        self.clobber_mcp_on_plugin_add = False
        self.clobber_marketplace_metadata_on_plugin_add = False
        self.malformed_marketplace_after_mutation = False
        self.marketplace_json_source = "https://github.com/mimfort/rag_for_git.git"
        self.marketplace_json_ref: str | None = None
        self.marketplace_json_sparse_paths: tuple[str, ...] | None = None

    @property
    def config_path(self) -> Path:
        return self.codex_home / "config.toml"

    @staticmethod
    def _without_table(raw: str, header: str) -> str:
        lines = raw.splitlines(keepends=True)
        output: list[str] = []
        skipping = False
        for line in lines:
            stripped = line.strip()
            if stripped == header:
                skipping = True
                continue
            if skipping and stripped.startswith("[") and stripped.endswith("]"):
                skipping = False
            if not skipping:
                output.append(line)
        return "".join(output)

    def _state(self) -> dict[str, object]:
        if not self.config_path.exists():
            return {}
        parsed = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        state = parsed.get("fake_codex")
        return state if isinstance(state, dict) else {}

    def _set_state(self, **updates: object) -> None:
        state = {**self._state(), **updates}
        raw = self.config_path.read_text(encoding="utf-8") if self.config_path.exists() else ""
        content = self._without_table(raw, self._STATE_HEADER)
        if content and not content.endswith("\n"):
            content += "\n"
        lines = [self._STATE_HEADER]
        for key in sorted(state):
            value = state[key]
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, str):
                rendered = json.dumps(value, ensure_ascii=False)
            else:
                raise TypeError(f"unsupported fake Codex state: {key}={value!r}")
            lines.append(f"{key} = {rendered}")
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_bytes((content + "\n".join(lines) + "\n").encode("utf-8"))

    def _set_marketplace_metadata(self, present: bool) -> None:
        raw = self.config_path.read_text(encoding="utf-8") if self.config_path.exists() else ""
        content = self._without_table(raw, self._MARKETPLACE_HEADER)
        if present:
            if content and not content.endswith("\n"):
                content += "\n"
            content += self._MARKETPLACE_CONFIG
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_bytes(content.encode("utf-8"))

    @property
    def marketplace(self) -> bool:
        return self._state().get("marketplace") is True

    @marketplace.setter
    def marketplace(self, value: bool) -> None:
        self._set_state(marketplace=value)
        self._set_marketplace_metadata(value)

    @property
    def installed(self) -> dict[str, object] | None:
        state = self._state()
        version = state.get("plugin_version")
        if not isinstance(version, str):
            return None
        marketplace = state.get("plugin_marketplace")
        return {
            "name": "rag-reviewer",
            "marketplaceName": marketplace
            if isinstance(marketplace, str)
            else "rag-reviewer",
            "version": version,
            "installed": state.get("plugin_installed") is True,
            "enabled": state.get("plugin_enabled") is True,
        }

    def _clobber_reviewer_mcp(self) -> None:
        if not self.config_path.exists():
            return
        raw = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_bytes(
            self._without_table(raw, "[mcp_servers.reviewer]").encode("utf-8")
        )

    def __call__(self, argv: tuple[str, ...]) -> CommandResult:
        self.calls.append(argv)
        tail = argv[1:]
        if tail[-1:] == ("--help",):
            return CommandResult(
                argv, 0, "add marketplace list --json --sparse --ref --available", ""
            )
        if self.fail is not None and tail[: len(self.fail)] == self.fail:
            return CommandResult(argv, 1, "", "injected failure")
        if tail == ("plugin", "marketplace", "list", "--json"):
            if self.marketplace and self.malformed_marketplace_after_mutation:
                rows = [
                    {
                        "name": "rag-reviewer",
                        "root": str(self.marketplace_root),
                        "marketplaceSource": [],
                    }
                ]
            elif self.marketplace:
                marketplace_source: dict[str, object] = {
                    "sourceType": "git",
                    "source": self.marketplace_json_source,
                }
                if self.marketplace_json_ref is not None:
                    marketplace_source["ref"] = self.marketplace_json_ref
                if self.marketplace_json_sparse_paths is not None:
                    marketplace_source["sparsePaths"] = list(
                        self.marketplace_json_sparse_paths
                    )
                rows = [
                    {
                        "name": "rag-reviewer",
                        "root": str(self.marketplace_root),
                        "marketplaceSource": marketplace_source,
                    }
                ]
            else:
                rows = []
            return CommandResult(argv, 0, json.dumps({"marketplaces": rows}), "")
        if tail == ("plugin", "list", "--available", "--json"):
            installed = [self.installed] if self.installed is not None else []
            return CommandResult(
                argv, 0, json.dumps({"installed": installed, "available": []}), ""
            )
        if tail[:3] == ("plugin", "marketplace", "add"):
            self.marketplace = True
            return CommandResult(argv, 0, json.dumps({"name": "rag-reviewer"}), "")
        if tail[:3] == ("plugin", "marketplace", "upgrade"):
            self.marketplace = True
            return CommandResult(argv, 0, json.dumps({"name": "rag-reviewer"}), "")
        if tail[:2] == ("plugin", "add"):
            manifest = json.loads(
                (self.marketplace_root / "plugin/.codex-plugin/plugin.json").read_text()
            )
            self._set_state(
                plugin_enabled=True,
                plugin_installed=True,
                plugin_marketplace="rag-reviewer",
                plugin_version=manifest["version"],
            )
            if self.clobber_mcp_on_plugin_add:
                self._clobber_reviewer_mcp()
            if self.clobber_marketplace_metadata_on_plugin_add:
                self._set_marketplace_metadata(False)
            return CommandResult(argv, 0, json.dumps(self.installed), "")
        return CommandResult(argv, 2, "", f"unexpected argv: {argv}")
