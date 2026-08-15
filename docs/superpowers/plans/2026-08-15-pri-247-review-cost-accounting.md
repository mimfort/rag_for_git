# PRI-247 — учёт расхода ревью (usage / total_cost / review_steps): план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** довести до конца учёт расхода ревью — расход снимает клиентский хук в sidecar-файл, `publish_review` его читает, а стадийный трейс сервер пишет сам, независимо от хуков.

**Architecture:** два независимых канала. Канал расхода: `PreToolUse`-хук на `publish_review` агрегирует транскрипт сессии (stdlib-only, системный python3) и кладёт JSON в tempdir по детерминированному пути от `repo`+`pr`; сервер читает его перед сборкой `_RunMetadata`, явные аргументы клиента приоритетнее. Канал трейса: `MCPReviewService._invoke_tool` пишет шаг на каждый тул PR-сессии со стадией и размером payload — работает в любом CLI без хуков.

**Tech Stack:** Python 3 stdlib (хуки), Python + psycopg (сервер), pytest, FastAPI, React + TypeScript.

**Spec:** `docs/superpowers/specs/2026-08-15-pri-247-review-cost-accounting-design.md`

**Ветка:** `feat/pri-247-review-cost-accounting` (уже создана; коммитить в неё).

## Global Constraints

- Код в `plugin/hooks/` исполняется **системным python3**: только stdlib, никаких импортов из `reviewer/`, никаких сторонних пакетов.
- Любой хук завершается кодом `0` при любой ошибке (fail-open); ничего не пишет в stdout/stderr вне отладочного режима.
- `publish_review` обязан работать без sidecar: отсутствие/битый файл → поля расхода пусты, ревью публикуется.
- Веса взвешивания: `fresh_in×1`, `output×5`, `cache_write×1.25`, `cache_read×0.1`. Единица — input-token equivalent, **условные единицы, не доллары**.
- Миграций БД в задаче нет: новые данные кладутся в существующие колонки (`tool_calls` JSONB, `usage` JSONB, `total_cost`).
- Язык кода проекта — русский: комментарии, докстринги, сообщения.
- Unit-тесты без Postgres/Neo4j/сети; всё остальное — `@pytest.mark.integration`.
- Любая правка содержимого `plugin/` требует прогона `scripts/update_codex_plugin_manifest.py` (Task 5), иначе install-тесты краснеют.
- Коммиты — Conventional Commits на русском, без self-attribution.

---

### Task 1: Общий stdlib-модуль агрегации транскрипта

Вынести агрегацию из `brief_cost.py` в переиспользуемый модуль, не изменив поведение solve-task ни на символ.

**Files:**
- Create: `plugin/hooks/_transcript.py`
- Modify: `plugin/hooks/brief_cost.py` (удалить `_read_jsonl`, `aggregate_usage`, `find_window_start`; импортировать из модуля)
- Test: `tests/hooks/test_transcript.py` (новый)
- Не трогать: `tests/hooks/test_brief_cost.py` — его зелёный прогон без правок и есть доказательство неизменности поведения

**Interfaces:**
- Produces:
  - `read_jsonl(path: str) -> list[dict]`
  - `aggregate_usage(lines: list, start_idx: int) -> tuple[dict, dict]` — `(main_by_model, sidechain_by_model)`, бакеты `{"fresh_in","output","cache_write","cache_read"}`
  - `find_window_start(lines: list, skill_marker: str) -> int`
  - `resolve_transcript(payload: dict) -> tuple[str, str] | tuple[None, None]` — `(путь, источник)`, источник `"payload"` | `"session_id"`
  - `weigh(bucket: dict) -> float`
- Consumes: ничего.

- [ ] **Step 1: Написать падающие тесты нового модуля**

```python
# tests/hooks/test_transcript.py
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugin" / "hooks"))

import _transcript  # noqa: E402


def _assistant(model, usage, sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain,
            "message": {"model": model, "usage": usage}}


def test_weigh_uses_bucket_weights_not_plain_sum():
    bucket = {"fresh_in": 100, "output": 100, "cache_write": 100, "cache_read": 100}
    # 100*1 + 100*5 + 100*1.25 + 100*0.1 = 735.0, а простая сумма дала бы 400
    assert _transcript.weigh(bucket) == 735.0


def test_find_window_start_is_parameterised_by_marker():
    lines = [
        {"type": "user", "message": {"content": "Base directory for this skill: x/skills/solve-task"}},
        {"type": "user", "message": {"content": "Base directory for this skill: x/skills/review-pr"}},
    ]
    assert _transcript.find_window_start(lines, "skills/review-pr") == 1
    assert _transcript.find_window_start(lines, "skills/solve-task") == 0
    assert _transcript.find_window_start(lines, "skills/nope") == -1


def test_aggregate_usage_splits_main_and_sidechain():
    lines = [
        {"type": "user", "message": {"content": "start"}},
        _assistant("opus", {"input_tokens": 10, "output_tokens": 2}),
        _assistant("sonnet", {"input_tokens": 5, "cache_read_input_tokens": 7}, sidechain=True),
    ]
    main, side = _transcript.aggregate_usage(lines, 0)
    assert main["opus"]["fresh_in"] == 10
    assert main["opus"]["output"] == 2
    assert side["sonnet"]["cache_read"] == 7
    assert "sonnet" not in main


def test_resolve_transcript_prefers_payload_path(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    assert _transcript.resolve_transcript({"transcript_path": str(path)}) == (str(path), "payload")


def test_resolve_transcript_falls_back_to_session_id(tmp_path, monkeypatch):
    projects = tmp_path / ".claude" / "projects" / "slug"
    projects.mkdir(parents=True)
    transcript = projects / "sess-1.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    found, source = _transcript.resolve_transcript({"session_id": "sess-1"})
    assert found == str(transcript)
    assert source == "session_id"


def test_resolve_transcript_returns_none_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _transcript.resolve_transcript({}) == (None, None)


def test_read_jsonl_skips_broken_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"a": 1}\nnot json\n\n{"b": 2}\n', encoding="utf-8")
    assert _transcript.read_jsonl(str(path)) == [{"a": 1}, {"b": 2}]
```

- [ ] **Step 2: Прогнать тесты и убедиться, что падают**

Run: `.venv/bin/pytest tests/hooks/test_transcript.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named '_transcript'`

- [ ] **Step 3: Написать модуль**

```python
# plugin/hooks/_transcript.py
"""Общая агрегация транскрипта сессии для хуков плагина.

Исполняется системным python3 (только stdlib): импорт из пакета ``reviewer``
здесь запрещён — пакет в этом интерпретаторе не установлен.
"""
from __future__ import annotations

import glob
import json
import os

BASE_DIR_MARKER = "Base directory for this skill:"

# Веса бакетов относительно input-токена (спайк PRI-246). Единица результата —
# input-token equivalent: условные единицы, НЕ доллары.
WEIGHTS = {"fresh_in": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.1}


def empty_bucket() -> dict:
    return {"fresh_in": 0, "output": 0, "cache_write": 0, "cache_read": 0}


def weigh(bucket: dict) -> float:
    """Взвешенная стоимость бакета в условных единицах."""
    return round(sum(WEIGHTS[k] * int(bucket.get(k) or 0) for k in WEIGHTS), 6)


def read_jsonl(path) -> list:
    """Прочитать JSONL; битые строки пропускаются, ошибка чтения → []."""
    rows: list = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


def message_text(line: dict) -> str:
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


def find_window_start(lines: list, skill_marker: str) -> int:
    """Индекс последнего user-сообщения с маркерами скилла; -1 если нет."""
    start = -1
    for i, line in enumerate(lines):
        if line.get("type") != "user":
            continue
        text = message_text(line)
        if BASE_DIR_MARKER in text and skill_marker in text:
            start = i
    return start


def aggregate_usage(lines: list, start_idx: int) -> tuple:
    """Сумма 4 бакетов токенов по model для assistant-ходов после start_idx.

    Returns:
        (main_by_model, sidechain_by_model); sidechain — ходы с isSidechain=True.
    """
    def _add(bucket: dict, usage: dict) -> None:
        bucket["fresh_in"] += int(usage.get("input_tokens") or 0)
        bucket["output"] += int(usage.get("output_tokens") or 0)
        bucket["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
        bucket["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)

    by_model: dict = {}
    sidechain: dict = {}
    for line in lines[start_idx + 1:]:
        if line.get("type") != "assistant":
            continue
        message = line.get("message") or {}
        usage = message.get("usage") or {}
        model = message.get("model") or "unknown"
        target = sidechain if line.get("isSidechain") else by_model
        _add(target.setdefault(model, empty_bucket()), usage)
    return (
        {m: b for m, b in by_model.items() if any(b.values())},
        {m: b for m, b in sidechain.items() if any(b.values())},
    )


def resolve_transcript(payload: dict) -> tuple:
    """Путь к транскрипту и способ его получения.

    Сначала payload['transcript_path'] (общее поле хуков), затем реконструкция
    по session_id в ~/.claude/projects/<slug>/<session_id>.jsonl. Второй путь
    оставлен намеренно: он снимает зависимость от того, отдаёт ли конкретное
    событие хука transcript_path.

    Returns:
        (путь, "payload"|"session_id") либо (None, None).
    """
    direct = payload.get("transcript_path")
    if direct and os.path.isfile(direct):
        return (direct, "payload")
    session_id = payload.get("session_id")
    if session_id:
        pattern = os.path.join(
            os.path.expanduser("~"), ".claude", "projects", "*", f"{session_id}.jsonl")
        matches = sorted(glob.glob(pattern))
        if matches:
            return (matches[0], "session_id")
    return (None, None)
```

- [ ] **Step 4: Прогнать тесты нового модуля**

Run: `.venv/bin/pytest tests/hooks/test_transcript.py -q`
Expected: PASS (7 тестов)

- [ ] **Step 5: Перевести brief_cost.py на общий модуль**

В `plugin/hooks/brief_cost.py` удалить функции `_read_jsonl`, `aggregate_usage`, `find_window_start`, `_message_text` и константу `BASE_DIR_MARKER`; добавить импорт и тонкие обёртки, сохраняющие имена, на которые смотрят существующие тесты:

```python
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _transcript import (  # noqa: E402
    BASE_DIR_MARKER, aggregate_usage, message_text as _message_text, read_jsonl as _read_jsonl,
)
from _transcript import find_window_start as _find_window_start  # noqa: E402

SKILL_MARKER = "skills/solve-task"


def find_window_start(lines: list) -> int:
    """Индекс последнего user-сообщения с маркерами solve-task; -1 если нет."""
    return _find_window_start(lines, SKILL_MARKER)
```

- [ ] **Step 6: Убедиться, что поведение solve-task не изменилось**

Run: `.venv/bin/pytest tests/hooks/ -q`
Expected: PASS — все существующие тесты `test_brief_cost.py` зелёные **без единой правки в них**. Если пришлось править тест — вынос сломал поведение, откатить и переделать.

- [ ] **Step 7: Коммит**

```bash
git add plugin/hooks/_transcript.py plugin/hooks/brief_cost.py tests/hooks/test_transcript.py
git commit -m "refactor(hooks): вынести агрегацию транскрипта в общий stdlib-модуль (PRI-247)"
```

---

### Task 2: PreToolUse-хук съёма расхода ревью в sidecar

**Files:**
- Create: `plugin/hooks/review_cost.py`
- Modify: `plugin/hooks/hooks.json` (добавить секцию `PreToolUse`)
- Test: `tests/hooks/test_review_cost.py` (новый)

**Interfaces:**
- Consumes: `_transcript.read_jsonl`, `aggregate_usage`, `find_window_start`, `resolve_transcript`, `weigh`, `empty_bucket` (Task 1).
- Produces:
  - `sidecar_path(repo: str, pr: int) -> str` — `<tempdir>/reviewer-review-cost/<repo с '/'→'_'>-<pr>.json`
  - `STAGE_MARKERS: dict[str, str]` — маркер в промпте субагента → стадия
  - `find_prepare_index(lines: list, repo: str, pr: int) -> int`
  - `run(payload: dict) -> int` — всегда 0
  - формат sidecar версии `1` (читает Task 3)

- [ ] **Step 1: Написать падающие тесты хука**

```python
# tests/hooks/test_review_cost.py
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugin" / "hooks"))

import review_cost  # noqa: E402


def _assistant(model, usage, sidechain=False, extra=None):
    line = {"type": "assistant", "isSidechain": sidechain,
            "message": {"model": model, "usage": usage}}
    line.update(extra or {})
    return line


def _prepare_call(repo, pr):
    return {"type": "assistant", "message": {"model": "opus", "usage": {}, "content": [
        {"type": "tool_use", "name": "mcp__reviewer__prepare_review",
         "input": {"repo": repo, "pr": pr}}]}}


def _sidechain_prompt(text):
    return {"type": "user", "isSidechain": True, "message": {"content": text}}


def _payload(tmp_path, lines, repo="owner/name", pr=7):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return {"transcript_path": str(transcript), "session_id": "s1",
            "tool_name": "mcp__reviewer__publish_review",
            "tool_input": {"repo": repo, "pr": pr, "summary": "s"}}


def test_sidecar_path_is_deterministic_from_repo_and_pr():
    first = review_cost.sidecar_path("owner/name", 7)
    assert first == review_cost.sidecar_path("owner/name", 7)
    assert first != review_cost.sidecar_path("owner/name", 8)
    assert Path(first).name == "owner_name-7.json"


def test_window_starts_at_prepare_review_of_same_pr(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    lines = [
        _assistant("opus", {"output_tokens": 1000}),          # до окна — не считается
        _prepare_call("owner/name", 7),
        _assistant("opus", {"output_tokens": 4}),             # в окне
    ]
    review_cost.run(_payload(tmp_path, lines))
    data = json.loads(Path(review_cost.sidecar_path("owner/name", 7)).read_text(encoding="utf-8"))
    assert data["usage"]["by_model"]["opus"]["output"] == 4


def test_window_ignores_prepare_of_another_pr(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    lines = [
        _prepare_call("owner/name", 99),
        _assistant("opus", {"output_tokens": 3}),
        _prepare_call("owner/name", 7),
        _assistant("opus", {"output_tokens": 5}),
    ]
    review_cost.run(_payload(tmp_path, lines))
    data = json.loads(Path(review_cost.sidecar_path("owner/name", 7)).read_text(encoding="utf-8"))
    assert data["usage"]["by_model"]["opus"]["output"] == 5


def test_falls_back_to_skill_marker_when_no_prepare(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    lines = [
        {"type": "user", "message": {"content":
            "Base directory for this skill: /x/skills/review-pr"}},
        _assistant("opus", {"output_tokens": 6}),
    ]
    review_cost.run(_payload(tmp_path, lines))
    data = json.loads(Path(review_cost.sidecar_path("owner/name", 7)).read_text(encoding="utf-8"))
    assert data["usage"]["by_model"]["opus"]["output"] == 6


def test_no_window_writes_no_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    review_cost.run(_payload(tmp_path, [_assistant("opus", {"output_tokens": 6})]))
    assert not Path(review_cost.sidecar_path("owner/name", 7)).exists()


def test_sidechain_attributed_to_stage_by_reference_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    lines = [
        _prepare_call("owner/name", 7),
        _sidechain_prompt("follow references/verify-prompt.md exactly"),
        _assistant("sonnet", {"output_tokens": 10}, sidechain=True),
        _sidechain_prompt("follow references/analyze-prompt.md exactly"),
        _assistant("sonnet", {"output_tokens": 20}, sidechain=True),
        _assistant("opus", {"output_tokens": 1}),
    ]
    review_cost.run(_payload(tmp_path, lines))
    data = json.loads(Path(review_cost.sidecar_path("owner/name", 7)).read_text(encoding="utf-8"))
    by_stage = data["usage"]["by_stage"]
    assert by_stage["verify"]["output"] == 10
    assert by_stage["analyze"]["output"] == 20
    assert by_stage["orchestrator"]["output"] == 1


def test_unrecognised_sidechain_goes_to_subagent_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    lines = [
        _prepare_call("owner/name", 7),
        _sidechain_prompt("do something unlabelled"),
        _assistant("sonnet", {"output_tokens": 9}, sidechain=True),
    ]
    review_cost.run(_payload(tmp_path, lines))
    data = json.loads(Path(review_cost.sidecar_path("owner/name", 7)).read_text(encoding="utf-8"))
    assert data["usage"]["by_stage"]["subagent"]["output"] == 9


def test_total_cost_is_weighted_not_summed(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    lines = [
        _prepare_call("owner/name", 7),
        _assistant("opus", {"input_tokens": 100, "output_tokens": 100,
                            "cache_creation_input_tokens": 100,
                            "cache_read_input_tokens": 100}),
    ]
    review_cost.run(_payload(tmp_path, lines))
    data = json.loads(Path(review_cost.sidecar_path("owner/name", 7)).read_text(encoding="utf-8"))
    assert data["total_cost"] == 735.0        # не 400 — веса, а не сумма
    assert data["model"] == "opus"
    assert data["transcript_source"] == "payload"
    assert data["version"] == 1


def test_broken_payload_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    assert review_cost.run({}) == 0
    assert review_cost.run({"tool_input": {"repo": "owner/name"}}) == 0


def test_stage_markers_cover_every_reference_prompt():
    """Guard: новый reference-промпт обязан получить строку в STAGE_MARKERS."""
    refs = Path(__file__).resolve().parents[2] / "plugin" / "skills" / "review-pr" / "references"
    for prompt in sorted(refs.glob("*-prompt.md")):
        assert any(prompt.name in marker for marker in review_cost.STAGE_MARKERS), (
            f"{prompt.name} не описан в review_cost.STAGE_MARKERS"
        )
```

- [ ] **Step 2: Прогнать тесты и убедиться, что падают**

Run: `.venv/bin/pytest tests/hooks/test_review_cost.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'review_cost'`

- [ ] **Step 3: Написать хук**

```python
# plugin/hooks/review_cost.py
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
from _transcript import (  # noqa: E402
    aggregate_usage, empty_bucket, find_window_start, message_text, read_jsonl,
    resolve_transcript, weigh,
)

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
```

- [ ] **Step 4: Прогнать тесты хука**

Run: `.venv/bin/pytest tests/hooks/test_review_cost.py -q`
Expected: PASS (10 тестов)

- [ ] **Step 5: Зарегистрировать хук**

`plugin/hooks/hooks.json` — добавить секцию `PreToolUse` рядом с существующей `PostToolUse`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "mcp__reviewer__publish_review|mcp__plugin_rag-reviewer_reviewer__publish_review",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/review_cost.py\""
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/brief_post_write.py\""
          }
        ]
      },
      {
        "matcher": "mcp__reviewer__.*|mcp__plugin_rag-reviewer_reviewer__.*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/reviewer_defect.py\""
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 6: Проверить, что весь набор хуков цел**

Run: `.venv/bin/pytest tests/hooks/ -q && .venv/bin/python -c "import json;print(sorted(json.load(open('plugin/hooks/hooks.json'))['hooks']))"`
Expected: PASS; печатает `['PostToolUse', 'PreToolUse']`

- [ ] **Step 7: Коммит**

```bash
git add plugin/hooks/review_cost.py plugin/hooks/hooks.json tests/hooks/test_review_cost.py
git commit -m "feat(hooks): снимать расход окна ревью в sidecar перед publish_review (PRI-247)"
```

---

### Task 3: Сервер читает sidecar в publish_review

**Files:**
- Create: `reviewer/services/cost_sidecar.py`
- Modify: `reviewer/mcp/service.py` (сборка `_RunMetadata` в `publish_review`, ~строка 3036)
- Test: `tests/services/test_cost_sidecar.py` (новый), `tests/mcp/test_publish.py` (дополнить)

**Interfaces:**
- Consumes: формат sidecar версии `1` из Task 2.
- Produces:
  - `reviewer.services.cost_sidecar.sidecar_path(repo: str, pr: int) -> str`
  - `reviewer.services.cost_sidecar.read_cost_sidecar(repo: str, pr: int) -> dict | None` — возвращает `{"model", "usage", "total_cost"}` или `None`; удаляет файл после попытки чтения
  - `reviewer.services.cost_sidecar.merge_metadata(explicit: dict, sidecar: dict | None) -> dict` — слияние по полям, явное приоритетнее

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/services/test_cost_sidecar.py
import json
import tempfile
from pathlib import Path

import pytest

from reviewer.services import cost_sidecar


@pytest.fixture
def tmp_tempdir(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def _write(repo, pr, data):
    path = Path(cost_sidecar.sidecar_path(repo, pr))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _valid(**over):
    data = {"version": 1, "repo": "owner/name", "pr": 7, "model": "opus",
            "usage": {"by_model": {"opus": {"output": 3}}}, "total_cost": 12.5,
            "written_at": "2999-01-01T00:00:00+00:00"}
    data.update(over)
    return data


def test_reads_valid_sidecar_and_deletes_it(tmp_tempdir):
    path = _write("owner/name", 7, _valid())
    got = cost_sidecar.read_cost_sidecar("owner/name", 7)
    assert got["model"] == "opus"
    assert got["total_cost"] == 12.5
    assert got["usage"]["by_model"]["opus"]["output"] == 3
    assert not path.exists(), "sidecar обязан удаляться, иначе следующее ревью переиспользует замер"


def test_missing_sidecar_returns_none(tmp_tempdir):
    assert cost_sidecar.read_cost_sidecar("owner/name", 7) is None


def test_broken_json_is_ignored_and_removed(tmp_tempdir):
    path = Path(cost_sidecar.sidecar_path("owner/name", 7))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert cost_sidecar.read_cost_sidecar("owner/name", 7) is None
    assert not path.exists()


def test_foreign_version_is_ignored(tmp_tempdir):
    _write("owner/name", 7, _valid(version=99))
    assert cost_sidecar.read_cost_sidecar("owner/name", 7) is None


def test_stale_sidecar_is_ignored(tmp_tempdir):
    _write("owner/name", 7, _valid(written_at="2000-01-01T00:00:00+00:00"))
    assert cost_sidecar.read_cost_sidecar("owner/name", 7) is None


def test_sidecar_of_another_pr_is_not_read(tmp_tempdir):
    _write("owner/name", 8, _valid(pr=8))
    assert cost_sidecar.read_cost_sidecar("owner/name", 7) is None


def test_merge_prefers_explicit_per_field():
    sidecar = {"model": "opus", "usage": {"a": 1}, "total_cost": 10.0}
    merged = cost_sidecar.merge_metadata(
        {"model": "gpt", "usage": None, "total_cost": None}, sidecar)
    assert merged == {"model": "gpt", "usage": {"a": 1}, "total_cost": 10.0}


def test_merge_without_sidecar_returns_explicit():
    explicit = {"model": None, "usage": None, "total_cost": None}
    assert cost_sidecar.merge_metadata(explicit, None) == explicit
```

- [ ] **Step 2: Прогнать тесты и убедиться, что падают**

Run: `.venv/bin/pytest tests/services/test_cost_sidecar.py -q`
Expected: FAIL — `ImportError: cannot import name 'cost_sidecar'`

- [ ] **Step 3: Написать модуль**

```python
# reviewer/services/cost_sidecar.py
"""Чтение sidecar-файла с расходом окна ревью (PRI-247).

Расход снимает клиентский PreToolUse-хук плагина (plugin/hooks/review_cost.py)
и кладёт JSON по детерминированному пути от repo и номера PR; publish_review
читает его здесь. Канал файловый, поэтому работает, когда хук и reviewer-mcp
делят файловую систему (stdio-запуск). При удалённом MCP файла не будет —
это штатный случай «sidecar отсутствует», ревью публикуется без метаданных.

Путь ДУБЛИРУЕТ формулу хука: хук исполняется системным python3 и не может
импортировать пакет reviewer. Совпадение формул закреплено guard-тестом.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

SIDECAR_DIR = "reviewer-review-cost"
SIDECAR_VERSION = 1
# Замер старше суток относится к другому ревью того же PR — применять его нельзя.
MAX_AGE = timedelta(hours=24)


def sidecar_path(repo: str, pr: int) -> str:
    """Детерминированный путь sidecar от repo и номера PR."""
    safe = str(repo).replace("/", "_").replace("..", "_")
    return os.path.join(tempfile.gettempdir(), SIDECAR_DIR, f"{safe}-{pr}.json")


def _drop(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def read_cost_sidecar(repo: str, pr: int) -> dict | None:
    """Прочитать и удалить sidecar. Любая негодность → None (fail-open).

    Returns:
        {"model", "usage", "total_cost"} либо None.
    """
    path = sidecar_path(repo, pr)
    try:
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        _drop(path)
        if not isinstance(data, dict) or data.get("version") != SIDECAR_VERSION:
            return None
        if str(data.get("repo") or "") != str(repo) or int(data.get("pr") or -1) != int(pr):
            return None
        written = data.get("written_at")
        if written:
            try:
                stamp = datetime.fromisoformat(written)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - stamp > MAX_AGE:
                    return None
            except ValueError:
                return None
        return {
            "model": data.get("model"),
            "usage": data.get("usage"),
            "total_cost": data.get("total_cost"),
        }
    except Exception as exc:  # noqa: BLE001
        log.debug("Sidecar расхода непригоден (%s): %s", path, exc)
        _drop(path)
        return None


def merge_metadata(explicit: dict, sidecar: dict | None) -> dict:
    """Слияние метаданных: явные аргументы клиента приоритетнее — по полям.

    Слияние именно пофайловое, а не «всё или ничего»: CLI, умеющий отдать
    model, но не расход, не должен терять расход из sidecar.
    """
    merged = dict(explicit)
    if not sidecar:
        return merged
    for key in ("model", "usage", "total_cost"):
        if merged.get(key) is None and sidecar.get(key) is not None:
            merged[key] = sidecar[key]
    return merged
```

- [ ] **Step 4: Прогнать тесты модуля**

Run: `.venv/bin/pytest tests/services/test_cost_sidecar.py -q`
Expected: PASS (8 тестов)

- [ ] **Step 5: Подключить чтение к publish_review**

В `reviewer/mcp/service.py`, в `publish_review`, заменить прямую сборку `_RunMetadata` (сейчас ~строка 3036) на слияние с sidecar:

```python
        # PRI-247: расход окна ревью снимает клиентский хук в sidecar; явные
        # аргументы клиента приоритетнее — по каждому полю отдельно.
        from reviewer.services.cost_sidecar import merge_metadata, read_cost_sidecar
        merged = merge_metadata(
            {"model": model, "usage": usage, "total_cost": total_cost},
            read_cost_sidecar(repo, pr),
        )
        metadata = _RunMetadata(
            model=merged["model"],
            model_verify=model_verify,
            usage=merged["usage"],
            total_cost=merged["total_cost"],
            started_at=started_at,
            steps=steps,
        )
```

- [ ] **Step 6: Добавить тесты на публикацию с sidecar**

Дописать в `tests/mcp/test_publish.py`, используя существующие хелперы файла:
`_make_mcp_service_with_publish()` (возвращает `svc, vcs, history`, где `history` — `_FakeHistory`
со списком `runs`), `_submit_then_publish(...)` и константу `RAW`. Репозиторий в этих тестах —
`"o/r"`, PR — `7`. Импорты `json`, `tempfile`, `Path` добавить в шапку файла, если их там нет.

```python
def _write_sidecar(tmp_path, *, repo="o/r", pr=7, **over):
    """Sidecar в подменённом tempdir; возвращает путь к файлу."""
    from reviewer.services import cost_sidecar
    data = {"version": 1, "repo": repo, "pr": pr, "model": "claude-opus-5",
            "usage": {"by_model": {"claude-opus-5": {"output": 5}}},
            "total_cost": 42.5,
            "written_at": datetime.now(timezone.utc).isoformat()}
    data.update(over)
    path = Path(cost_sidecar.sidecar_path(repo, pr))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_fills_cost_from_sidecar(_ov, _ch, monkeypatch, tmp_path) -> None:
    """PRI-247: клиент расход не передал — он берётся из sidecar и файл исчезает."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    path = _write_sidecar(tmp_path)
    svc, _, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True)
    run = history.runs[0]
    assert run["model"] == "claude-opus-5"
    assert float(run["total_cost"]) == 42.5
    assert run["usage"]["by_model"]["claude-opus-5"]["output"] == 5
    assert not path.exists()


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_explicit_client_metadata_wins_over_sidecar(_ov, _ch, monkeypatch, tmp_path) -> None:
    """Явная model клиента приоритетнее, но расход всё равно подхватывается из sidecar."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    _write_sidecar(tmp_path)
    svc, _, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True, model="explicit-model")
    run = history.runs[0]
    assert run["model"] == "explicit-model"
    assert float(run["total_cost"]) == 42.5


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_without_sidecar_still_works(_ov, _ch, monkeypatch, tmp_path) -> None:
    """Fail-open: sidecar нет — ревью публикуется, поля расхода пусты."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    svc, _, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    report = _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True)
    assert report["run_id"] is not None
    assert history.runs[0]["total_cost"] is None
```

- [ ] **Step 7: Добавить guard-тест на дублирование формулы пути**

Формула пути sidecar живёт в двух местах: хук не может импортировать пакет `reviewer`. Расхождение
формул тихо разорвало бы канал — тест это ловит. Дописать в `tests/hooks/test_review_cost.py`:

```python
def test_sidecar_path_matches_server_side_formula(tmp_path, monkeypatch):
    """Формула пути продублирована в хуке и на сервере — они обязаны совпадать."""
    import tempfile as _tempfile

    from reviewer.services import cost_sidecar

    monkeypatch.setattr(review_cost.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(tmp_path))
    for repo, pr in (("owner/name", 7), ("o/r", 1), ("a/b-c", 42)):
        assert review_cost.sidecar_path(repo, pr) == cost_sidecar.sidecar_path(repo, pr)
    assert review_cost.SIDECAR_VERSION == cost_sidecar.SIDECAR_VERSION
```

- [ ] **Step 8: Прогнать серверные тесты**

Run: `.venv/bin/pytest tests/services/test_cost_sidecar.py tests/mcp/ tests/hooks/ -q`
Expected: PASS

- [ ] **Step 9: Коммит**

```bash
git add reviewer/services/cost_sidecar.py reviewer/mcp/service.py tests/services/test_cost_sidecar.py tests/mcp/test_publish.py tests/hooks/test_review_cost.py
git commit -m "feat(mcp): читать расход ревью из sidecar в publish_review (PRI-247)"
```

---

### Task 4: Серверный стадийный учёт review_steps без хуков

**Files:**
- Modify: `reviewer/mcp/service.py` (`_invoke_tool` ~383-407; методы `submit_findings`, `submit_verdicts`, `get_candidate_findings`; нормализация клиентских шагов в `_record_history` ~3184-3197)
- Test: `tests/mcp/test_steps.py` (новый)

**Interfaces:**
- Consumes: ничего из предыдущих задач (канал полностью независим).
- Produces: `reviewer.mcp.service.TOOL_STAGES: dict[str, str]`; формат шага — существующие колонки `review_steps` плюс ключи `args_bytes` / `result_bytes` внутри `tool_calls[0]`.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/mcp/test_steps.py
from reviewer.mcp.service import TOOL_STAGES, build_step


def test_stage_map_covers_pr_session_tools():
    assert TOOL_STAGES["search_code"] == "analyze"
    assert TOOL_STAGES["read_file"] == "analyze"
    assert TOOL_STAGES["get_impact"] == "analyze"
    assert TOOL_STAGES["get_candidate_findings"] == "verify"
    assert TOOL_STAGES["submit_verdicts"] == "verify"
    assert TOOL_STAGES["submit_findings"] == "synthesize"


def test_build_step_records_stage_and_payload_sizes():
    step = build_step(seq=3, name="search_code", args={"query": "abc"}, result="x" * 10)
    assert step["stage"] == "analyze"
    assert step["kind"] == "tool_call"
    assert step["seq"] == 3
    assert step["name"] == "search_code"
    tc = step["tool_calls"][0]
    assert tc["result_bytes"] == 10
    assert tc["args_bytes"] > 0


def test_build_step_truncates_text_but_not_byte_count():
    step = build_step(seq=0, name="read_file", args={"path": "a.py"}, result="y" * 5000)
    assert len(step["text"]) == 500
    assert step["tool_calls"][0]["result_bytes"] == 5000


def test_unknown_tool_falls_back_to_analyze():
    assert build_step(seq=0, name="mystery", args={}, result="")["stage"] == "analyze"


def test_non_string_result_has_zero_bytes_and_no_text():
    step = build_step(seq=0, name="submit_findings", args={}, result={"recorded": 2})
    assert step["text"] is None
    assert step["tool_calls"][0]["result_bytes"] == 0


def test_client_steps_get_default_stage_and_kind():
    from reviewer.mcp.service import normalize_client_step
    assert normalize_client_step({"name": "prepare"}) == {
        "name": "prepare", "stage": "client", "kind": "client_step",
    }
    # заполненные клиентом значения не перетираются
    kept = normalize_client_step({"name": "x", "stage": "analyze", "kind": "llm_call"})
    assert kept["stage"] == "analyze" and kept["kind"] == "llm_call"
```

- [ ] **Step 2: Прогнать тесты и убедиться, что падают**

Run: `.venv/bin/pytest tests/mcp/test_steps.py -q`
Expected: FAIL — `ImportError: cannot import name 'TOOL_STAGES'`

- [ ] **Step 3: Реализовать карту стадий и сборку шага**

В `reviewer/mcp/service.py` рядом с `_MAX_SESSION_STEPS` добавить:

```python
# PRI-247: стадия шага выводится из имени тула — серверный канал трейса
# работает в любом CLI, без клиентских хуков.
TOOL_STAGES = {
    "search_code": "analyze",
    "read_file": "analyze",
    "get_definition": "analyze",
    "find_callers": "analyze",
    "get_related_symbols": "analyze",
    "get_changed_file_diff": "analyze",
    "get_impact": "analyze",
    "get_candidate_findings": "verify",
    "submit_verdicts": "verify",
    "submit_findings": "synthesize",
}


def build_step(seq: int, name: str, args: dict, result) -> dict:
    """Строка review_steps для одного тул-вызова PR-сессии.

    Размеры payload кладём в существующий tool_calls JSONB — новых колонок и
    миграции не требуется. tokens/cost не заполняем: сервер не видит LLM-вызовов.
    """
    import json as _json
    text = result[:500] if isinstance(result, str) else None
    try:
        args_bytes = len(_json.dumps(args, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        args_bytes = 0
    result_bytes = len(result.encode("utf-8")) if isinstance(result, str) else 0
    return {
        "stage": TOOL_STAGES.get(name, "analyze"),
        "unit": args.get("path") or args.get("node_id") or args.get("symbol") or "",
        "seq": seq,
        "kind": "tool_call",
        "name": name,
        "text": text,
        "tool_calls": [{
            "name": name, "args": args,
            "args_bytes": args_bytes, "result_bytes": result_bytes,
        }],
    }


def normalize_client_step(step: dict) -> dict:
    """Клиентский шаг с пустыми stage/kind получает явные дефолты.

    Замер PRI-247 показал шаги с заполненным name, но пустыми stage/kind —
    в стадийной агрегации они падали в безымянное ведро.
    """
    out = dict(step)
    if not out.get("stage"):
        out["stage"] = "client"
    if not out.get("kind"):
        out["kind"] = "client_step"
    return out
```

- [ ] **Step 4: Перевести `_invoke_tool` на build_step**

Заменить тело записи шага в `_invoke_tool` (~строки 395-406):

```python
        if len(s.steps) < _MAX_SESSION_STEPS:
            s.steps.append(build_step(len(s.steps), name, args, result))
```

- [ ] **Step 5: Записывать шаги verify/synthesize**

`submit_findings`, `submit_verdicts` и `get_candidate_findings` не идут через `_invoke_tool`. В каждом из трёх методов, сразу перед `return`, добавить запись шага (взяв сессию, которая в методе уже получена):

```python
        if len(s.steps) < _MAX_SESSION_STEPS:
            s.steps.append(build_step(len(s.steps), "submit_findings", {"count": len(findings)}, result))
```

Для `submit_verdicts` — имя `"submit_verdicts"` и `{"count": len(verdicts)}`; для `get_candidate_findings` — имя `"get_candidate_findings"`, `{}` и фактический результат. `result` — то, что метод возвращает.

- [ ] **Step 6: Применить нормализацию к клиентским шагам**

В `_record_history`, там где формируется `all_steps` (~3194), пропустить клиентские шаги через нормализацию:

```python
            all_steps = session.steps + [normalize_client_step(x) for x in client_steps]
```

- [ ] **Step 7: Прогнать тесты**

Run: `.venv/bin/pytest tests/mcp/ -q`
Expected: PASS — новые тесты зелёные, существующие тесты MCP не сломаны

- [ ] **Step 8: Коммит**

```bash
git add reviewer/mcp/service.py tests/mcp/test_steps.py
git commit -m "feat(mcp): стадии и размер payload в серверном трейсе ревью (PRI-247)"
```

---

### Task 5: Разрез по стадиям в админке, манифесты и документация

**Files:**
- Modify: `reviewer/web/history.py` (новый метод `stage_breakdown`), `reviewer/web/api.py:118-127` (поле `by_stage` в ответе trace), `web/frontend/src/api.ts` (тип), `web/frontend/src/pages/TraceView.tsx` (таблица разреза), `plugin/skills/review-pr/SKILL.md` (~строка 127), `README.md`, `README.ru.md`, `CLAUDE.md`
- Test: `tests/web/test_history.py`, `tests/web/test_api.py` (дополнить)
- Run: `scripts/update_codex_plugin_manifest.py`

**Interfaces:**
- Consumes: формат шага из Task 4 (`tool_calls[0].args_bytes` / `result_bytes`), `usage.by_stage` из Task 2/3.
- Produces: `ReviewHistory.stage_breakdown(run_id: int) -> list[dict]` со строками `{"stage", "steps", "args_bytes", "result_bytes"}`; поле `by_stage` в `GET /api/runs/{id}/trace`.

- [ ] **Step 1: Написать падающий тест агрегации**

```python
# дописать в tests/web/test_history.py
def test_stage_breakdown_groups_steps_and_bytes():
    steps = [
        {"stage": "analyze", "tool_calls": [{"args_bytes": 10, "result_bytes": 100}]},
        {"stage": "analyze", "tool_calls": [{"args_bytes": 5, "result_bytes": 50}]},
        {"stage": "verify", "tool_calls": [{"args_bytes": 1, "result_bytes": 2}]},
        {"stage": "client", "tool_calls": None},
    ]
    from reviewer.web.history import aggregate_stages
    rows = aggregate_stages(steps)
    by_stage = {r["stage"]: r for r in rows}
    assert by_stage["analyze"] == {
        "stage": "analyze", "steps": 2, "args_bytes": 15, "result_bytes": 150}
    assert by_stage["verify"]["steps"] == 1
    assert by_stage["client"] == {
        "stage": "client", "steps": 1, "args_bytes": 0, "result_bytes": 0}


def test_stage_breakdown_of_empty_trace_is_empty():
    from reviewer.web.history import aggregate_stages
    assert aggregate_stages([]) == []
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `.venv/bin/pytest tests/web/test_history.py -q -k stage_breakdown`
Expected: FAIL — `ImportError: cannot import name 'aggregate_stages'`

- [ ] **Step 3: Реализовать агрегацию**

В `reviewer/web/history.py` добавить чистую функцию модульного уровня (тестируется без БД):

```python
def aggregate_stages(steps: list) -> list:
    """Разрез трейса по стадиям: число шагов и размеры payload.

    Токенов и стоимости здесь нет: сервер не видит LLM-вызовов. Расход по
    стадиям приходит отдельным каналом — из usage.by_stage прогона.
    """
    order: list = []
    acc: dict = {}
    for step in steps:
        stage = step.get("stage") or "unknown"
        row = acc.get(stage)
        if row is None:
            row = {"stage": stage, "steps": 0, "args_bytes": 0, "result_bytes": 0}
            acc[stage] = row
            order.append(stage)
        row["steps"] += 1
        calls = step.get("tool_calls") or []
        for call in calls:
            if not isinstance(call, dict):
                continue
            row["args_bytes"] += int(call.get("args_bytes") or 0)
            row["result_bytes"] += int(call.get("result_bytes") or 0)
    return [acc[s] for s in order]
```

- [ ] **Step 4: Отдать разрез в API**

`reviewer/web/api.py`, эндпоинт `get_trace` (сейчас возвращает `{"steps": steps}`):

```python
            steps = history.get_trace(run_id)
            return JSONResponse({"steps": steps, "by_stage": aggregate_stages(steps)})
```

Импорт `aggregate_stages` — из `reviewer.web.history`.

Дописать тест в `tests/web/test_api.py` по образцу соседних тестов файла: ответ `/api/runs/{id}/trace` содержит ключ `by_stage`, а для прогона без шагов — пустой список.

- [ ] **Step 5: Показать разрез в UI**

`web/frontend/src/api.ts` — добавить тип и расширить возврат `getTrace`:

```ts
export type StageBreakdown = {
  stage: string
  steps: number
  args_bytes: number
  result_bytes: number
}
```

`web/frontend/src/pages/TraceView.tsx` — над списком шагов отрисовать таблицу разреза: колонки «Стадия», «Шагов», «Отдано, КБ», «Получено, КБ». Стадии подписывать через существующий `STAGE_LABELS`, дополнив его новыми ключами: `risk: 'Риски'`, `blast_radius: 'Радиус поражения'`, `requirements: 'Требования'`, `performance: 'Производительность'`, `maintainability: 'Поддерживаемость'`, `client: 'Клиент'`, `orchestrator: 'Оркестратор'`, `subagent: 'Субагент'`. Расход подписывать «усл. ед.», без знака валюты; отсутствующее значение показывать прочерком, а не нулём.

- [ ] **Step 6: Прогнать веб-тесты и сборку фронта**

Run: `.venv/bin/pytest tests/web/ -q && (cd web/frontend && npm run build)`
Expected: PASS; сборка фронта без ошибок TypeScript

- [ ] **Step 7: Обновить скилл и документацию**

- `plugin/skills/review-pr/SKILL.md`, шаг 6: заменить «If the CLI provides model/usage/cost metadata, pass them…» на формулировку, что расход снимается автоматически хуком плагина в sidecar, а явная передача `model`/`usage`/`total_cost` остаётся поддержанной и приоритетной, когда CLI умеет их отдать.
- `README.md` и `README.ru.md`: в разделе наблюдаемости описать два канала (расход — клиентский хук, трейс — сервер) и что `total_cost` — условные единицы, не доллары.
- `CLAUDE.md`, раздел «Неочевидные факты»: добавить пункт про sidecar-канал (путь от repo+PR в tempdir, дублирование формулы пути в хуке и сервере под guard-тестом, приоритет явных аргументов по полям, взвешенные единицы вместо суммы токенов, неработоспособность при удалённом MCP).

- [ ] **Step 8: Пересобрать codex-манифесты**

Run: `.venv/bin/python scripts/update_codex_plugin_manifest.py && .venv/bin/pytest -q`
Expected: манифесты обновлены; полный unit-прогон зелёный (install-тесты в том числе)

- [ ] **Step 9: Коммит**

```bash
git add reviewer/web/history.py reviewer/web/api.py web/frontend/src tests/web plugin README.md README.ru.md CLAUDE.md
git commit -m "feat(web): разрез расхода ревью по стадиям в админке (PRI-247)"
```

---

## Приёмка

Все критерии задачи проверяются на живом прогоне `review-pr` после установки обновлённого плагина:

1. `usage` и `total_cost` в `review_runs` непусты, `model` — реальная модель, а не `claude-code`.
2. `review_steps` содержит шаги со стадиями `analyze`/`verify`/`synthesize` даже при выключенных хуках.
3. Ревью публикуется при отсутствующем sidecar и при упавшем хуке.
4. `.venv/bin/pytest tests/hooks/test_brief_cost.py -q` зелёный без правок в самом файле теста.
5. Разрез по стадиям виден на странице трейса прогона.
6. `total_cost` — взвешенная величина: для равных бакетов по 100 токенов даёт 735, а не 400.
