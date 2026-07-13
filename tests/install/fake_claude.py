import json
from pathlib import Path

from reviewer.install_codex import CommandResult


class FakeClaude:
    """File-backed Claude fake with the public plugin JSON list shapes."""

    def __init__(self, executable: Path, config_dir: Path):
        self.executable = executable
        self.config_dir = config_dir
        self.calls: list[tuple[str, ...]] = []
        self.fail: tuple[str, ...] | None = None
        self.marketplace_after_add: dict[str, object] | None = None
        self.plugin_after_install: dict[str, object] | None = None

    @property
    def config_path(self) -> Path:
        return self.config_dir / "fake-claude-state.json"

    def _state(self) -> dict[str, object]:
        if not self.config_path.exists():
            return {"marketplace": None, "plugin": None}
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("fake Claude state must be an object")
        return data

    def _write_state(self, **updates: object) -> None:
        state = {**self._state(), **updates}
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(state, sort_keys=True), encoding="utf-8"
        )

    @property
    def marketplace(self) -> dict[str, object] | None:
        value = self._state().get("marketplace")
        if value is not None and not isinstance(value, dict):
            raise TypeError("fake Claude marketplace must be an object")
        return value

    @marketplace.setter
    def marketplace(self, value: dict[str, object] | None) -> None:
        self._write_state(marketplace=value)

    @property
    def plugin(self) -> dict[str, object] | None:
        value = self._state().get("plugin")
        if value is not None and not isinstance(value, dict):
            raise TypeError("fake Claude plugin must be an object")
        return value

    @plugin.setter
    def plugin(self, value: dict[str, object] | None) -> None:
        self._write_state(plugin=value)

    def __call__(self, argv: tuple[str, ...]) -> CommandResult:
        self.calls.append(argv)
        tail = argv[1:]
        if tail == ("plugin", "marketplace", "add", "--help"):
            return CommandResult(argv, 0, "--scope --sparse", "")
        if tail == ("plugin", "marketplace", "list", "--help"):
            return CommandResult(argv, 0, "--json", "")
        if tail == ("plugin", "install", "--help"):
            return CommandResult(argv, 0, "--scope", "")
        if tail == ("plugin", "list", "--help"):
            return CommandResult(argv, 0, "--json", "")
        if self.fail is not None and tail[: len(self.fail)] == self.fail:
            return CommandResult(argv, 1, "", "injected failure")
        if tail == ("plugin", "marketplace", "list", "--json"):
            rows = [self.marketplace] if self.marketplace is not None else []
            return CommandResult(argv, 0, json.dumps(rows), "")
        if tail == ("plugin", "list", "--json"):
            rows = [self.plugin] if self.plugin is not None else []
            return CommandResult(argv, 0, json.dumps(rows), "")
        if tail[:3] == ("plugin", "marketplace", "add"):
            source = tail[3]
            self.marketplace = self.marketplace_after_add or {
                "name": "rag-reviewer-marketplace",
                "source": "git",
                "url": source,
            }
            return CommandResult(argv, 0, "Marketplace added", "")
        if tail[:2] == ("plugin", "install"):
            plugin_id = tail[2]
            self.plugin = self.plugin_after_install or {
                "id": plugin_id,
                "scope": "user",
                "enabled": True,
            }
            return CommandResult(argv, 0, "Plugin installed", "")
        return CommandResult(argv, 2, "", f"unexpected argv: {argv}")
