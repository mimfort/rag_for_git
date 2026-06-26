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
