"""Последовательно запускает PostToolUse-хуки записи brief."""

import json
import sys

import brief_cost
import brief_guard


def run(payload: dict) -> int:
    """Запустить cost и guard по порядку, независимо и fail-open."""
    try:
        brief_cost.run(payload)
    except Exception:
        pass
    try:
        brief_guard.run(payload)
    except Exception:
        pass
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    return run(payload)


if __name__ == "__main__":
    sys.exit(main())
