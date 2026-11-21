"""Record reported Codex model request usage in an explicitly selected local file."""

from __future__ import annotations

import json
import os
import sys

_BUCKETS = (
    "input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "reasoning_output_tokens",
)
_LABELS = ("session_id", "turn_id", "model", "status")


def main() -> int:
    destination = os.environ.get("REVIEWER_CODEX_USAGE_FILE")
    if not destination:
        return 0
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("hook_event_name") != "PostModelRequest":
            return 0
        usage = payload.get("token_usage")
        if not isinstance(usage, dict):
            return 0
        tokens = {
            field: usage[field] for field in _BUCKETS
            if type(usage.get(field)) is int and usage[field] >= 0
        }
        if not {"input_tokens", "output_tokens"}.issubset(tokens):
            return 0
        record = {field: payload[field] for field in _LABELS
                  if isinstance(payload.get(field), str)}
        record.update(hook_event_name="PostModelRequest", token_usage=tokens)
        with open(destination, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except (OSError, ValueError):
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
