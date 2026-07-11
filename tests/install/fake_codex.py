import json
from pathlib import Path

from reviewer.install_codex import CommandResult


class FakeCodex:
    def __init__(self, executable: Path, marketplace_root: Path):
        self.executable = executable
        self.marketplace_root = marketplace_root
        self.marketplace = False
        self.installed: dict | None = None
        self.calls: list[tuple[str, ...]] = []
        self.fail: tuple[str, ...] | None = None

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
            rows = (
                [
                    {
                        "name": "rag-reviewer",
                        "root": str(self.marketplace_root),
                        "marketplaceSource": {
                            "sourceType": "git",
                            "source": "mimfort/rag_for_git",
                            "ref": "main",
                            "sparsePaths": [".agents/plugins", "plugin"],
                        },
                    }
                ]
                if self.marketplace
                else []
            )
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
            return CommandResult(argv, 0, json.dumps({"name": "rag-reviewer"}), "")
        if tail[:2] == ("plugin", "add"):
            manifest = json.loads(
                (self.marketplace_root / "plugin/.codex-plugin/plugin.json").read_text()
            )
            self.installed = {
                "name": "rag-reviewer",
                "marketplaceName": "rag-reviewer",
                "version": manifest["version"],
                "installed": True,
                "enabled": True,
            }
            return CommandResult(argv, 0, json.dumps(self.installed), "")
        return CommandResult(argv, 2, "", f"unexpected argv: {argv}")
