"""Normalize LLM usage records without depending on a client's hook lifecycle."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from reviewer.metrics.brief_quality.briefs import BUCKET_KEYS

SOURCES = ("codex-exec", "codex-app-server", "claude-code", "normalized")

_CODEX_FIELDS = {
    "codex-exec": ("input_tokens", "output_tokens", "cached_input_tokens",
                   "cache_write_input_tokens"),
    "codex-app-server": ("inputTokens", "outputTokens", "cachedInputTokens",
                         "cacheWriteInputTokens"),
}


def _tokens(usage: Mapping, field: str, *, optional: bool = False) -> int:
    value = usage.get(field, 0) if optional else usage.get(field)
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _codex_usage(usage: Mapping, source: str) -> dict[str, int]:
    input_field, output_field, read_field, write_field = _CODEX_FIELDS[source]
    cache_read = _tokens(usage, read_field, optional=True)
    cache_write = _tokens(usage, write_field, optional=True)
    fresh = _tokens(usage, input_field) - cache_read - cache_write
    if fresh < 0:
        raise ValueError(f"cached input exceeds {input_field}")
    # Reasoning tokens are already included in the output.
    return {
        "fresh_in": fresh,
        "output": _tokens(usage, output_field),
        "cache_write": cache_write,
        "cache_read": cache_read,
    }


def _app_server_usage(event: Mapping, previous: dict[str, dict[str, int]]) -> dict | None:
    if event.get("method") != "thread/tokenUsage/updated":
        return None
    params = event.get("params")
    if not isinstance(params, Mapping):
        raise ValueError("usage notification is missing params")
    thread = params.get("threadId")
    if not isinstance(thread, str) or not thread:
        raise ValueError("usage notification is missing threadId")
    usage = params.get("tokenUsage")
    if not isinstance(usage, Mapping) or not isinstance(usage.get("total"), Mapping):
        raise ValueError("usage notification is missing cumulative usage")
    total = _codex_usage(usage["total"], "codex-app-server")
    prior = previous.get(thread)
    if prior is None:
        last = usage.get("last")
        if not isinstance(last, Mapping):
            raise ValueError("first usage notification is missing its last request")
        delta = _codex_usage(last, "codex-app-server")
        if any(delta[key] > total[key] for key in BUCKET_KEYS):
            raise ValueError("last request exceeds cumulative usage")
    else:
        delta = {key: total[key] - prior[key] for key in BUCKET_KEYS}
        if any(value < 0 for value in delta.values()):
            raise ValueError("cumulative usage decreased for thread")
    previous[thread] = total
    return delta if any(delta.values()) else None


def _usage(event: Mapping, source: str) -> tuple[str | None, str | None, dict] | None:
    kind = event.get("type")
    if source == "codex-exec" and kind == "turn.completed":
        usage = event.get("usage")
        if not isinstance(usage, Mapping):
            raise ValueError("completed turn is missing usage")
        return event.get("model"), None, _codex_usage(usage, source)
    if source == "claude-code" and kind == "assistant":
        message = event.get("message")
        if not isinstance(message, Mapping) or message.get("usage") is None:
            return None
        usage = message["usage"]
        if not isinstance(usage, Mapping):
            raise ValueError("assistant usage must be an object")
        return message.get("model"), message.get("id"), {
            "fresh_in": _tokens(usage, "input_tokens"),
            "output": _tokens(usage, "output_tokens"),
            "cache_write": _tokens(usage, "cache_creation_input_tokens", optional=True),
            "cache_read": _tokens(usage, "cache_read_input_tokens", optional=True),
        }
    if source == "normalized" and kind == "llm_usage":
        usage = event.get("usage")
        if not isinstance(usage, Mapping):
            raise ValueError("normalized record is missing usage")
        return event.get("model"), None, {key: _tokens(usage, key) for key in BUCKET_KEYS}
    return None


def measure_usage(lines: Iterable[str], source: str, *, model: str | None = None) -> dict:
    """Aggregate JSONL events to the same four buckets used by reviewer metrics."""
    if source not in SOURCES:
        raise ValueError(f"Unknown usage source: {source}")
    records = {}
    thread_totals: dict[str, dict[str, int]] = {}
    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("expected a JSON object")
            if source == "codex-app-server":
                delta = _app_server_usage(event, thread_totals)
                normalized = (None, None, delta) if delta is not None else None
            else:
                normalized = _usage(event, source)
        except ValueError as error:
            raise ValueError(f"line {lineno}: {error}") from error
        if normalized is None:
            continue
        name, message_id, usage = normalized
        name = name if isinstance(name, str) and name else (model or "unknown")
        # Claude can repeat an assistant message with a cumulative usage snapshot.
        key = message_id if isinstance(message_id, str) and message_id else lineno
        records[key] = (name, usage)

    total = dict.fromkeys(BUCKET_KEYS, 0)
    by_model: dict[str, dict[str, int]] = {}
    for name, usage in records.values():
        buckets = by_model.setdefault(name, dict.fromkeys(BUCKET_KEYS, 0))
        for kind, tokens in usage.items():
            total[kind] += tokens
            buckets[kind] += tokens
    return {"source": source, "records": len(records), "usage": total, "by_model": by_model}
