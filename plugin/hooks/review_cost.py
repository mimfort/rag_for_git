"""PreToolUse-хук: снимает расход окна ревью в sidecar-файл для publish_review.

Событие именно PreToolUse: PostToolUse опаздывает — к его срабатыванию
publish_review уже записал историю, и метаданные некуда положить.

Запускается системным python3 (только stdlib). Любая ошибка → no-op (exit 0):
сбой хука не имеет права ломать публикацию ревью.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _transcript import (  # noqa: E402
        aggregate_usage, empty_bucket, find_window_start, message_text, read_jsonl,
        resolve_transcript, weigh,
    )
except ImportError:
    # fail-open: недоступный/битый общий модуль не должен ронять хук с traceback
    # и ненулевым exit code — весь функционал ниже становится тихим no-op'ом.
    aggregate_usage = None
    empty_bucket = None
    find_window_start = None
    message_text = None
    read_jsonl = None
    resolve_transcript = None
    weigh = None

SIDECAR_DIR = "reviewer-review-cost"
SIDECAR_VERSION = 1
SKILL_MARKER = "skills/review-pr"

# Маркер в первом промпте sidechain-цепочки → стадия. Маркеры детерминированы:
# скилл review-pr передаёт имена reference-файлов в промпт субагента дословно.
# Guard-тест сверяет словарь с содержимым plugin/skills/review-pr/references/.
STAGE_MARKERS = {
    "references/analyze-prompt.md": "analyze",
    "references/risk-changes-prompt.md": "risk",
    "references/blast-radius-prompt.md": "blast_radius",
    "references/requirements-prompt.md": "requirements",
    "references/verify-prompt.md": "verify",
    "rag-reviewer:performance-review": "performance",
    "rag-reviewer:maintainability-review": "maintainability",
}


def sidecar_path(repo: str, pr: int) -> str:
    """Детерминированный путь sidecar от repo и номера PR."""
    safe = str(repo).replace("/", "_").replace("..", "_")
    return os.path.join(tempfile.gettempdir(), SIDECAR_DIR, f"{safe}-{pr}.json")


def _tool_uses(line: dict) -> list:
    content = (line.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def find_prepare_index(lines: list, repo: str, pr: int) -> int:
    """Индекс последнего хода с вызовом prepare_review этого же (repo, pr); -1 если нет."""
    found = -1
    for i, line in enumerate(lines):
        if line.get("type") != "assistant":
            continue
        for block in _tool_uses(line):
            if not str(block.get("name") or "").endswith("prepare_review"):
                continue
            args = block.get("input") or {}
            if str(args.get("repo") or "") == repo and int(args.get("pr") or -1) == int(pr):
                found = i
    return found


def _stage_of(text: str) -> str:
    for marker, stage in STAGE_MARKERS.items():
        if marker in text:
            return stage
    return "subagent"


def aggregate_by_stage(lines: list, start_idx: int) -> dict:
    """Бакеты токенов по стадиям окна.

    Ходы главного агента → 'orchestrator'. Sidechain-ходы наследуют стадию
    последнего sidechain-промпта: он и несёт маркер reference-файла.
    """
    by_stage: dict = {}
    current = "subagent"
    for line in lines[start_idx + 1:]:
        if line.get("isSidechain") and line.get("type") == "user":
            current = _stage_of(message_text(line))
            continue
        if line.get("type") != "assistant":
            continue
        stage = current if line.get("isSidechain") else "orchestrator"
        usage = (line.get("message") or {}).get("usage") or {}
        bucket = by_stage.setdefault(stage, empty_bucket())
        bucket["fresh_in"] += int(usage.get("input_tokens") or 0)
        bucket["output"] += int(usage.get("output_tokens") or 0)
        bucket["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
        bucket["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
    return {s: b for s, b in by_stage.items() if any(b.values())}


def _dominant_model(lines: list, start_idx: int) -> str | None:
    """Модель с наибольшим числом ходов главного агента в окне."""
    counts: dict = {}
    for line in lines[start_idx + 1:]:
        if line.get("type") != "assistant" or line.get("isSidechain"):
            continue
        model = (line.get("message") or {}).get("model")
        if model:
            counts[model] = counts.get(model, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _write_json(path: str, data: dict) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run(payload: dict) -> int:
    """Оркестрация хука. Всегда возвращает 0 (fail-open)."""
    try:
        if read_jsonl is None:
            # _transcript недоступен/битый (см. импорт выше) — тихий no-op.
            return 0
        tool_input = payload.get("tool_input") or {}
        repo = tool_input.get("repo")
        pr = tool_input.get("pr")
        if not repo or pr is None:
            return 0
        pr = int(pr)
        path, source = resolve_transcript(payload)
        if not path:
            return 0
        lines = read_jsonl(path)
        if not lines:
            return 0
        start = find_prepare_index(lines, str(repo), pr)
        if start < 0:
            start = find_window_start(lines, SKILL_MARKER)
        if start < 0:
            return 0
        by_model, sidechain = aggregate_usage(lines, start)
        if not by_model and not sidechain:
            return 0
        by_stage = aggregate_by_stage(lines, start)
        total = sum(weigh(b) for b in by_model.values())
        total += sum(weigh(b) for b in sidechain.values())
        _write_json(sidecar_path(str(repo), pr), {
            "version": SIDECAR_VERSION,
            "repo": str(repo),
            "pr": pr,
            "model": _dominant_model(lines, start),
            "transcript_source": source,
            "usage": {"by_model": by_model, "sidechain": sidechain, "by_stage": by_stage},
            "total_cost": round(total, 6),
            "written_at": datetime.now(timezone.utc).isoformat(),
        })
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
