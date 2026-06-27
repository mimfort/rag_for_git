# Токены этапа solve-task в брифе — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Опциональный PostToolUse-хук плагина дописывает в бриф solve-task блок «## Токены (этап solve-task)» с расходом LLM-токенов на этап (4 бакета), считая детерминированно по транскрипту сессии.

**Architecture:** Один stdlib-скрипт `plugin/hooks/brief_cost.py`, запускаемый Claude Code как `PostToolUse` на инструмент `Write`. Скрипт сам делает path-guard (только briefs/), читает флаг из `.review.yml`, находит окно этапа в транскрипте (от последнего маркера `skills/solve-task` до EOF), агрегирует `message.usage` по моделям и идемпотентно вписывает блок в файл брифа. Все ошибки → no-op (`exit 0`), сессия никогда не падает.

**Tech Stack:** Python 3.11 stdlib (`json`, `os`, `sys`, `re`); `PyYAML` опционально (yaml-primary разбор флага со stdlib-фолбэком, т.к. хук исполняется системным `python3`, где PyYAML может отсутствовать). Тесты — pytest на фейках.

## Global Constraints

- Язык проекта — русский: комментарии, докстринги, текст блока в брифе. Код-идентификаторы — латиницей.
- Линт: `ruff check .` (line-length 100, target py311) — хук тоже линтится (он под `plugin/`).
- Коммиты — Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- Хук исполняется **системным `python3`** (не venv) → разрешён только stdlib; `import yaml` — внутри `try` с фолбэком.
- **Fail-open везде:** любая ошибка/нехватка данных → `exit 0`, файл брифа не модифицируется.
- **stdin-контракт.** `tool_name`/`tool_input.file_path`/`tool_output` — PostToolUse-специфичные поля; `transcript_path`/`cwd` — общие поля всех хуков (подтверждено `claude-code-guide`, но в PostToolUse-секции доков не продублированы). Зависимость от `cwd` снята (ищем `.review.yml` от `file_path`). `transcript_path` — единственный источник окна; если его в payload не окажется → fail-open no-op. Эмпирическая проверка контракта после установки: `BRIEF_COST_DEBUG=1` → хук пишет ключи payload в stderr.
- Точный заголовок блока (маркер идемпотентности): `## Токены (этап solve-task)`.
- 4 бакета 1:1 из транскрипта: `fresh_in`←`input_tokens`, `output`←`output_tokens`, `cache_write`←`cache_creation_input_tokens`, `cache_read`←`cache_read_input_tokens`. Долларов нет.
- Маркер старта окна — user-сообщение, содержащее одновременно `Base directory for this skill:` и `skills/solve-task`; берётся **последнее** вхождение.

---

### Task 1: Форматирование токенов + рендер/идемпотентная вставка блока

**Files:**
- Create: `plugin/hooks/brief_cost.py`
- Create: `tests/hooks/__init__.py` (пустой)
- Test: `tests/hooks/test_brief_cost.py`

**Interfaces:**
- Produces:
  - `HEADER: str = "## Токены (этап solve-task)"`
  - `human_tokens(n: int) -> str` — `512→"512"`, `9900→"9.9K"`, `164000→"164K"`, `14200000→"14.2M"`
  - `render_block(by_model: dict[str, dict[str, int]]) -> str` — текст блока (без хвостового `\n`); ключи бакетов: `fresh_in`/`output`/`cache_write`/`cache_read`
  - `upsert_block(text: str, block: str) -> str` — заменяет существующий блок по `HEADER` (от заголовка до следующего `## ` или EOF) либо дописывает в конец; результат всегда оканчивается одним `\n`

- [ ] **Step 1: Написать падающие тесты**

`tests/hooks/__init__.py` — создать пустым.

`tests/hooks/test_brief_cost.py`:
```python
"""Unit-тесты PostToolUse-хука brief_cost (токены этапа solve-task в брифе)."""
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOK_PATH = ROOT / "plugin" / "hooks" / "brief_cost.py"


def _load():
    spec = importlib.util.spec_from_file_location("brief_cost", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bc = _load()


def test_human_tokens_formats_k_and_m():
    assert bc.human_tokens(512) == "512"
    assert bc.human_tokens(9900) == "9.9K"
    assert bc.human_tokens(164000) == "164K"
    assert bc.human_tokens(14200000) == "14.2M"


def test_render_block_single_model():
    by_model = {
        "claude-opus-4-8": {
            "fresh_in": 9900, "output": 164000,
            "cache_write": 533000, "cache_read": 14200000,
        },
    }
    block = bc.render_block(by_model)
    assert block.splitlines()[0] == "## Токены (этап solve-task)"
    assert "Модель: claude-opus-4-8" in block
    assert "fresh-in 9.9K · out 164K · cache-write 533K · cache-read 14.2M" in block
    # сумма всех бакетов = 9900+164000+533000+14200000 = 14_906_900 → 14.9M
    assert "Всего: 14.9M токенов" in block


def test_upsert_block_appends_when_absent():
    brief = "# Brief — test\n\n## Task\nдетали\n"
    block = ("## Токены (этап solve-task)\nМодель: x\n"
             "fresh-in 1K · out 1K · cache-write 0 · cache-read 0\nВсего: 2K токенов")
    out = bc.upsert_block(brief, block)
    assert out.count("## Токены (этап solve-task)") == 1
    assert out.startswith("# Brief — test")
    assert out.rstrip().endswith("Всего: 2K токенов")
    assert out.endswith("\n")


def test_upsert_block_replaces_when_present():
    block_old = ("## Токены (этап solve-task)\nМодель: x\n"
                 "fresh-in 1K · out 1K · cache-write 0 · cache-read 0\nВсего: 2K токенов")
    brief = "# Brief — test\n\n## Task\nдетали\n\n" + block_old + "\n"
    block_new = ("## Токены (этап solve-task)\nМодель: y\n"
                 "fresh-in 3K · out 0 · cache-write 0 · cache-read 0\nВсего: 3K токенов")
    out = bc.upsert_block(brief, block_new)
    assert out.count("## Токены (этап solve-task)") == 1
    assert "Модель: y" in out
    assert "Модель: x" not in out
    assert "## Task" in out
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/hooks/test_brief_cost.py -q`
Expected: FAIL — `ModuleNotFoundError`/`FileNotFoundError` (нет `plugin/hooks/brief_cost.py`).

- [ ] **Step 3: Создать `plugin/hooks/brief_cost.py` с форматированием и вставкой**

```python
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
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/hooks/test_brief_cost.py -q`
Expected: PASS (4 теста).

- [ ] **Step 5: Линт**

Run: `.venv/bin/ruff check plugin/hooks/brief_cost.py tests/hooks/`
Expected: без ошибок.

- [ ] **Step 6: Commit**

```bash
git add plugin/hooks/brief_cost.py tests/hooks/__init__.py tests/hooks/test_brief_cost.py
git commit -m "feat(solve-task): форматирование и идемпотентная вставка блока токенов в бриф"
```

---

### Task 2: Разбор транскрипта — окно этапа + агрегация usage

**Files:**
- Modify: `plugin/hooks/brief_cost.py`
- Test: `tests/hooks/test_brief_cost.py`

**Interfaces:**
- Consumes (из Task 1): модуль `brief_cost`.
- Produces:
  - `SKILL_MARKER = "skills/solve-task"`, `BASE_DIR_MARKER = "Base directory for this skill:"`
  - `_message_text(line: dict) -> str` — извлекает текст из `message.content` (str или список блоков `{text}`)
  - `find_window_start(lines: list[dict]) -> int` — индекс **последнего** user-сообщения с обоими маркерами; `-1` если нет
  - `aggregate_usage(lines: list[dict], start_idx: int) -> dict[str, dict[str, int]]` — сумма 4 бакетов по `model` для assistant-ходов после `start_idx`; пропускает `isSidechain`; модели с нулём отбрасывает

- [ ] **Step 1: Написать падающие тесты** (добавить в `tests/hooks/test_brief_cost.py`)

```python
def test_message_text_handles_list_content():
    line = {"message": {"content": [
        {"type": "text", "text": "Base directory for this skill: skills/solve-task"},
    ]}}
    assert "solve-task" in bc._message_text(line)


def test_find_window_start_returns_last_marker():
    lines = [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "user", "message": {
            "content": "Base directory for this skill: /x/plugin/skills/solve-task\n# Solve Task"}},
        {"type": "assistant", "message": {"model": "m", "usage": {"input_tokens": 1}}},
        {"type": "user", "message": {
            "content": "Base directory for this skill: /x/plugin/skills/solve-task (rerun)"}},
    ]
    assert bc.find_window_start(lines) == 3


def test_find_window_start_absent_returns_minus_one():
    lines = [{"type": "user", "message": {"content": "nothing here"}}]
    assert bc.find_window_start(lines) == -1


def test_aggregate_usage_sums_per_model_skips_sidechain():
    lines = [
        {"type": "user", "message": {
            "content": "Base directory for this skill: skills/solve-task"}},
        {"type": "assistant", "message": {"model": "claude-opus-4-8", "usage": {
            "input_tokens": 100, "output_tokens": 200,
            "cache_creation_input_tokens": 300, "cache_read_input_tokens": 400}}},
        {"type": "assistant", "isSidechain": True, "message": {
            "model": "claude-opus-4-8", "usage": {"input_tokens": 999}}},
        {"type": "assistant", "message": {"model": "claude-haiku-4-5", "usage": {
            "input_tokens": 1, "output_tokens": 2,
            "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}}},
    ]
    by_model = bc.aggregate_usage(lines, 0)
    assert by_model["claude-opus-4-8"] == {
        "fresh_in": 100, "output": 200, "cache_write": 300, "cache_read": 400}
    assert by_model["claude-haiku-4-5"] == {
        "fresh_in": 1, "output": 2, "cache_write": 3, "cache_read": 4}
```

- [ ] **Step 2: Запустить новые тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/hooks/test_brief_cost.py -q -k "message_text or window_start or aggregate"`
Expected: FAIL (`AttributeError: module 'brief_cost' has no attribute ...`).

- [ ] **Step 3: Добавить разбор транскрипта в `plugin/hooks/brief_cost.py`**

Вставить после `upsert_block` (перед любым `if __name__`):
```python
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
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/hooks/test_brief_cost.py -q`
Expected: PASS (8 тестов).

- [ ] **Step 5: Линт + commit**

```bash
.venv/bin/ruff check plugin/hooks/brief_cost.py tests/hooks/
git add plugin/hooks/brief_cost.py tests/hooks/test_brief_cost.py
git commit -m "feat(solve-task): разбор транскрипта — окно этапа и агрегация usage по моделям"
```

---

### Task 3: Флаг `.review.yml` + оркестрация `run`/`main` (stdin, path-guard, fail-open)

**Files:**
- Modify: `plugin/hooks/brief_cost.py`
- Test: `tests/hooks/test_brief_cost.py`

**Interfaces:**
- Consumes (Task 1–2): `render_block`, `upsert_block`, `find_window_start`, `aggregate_usage`.
- Produces:
  - `read_flag(text: str | None) -> bool` — `True` только если `solve_task.brief_token_cost is True` (yaml-primary, stdlib-фолбэк)
  - `_under_briefs(path: str) -> bool` — путь под `docs/superpowers/briefs/`
  - `_find_review_yml(cwd: str) -> str | None` — `.review.yml` от `cwd` вверх до корня ФС
  - `_read_text(path) -> str | None`, `_read_jsonl(path) -> list[dict]`, `_write_text(path, text) -> None`
  - `run(payload: dict) -> int` — оркестратор, всегда `0`
  - `main() -> int` — читает stdin JSON, вызывает `run`; всегда `0`

- [ ] **Step 1: Написать падающие тесты** (добавить в `tests/hooks/test_brief_cost.py`)

```python
def test_read_flag_true_only_when_set():
    assert bc.read_flag("solve_task:\n  brief_token_cost: true\n") is True
    assert bc.read_flag("solve_task:\n  brief_token_cost: false\n") is False
    assert bc.read_flag("other: 1\n") is False
    assert bc.read_flag(None) is False


def test_under_briefs_path_guard():
    assert bc._under_briefs("/Users/me/repo/docs/superpowers/briefs/2026-06-27-x.md") is True
    assert bc._under_briefs("/Users/me/repo/docs/superpowers/specs/x.md") is False


def _setup_repo(tmp_path, flag="true", brief_name="x.md", brief_body="# Brief\n"):
    (tmp_path / ".review.yml").write_text(
        f"solve_task:\n  brief_token_cost: {flag}\n", encoding="utf-8")
    briefs = tmp_path / "docs" / "superpowers" / "briefs"
    briefs.mkdir(parents=True)
    brief = briefs / brief_name
    brief.write_text(brief_body, encoding="utf-8")
    return brief


def _write_transcript(tmp_path, rows):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return transcript


_SOLVE_USER = {"type": "user", "message": {
    "content": "Base directory for this skill: skills/solve-task"}}


def test_run_end_to_end_writes_block(tmp_path):
    brief = _setup_repo(tmp_path, brief_body="# Brief — x\n\n## Task\nt\n")
    transcript = _write_transcript(tmp_path, [
        _SOLVE_USER,
        {"type": "assistant", "message": {"model": "claude-opus-4-8", "usage": {
            "input_tokens": 9900, "output_tokens": 164000,
            "cache_creation_input_tokens": 533000, "cache_read_input_tokens": 14200000}}},
    ])
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(brief)},
               "transcript_path": str(transcript), "cwd": str(tmp_path)}
    assert bc.run(payload) == 0
    out = brief.read_text(encoding="utf-8")
    assert "## Токены (этап solve-task)" in out
    assert "Всего: 14.9M токенов" in out


def test_run_noop_when_flag_off(tmp_path):
    brief = _setup_repo(tmp_path, flag="false")
    original = brief.read_text(encoding="utf-8")
    transcript = _write_transcript(tmp_path, [_SOLVE_USER])
    payload = {"tool_input": {"file_path": str(brief)},
               "transcript_path": str(transcript), "cwd": str(tmp_path)}
    assert bc.run(payload) == 0
    assert brief.read_text(encoding="utf-8") == original


def test_run_noop_when_path_not_brief(tmp_path):
    (tmp_path / ".review.yml").write_text(
        "solve_task:\n  brief_token_cost: true\n", encoding="utf-8")
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    spec = specs / "x.md"
    spec.write_text("# Spec\n", encoding="utf-8")
    payload = {"tool_input": {"file_path": str(spec)},
               "transcript_path": "", "cwd": str(tmp_path)}
    assert bc.run(payload) == 0
    assert spec.read_text(encoding="utf-8") == "# Spec\n"


def test_run_noop_when_no_marker(tmp_path):
    brief = _setup_repo(tmp_path)
    transcript = _write_transcript(tmp_path, [
        {"type": "assistant", "message": {"model": "m", "usage": {"input_tokens": 5}}}])
    payload = {"tool_input": {"file_path": str(brief)},
               "transcript_path": str(transcript), "cwd": str(tmp_path)}
    assert bc.run(payload) == 0
    assert brief.read_text(encoding="utf-8") == "# Brief\n"


def test_run_replaces_on_rerun(tmp_path):
    brief = _setup_repo(tmp_path)
    transcript = _write_transcript(tmp_path, [
        _SOLVE_USER,
        {"type": "assistant", "message": {"model": "m", "usage": {"input_tokens": 1000}}}])
    payload = {"tool_input": {"file_path": str(brief)},
               "transcript_path": str(transcript), "cwd": str(tmp_path)}
    bc.run(payload)
    bc.run(payload)
    out = brief.read_text(encoding="utf-8")
    assert out.count("## Токены (этап solve-task)") == 1
```

- [ ] **Step 2: Запустить новые тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/hooks/test_brief_cost.py -q -k "read_flag or briefs or run_"`
Expected: FAIL (`AttributeError: module 'brief_cost' has no attribute 'read_flag'`).

- [ ] **Step 3: Добавить флаг + I/O + оркестрацию в `plugin/hooks/brief_cost.py`**

Добавить импорты в начало файла (после докстринга, до `HEADER`):
```python
import json
import os
import re
import sys
```

Вставить в конец файла (после `aggregate_usage`):
```python
def read_flag(text) -> bool:
    """True только если solve_task.brief_token_cost == true.

    Хук исполняется системным python3, где PyYAML может отсутствовать → yaml,
    если доступен, иначе минимальный stdlib-разбор задокументированного формата.
    """
    if not text:
        return False
    try:
        import yaml
    except ImportError:
        return _read_flag_fallback(text)
    try:
        data = yaml.safe_load(text) or {}
    except Exception:
        return False
    block = data.get("solve_task") if isinstance(data, dict) else None
    return isinstance(block, dict) and block.get("brief_token_cost") is True


def _read_flag_fallback(text: str) -> bool:
    """Stdlib-разбор флага: inline `{...}` или block-style под `solve_task:`."""
    if re.search(r"solve_task:\s*\{[^}]*brief_token_cost:\s*true", text):
        return True
    in_block = False
    for line in text.splitlines():
        if re.match(r"^solve_task:\s*(#.*)?$", line):
            in_block = True
            continue
        if in_block:
            if line and not line[0].isspace():
                in_block = False
                continue
            if re.match(r"^\s+brief_token_cost:\s*true\b", line):
                return True
    return False


def _under_briefs(path: str) -> bool:
    norm = os.path.normpath(path).replace(os.sep, "/")
    return "/docs/superpowers/briefs/" in norm or norm.startswith("docs/superpowers/briefs/")


def _find_review_yml(cwd: str):
    current = os.path.abspath(cwd)
    while True:
        candidate = os.path.join(current, ".review.yml")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _read_jsonl(path) -> list:
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


def _write_text(path, text) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def run(payload: dict) -> int:
    """Оркестрация хука. Всегда возвращает 0 (fail-open)."""
    try:
        if os.environ.get("BRIEF_COST_DEBUG"):
            sys.stderr.write("brief_cost payload keys: " + ",".join(sorted(payload)) + "\n")
        file_path = (payload.get("tool_input") or {}).get("file_path") or ""
        if not _under_briefs(file_path):
            return 0
        # .review.yml ищем вверх от каталога брифа (file_path гарантированно
        # присутствует); cwd — фолбэк, если file_path пуст.
        start_dir = (os.path.dirname(os.path.abspath(file_path)) if file_path
                     else (payload.get("cwd") or os.getcwd()))
        yml_path = _find_review_yml(start_dir)
        if not read_flag(_read_text(yml_path) if yml_path else None):
            return 0
        lines = _read_jsonl(payload.get("transcript_path") or "")
        if not lines:
            return 0
        start = find_window_start(lines)
        if start < 0:
            return 0
        by_model = aggregate_usage(lines, start)
        if not by_model:
            return 0
        brief = _read_text(file_path)
        if brief is None:
            return 0
        _write_text(file_path, upsert_block(brief, render_block(by_model)))
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

- [ ] **Step 4: Запустить ВСЕ тесты хука — убедиться, что проходят**

Run: `.venv/bin/pytest tests/hooks/test_brief_cost.py -q`
Expected: PASS (≈16 тестов).

- [ ] **Step 5: Smoke-тест stdin-контракта вручную**

Run:
```bash
printf '{"tool_input":{"file_path":"/nope/docs/superpowers/specs/x.md"},"transcript_path":"","cwd":"/tmp"}' | python3 plugin/hooks/brief_cost.py; echo "exit=$?"
```
Expected: `exit=0`, никаких изменений на диске (путь не под briefs/).

- [ ] **Step 6: Линт + commit**

```bash
.venv/bin/ruff check plugin/hooks/brief_cost.py tests/hooks/
git add plugin/hooks/brief_cost.py tests/hooks/test_brief_cost.py
git commit -m "feat(solve-task): флаг .review.yml + оркестрация хука (stdin, path-guard, fail-open)"
```

---

### Task 4: Регистрация плагинного хука + флаг в `.review.yml` + guard-тест

**Files:**
- Create: `plugin/hooks/hooks.json`
- Modify: `plugin/.claude-plugin/plugin.json` (привязка хука)
- Modify: `.review.yml` (активный флаг `solve_task.brief_token_cost: true` — dogfood в этом репо)
- Modify: `tests/test_review_yml_example.py` (документирующий guard на новый ключ)
- Test: `tests/hooks/test_brief_cost.py` (guard на форму `hooks.json`)

**Interfaces:**
- Consumes: рабочий `plugin/hooks/brief_cost.py` (Task 3).
- Produces: установленный плагин регистрирует `PostToolUse(Write)` → `brief_cost.py`.

> **Wiring подтверждён `claude-code-guide` (офиц. доки `plugins-reference.md` + `hooks.md`):** плагин грузит хуки и из авто-дискаверного `hooks/hooks.json`, и из поля `"hooks"` в `plugin.json` (берём оба — пояса и подтяжки); схема `hooks.json` и `${CLAUDE_PLUGIN_ROOT}` (в двойных кавычках) — верны; matcher `"Write"` — точное строковое совпадение, регистрозависимо. Скрипт зовём через `python3 "<path>"`, поэтому бит исполняемости (`chmod +x`) не нужен.

- [ ] **Step 1: Написать падающий guard-тест** (добавить в `tests/hooks/test_brief_cost.py`)

```python
def test_hooks_json_registers_posttooluse_write():
    hooks = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    entries = hooks["hooks"]["PostToolUse"]
    write_entries = [e for e in entries if e.get("matcher") == "Write"]
    assert write_entries, "нужен PostToolUse-матчер на Write"
    command = write_entries[0]["hooks"][0]["command"]
    assert "brief_cost.py" in command
    assert "${CLAUDE_PLUGIN_ROOT}" in command
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/hooks/test_brief_cost.py::test_hooks_json_registers_posttooluse_write -q`
Expected: FAIL (`FileNotFoundError` — нет `plugin/hooks/hooks.json`).

- [ ] **Step 3: Создать `plugin/hooks/hooks.json`**

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/brief_cost.py\""
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 4: Привязать хук в `plugin/.claude-plugin/plugin.json`**

Заменить текущее содержимое на (добавлен ключ `hooks` — относительный путь от корня плагина):
```json
{
  "name": "rag-reviewer",
  "version": "0.1.0",
  "description": "Agentic PR review: hybrid RAG + code graph via MCP, review skills for Claude Code",
  "hooks": "./hooks/hooks.json"
}
```

- [ ] **Step 5: Прогнать guard-тест `hooks.json`**

Run: `.venv/bin/pytest tests/hooks/test_brief_cost.py::test_hooks_json_registers_posttooluse_write -q`
Expected: PASS.

- [ ] **Step 6: Добавить флаг в `.review.yml`** (после блока `task_board`, перед `# --- Политика контекст-слоя`)

```yaml
# --- Скилл solve-task ---

# Дописывать в бриф solve-task расход LLM-токенов на этап (4 бакета:
# fresh-in/output/cache-write/cache-read). Считает PostToolUse-хук плагина
# (plugin/hooks/brief_cost.py) детерминированно по транскрипту сессии — ноль
# доп. LLM-токенов, без долларов. Дефолт (без ключа) — выключено.
solve_task:
  brief_token_cost: true
```

- [ ] **Step 7: Документирующий guard на новый ключ** (добавить в `tests/test_review_yml_example.py`, в конец `test_example_review_yml_documents_new_keys`)

```python
    assert "brief_token_cost" in (data.get("solve_task") or {})
```

- [ ] **Step 8: Прогнать релевантные тесты**

Run: `.venv/bin/pytest tests/hooks/ tests/test_review_yml_example.py -q`
Expected: PASS.

- [ ] **Step 9: Полный прогон unit + линт**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: PASS, без новых ошибок линта (учесть заметку памяти: ruff может быть не чист на main — сверять, что новых ошибок не добавили).

- [ ] **Step 10: Commit**

```bash
git add plugin/hooks/hooks.json plugin/.claude-plugin/plugin.json .review.yml tests/test_review_yml_example.py tests/hooks/test_brief_cost.py
git commit -m "feat(solve-task): регистрация PostToolUse-хука токенов + флаг brief_token_cost в .review.yml"
```

---

## Self-Review

**Spec coverage** (спека → задача):
- §3/§4.2 скрипт-хук, 4 бакета, fail-open → Task 1–3.
- §4.1 `hooks.json` (matcher Write, `${CLAUDE_PLUGIN_ROOT}`) → Task 4 Step 3.
- §4.3 флаг `.review.yml` → Task 4 Step 6.
- §5 окно (последний маркер `skills/solve-task`) + агрегация по моделям → Task 2.
- §6 fail-open (path-guard, флаг, транскрипт, read-only) → Task 3 `run`.
- §7 формат блока + идемпотентность → Task 1 (`render_block`/`upsert_block`).
- §8 stdin-контракт (`tool_input.file_path`/`transcript_path`/`cwd`) → Task 3 `run`/`main`.
- §9 тесты (флаг off / маркер / повтор / нет маркера / path-guard / смешанные модели) → Task 1–4 тесты.
- §10 файлы → совпадает (`prices.json` исключён — долларов нет).

**Placeholder scan:** код полон во всех шагах, плейсхолдеров нет. Привязка хука (Task 4) подтверждена `claude-code-guide` по офиц. докам. Единственная вынесенная на эмпирику точка — наличие `transcript_path` в payload (общее поле хуков); проверяется `BRIEF_COST_DEBUG=1` после установки, при отсутствии — fail-open no-op (не плейсхолдер, а отказоустойчивость).

**Type consistency:** ключи бакетов `fresh_in/output/cache_write/cache_read` едины в `render_block` (Task 1), `aggregate_usage` (Task 2), тестах (Task 2–3). `HEADER` един. Сигнатуры `run(payload)`/`main()`/`read_flag(text)` совпадают между задачами и тестами.
