from __future__ import annotations

import argparse
from pathlib import Path

from reviewer.install_codex import sync_plugin_metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    errors = sync_plugin_metadata(root, check=args.check)
    if errors:
        for error in errors:
            print(error)
        if args.check:
            # Манифесты плагина — сгенерированный артефакт, но лежат в git: их
            # читает marketplace прямо из репозитория. Значит версию pyproject
            # они не наследуют автоматически, и после бампа их надо пересобрать.
            print(
                "\nМанифесты плагина рассинхронизированы с pyproject. Почини так:\n"
                "  python scripts/update_codex_plugin_manifest.py\n"
                "  git add .codex-plugin plugin/.codex-plugin plugin/.claude-plugin "
                "plugin/assets\n"
                "и закоммить результат."
            )
        return 1
    if not args.check:
        print("Codex plugin manifests synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
