"""Проверка подтверждённых путей для PostToolUse-хука solve-task brief."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time

WARNING = "⚠️ [файл не в результатах поиска]"
SKILL_MARKER = "skills/solve-task"
BASE_DIR_MARKER = "Base directory for this skill:"
SOLVE_ATTRIBUTION = "rag-reviewer:solve-task"
CHECKED_SECTIONS = ("## Relevant code", "## Test exemplars")
MCP_TOOL_PREFIXES = (
    "mcp__reviewer__",
    "mcp__plugin_rag-reviewer_reviewer__",
)
ALLOWED_LOGICAL_TOOLS = frozenset({
    "search_codebase",
    "related_symbols",
    "callers",
    "implementations",
    "definition",
})
ALLOWED_TOOL_NAMES = frozenset(
    prefix + logical
    for prefix in MCP_TOOL_PREFIXES
    for logical in ALLOWED_LOGICAL_TOOLS
)

_HEADER_RE = re.compile(
    r"^\s*(?://\s*)?\S+#\S+\s+\((?P<path>[^()\r\n]+?):\d+(?:-\d+)?\)",
    re.MULTILINE,
)
_CITATION_RE = re.compile(
    r"([A-Za-z0-9_./\\+\-]+\.[A-Za-z0-9]{1,6}):\d+(?:-\d+)?"
)
_INCOMPLETE_RE = re.compile(r"\[(?:\.\.\.|…)?truncated\]")


def _block_text(content: object) -> str:
    """Вернуть текст строкового content или его вложенных text-блоков."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def _message_text(line: dict) -> str:
    """Вернуть текст строкового message.content или его text-блоков."""
    message = line.get("message")
    if not isinstance(message, dict):
        return ""
    return _block_text(message.get("content"))


def find_window_start(lines: list[dict]) -> int:
    """Найти последнее user-сообщение с обоими маркерами solve-task."""
    start = -1
    for index, line in enumerate(lines):
        if line.get("type") != "user":
            continue
        text = _message_text(line)
        if BASE_DIR_MARKER in text and SKILL_MARKER in text:
            start = index
    return start


def tool_result_texts(lines: list[dict], start_idx: int) -> list[str]:
    """Собрать связанные результаты разрешённых retrieval-вызовов."""
    texts: list[str] = []
    allowed_ids: set[str] = set()
    for line in lines[start_idx + 1:]:
        content = (line.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if line.get("type") == "assistant" and block.get("type") == "tool_use":
                tool_id = block.get("id")
                if (
                    isinstance(tool_id, str)
                    and block.get("name") in ALLOWED_TOOL_NAMES
                ):
                    allowed_ids.add(tool_id)
                continue
            if (
                line.get("type") != "user"
                or block.get("type") != "tool_result"
                or block.get("tool_use_id") not in allowed_ids
            ):
                continue
            text = _block_text(block.get("content"))
            if text:
                try:
                    envelope = json.loads(text)
                except ValueError:
                    envelope = None
                if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
                    text = envelope["result"]
                texts.append(text)
    return texts


def normalize_path(path: str) -> str:
    """Нормализовать разделители без разрешения пути через файловую систему."""
    path = re.sub(r"/+", "/", path.replace("\\", "/"))
    while path.startswith("./"):
        path = path[2:]
    return path


def evidence_paths(texts: list[str]) -> set[str]:
    """Извлечь пути только из заголовков результатов поиска кода."""
    return {
        normalize_path(match.group("path"))
        for text in texts
        for match in _HEADER_RE.finditer(text)
    }


def _result_is_incomplete(text: str) -> bool:
    """Проверить наличие точного sentinel жёсткого усечения результата."""
    return bool(_INCOMPLETE_RE.search(text))


def path_is_observed(cited: str, observed: set[str]) -> bool:
    """Проверить exact или suffix repo-relative пути во внешнем absolute path."""
    cited = normalize_path(cited)
    for candidate in observed:
        candidate = normalize_path(candidate)
        if candidate == cited:
            return True
        is_absolute = candidate.startswith("/") or bool(
            re.match(r"^[A-Za-z]:/", candidate)
        )
        if is_absolute and "/" in cited and candidate.endswith("/" + cited):
            return True
    return False


def guard_brief(text: str, observed: set[str]) -> tuple[str, list[str], list[str]]:
    """Пометить неподтверждённые пути в проверяемых секциях brief."""
    cited: list[str] = []
    missing: list[str] = []
    guarded_lines: list[str] = []
    active = False

    for line in text.splitlines(keepends=True):
        heading = line.rstrip("\r\n")
        if heading.startswith("## "):
            active = heading in CHECKED_SECTIONS
            guarded_lines.append(line)
            continue
        if not active:
            guarded_lines.append(line)
            continue

        def annotate(match: re.Match[str]) -> str:
            path = normalize_path(match.group(1))
            cited.append(path)
            if path_is_observed(path, observed):
                return match.group(0)
            missing.append(path)
            following = line[match.end():]
            if re.match(r"[ \t]*" + re.escape(WARNING), following):
                return match.group(0)
            return f"{match.group(0)} {WARNING}"

        guarded_lines.append(_CITATION_RE.sub(annotate, line))

    return "".join(guarded_lines), cited, missing


def _under_briefs(path: str) -> bool:
    norm = os.path.normpath(path).replace(os.sep, "/")
    return "/docs/superpowers/briefs/" in norm or norm.startswith(
        "docs/superpowers/briefs/"
    )


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _read_jsonl(path: str) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _find_tool_use(
    lines: list[dict],
    tool_use_id: str,
) -> tuple[dict, dict] | None:
    """Найти assistant-строку и tool_use с точным id."""
    for line in lines:
        if line.get("type") != "assistant":
            continue
        content = (line.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id") == tool_use_id
            ):
                return line, block
    return None


def _read_fresh_transcript(
    path: str,
    tool_use_id: object,
) -> tuple[list[dict], tuple[dict, dict] | None]:
    """Дождаться появления текущего tool_use в transcript."""
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return [], None
    for attempt in range(3):
        lines = _read_jsonl(path)
        current = _find_tool_use(lines, tool_use_id)
        if current is not None:
            return lines, current
        if attempt < 2:
            time.sleep(0.05)
    return [], None


def _markerless_write_is_trusted(
    payload: dict,
    row: dict,
    block: dict,
) -> bool:
    """Проверить provenance текущего Path A Write без skill marker."""
    agent_id = payload.get("agent_id")
    tool_input = payload.get("tool_input")
    block_input = block.get("input")
    return bool(
        agent_id
        and row.get("type") == "assistant"
        and row.get("agentId") == agent_id
        and row.get("attributionSkill") == SOLVE_ATTRIBUTION
        and block.get("type") == "tool_use"
        and block.get("id") == payload.get("tool_use_id")
        and block.get("name") == payload.get("tool_name") == "Write"
        and isinstance(tool_input, dict)
        and isinstance(block_input, dict)
        and block_input.get("file_path") == tool_input.get("file_path")
    )


def _write_text(path: str, text: str) -> None:
    directory = os.path.dirname(path) or "."
    fd, temp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _debug(label: str, paths: list[str] | set[str]) -> None:
    if os.environ.get("BRIEF_GUARD_DEBUG"):
        rendered = ", ".join(sorted(set(paths)))
        sys.stderr.write(f"brief_guard {label}: {rendered}\n")


def run(payload: dict) -> int:
    """Оркестрация fail-open хука проверки brief."""
    try:
        file_path = (payload.get("tool_input") or {}).get("file_path") or ""
        if not _under_briefs(file_path):
            return 0

        lines, current = _read_fresh_transcript(
            payload.get("transcript_path") or "",
            payload.get("tool_use_id"),
        )
        if not lines or current is None:
            return 0
        start = find_window_start(lines)
        if start < 0:
            current_row, current_block = current
            if not _markerless_write_is_trusted(
                payload,
                current_row,
                current_block,
            ):
                return 0

        result_texts = tool_result_texts(lines, start)
        if any(_result_is_incomplete(text) for text in result_texts):
            return 0
        observed = evidence_paths(result_texts)
        if not observed:
            return 0

        brief = _read_text(file_path)
        if brief is None:
            return 0
        guarded, cited, missing = guard_brief(brief, observed)
        _debug("observed", observed)
        _debug("cited", cited)
        _debug("missing", missing)
        if guarded != brief:
            _write_text(file_path, guarded)
    except Exception:
        return 0
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    return run(payload)


if __name__ == "__main__":
    sys.exit(main())
