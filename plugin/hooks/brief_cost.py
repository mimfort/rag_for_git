"""PostToolUse-хук: дописывает в бриф solve-task расход LLM-токенов на этап.

Запускается системным python3 (только stdlib). Считает детерминированно по
транскрипту сессии; долларов не показывает. Любая ошибка → no-op (exit 0).
"""
from __future__ import annotations

HEADER = "## Токены (этап solve-task)"


def human_tokens(n: int) -> str:
    """Человекочитаемое число токенов: 9900→'9.9K', 164000→'164K', 14.2e6→'14.2M'."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        value, unit = n / 1000, "K"
    else:
        value, unit = n / 1_000_000, "M"
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{text}{unit}"


def render_block(by_model: dict) -> str:
    """Текст блока «## Токены (этап solve-task)» (без хвостового перевода строки)."""
    lines = [HEADER]
    total = 0
    for model, b in by_model.items():
        lines.append(f"Модель: {model}")
        lines.append(
            f"fresh-in {human_tokens(b['fresh_in'])} · "
            f"out {human_tokens(b['output'])} · "
            f"cache-write {human_tokens(b['cache_write'])} · "
            f"cache-read {human_tokens(b['cache_read'])}"
        )
        total += b["fresh_in"] + b["output"] + b["cache_write"] + b["cache_read"]
    lines.append(f"Всего: {human_tokens(total)} токенов")
    return "\n".join(lines)


def upsert_block(text: str, block: str) -> str:
    """Заменить существующий блок по HEADER либо дописать в конец. Идемпотентно."""
    block = block.rstrip("\n")
    if HEADER not in text:
        body = text.rstrip("\n")
        return f"{body}\n\n{block}\n"
    lines = text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].strip() == HEADER:
            i += 1
            while i < n and not lines[i].startswith("## "):
                i += 1
            out.extend(block.splitlines())
            if i < n:
                out.append("")
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out).rstrip("\n") + "\n"


SKILL_MARKER = "skills/solve-task"
BASE_DIR_MARKER = "Base directory for this skill:"


def _message_text(line: dict) -> str:
    """Текст сообщения: message.content как строка или список блоков {text}."""
    content = (line.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b["text"] for b in content
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        ]
        return "\n".join(parts)
    return ""


def find_window_start(lines: list) -> int:
    """Индекс последнего user-сообщения с маркерами solve-task; -1 если нет."""
    start = -1
    for i, line in enumerate(lines):
        if line.get("type") != "user":
            continue
        text = _message_text(line)
        if BASE_DIR_MARKER in text and SKILL_MARKER in text:
            start = i
    return start


def aggregate_usage(lines: list, start_idx: int) -> dict:
    """Сумма 4 бакетов токенов по model для assistant-ходов после start_idx."""
    by_model: dict = {}
    for line in lines[start_idx + 1:]:
        if line.get("type") != "assistant" or line.get("isSidechain"):
            continue
        message = line.get("message") or {}
        usage = message.get("usage") or {}
        model = message.get("model") or "unknown"
        bucket = by_model.setdefault(
            model, {"fresh_in": 0, "output": 0, "cache_write": 0, "cache_read": 0})
        bucket["fresh_in"] += int(usage.get("input_tokens") or 0)
        bucket["output"] += int(usage.get("output_tokens") or 0)
        bucket["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
        bucket["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
    return {m: b for m, b in by_model.items() if any(b.values())}
