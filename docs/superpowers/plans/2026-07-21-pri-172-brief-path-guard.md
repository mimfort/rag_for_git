# PRI-172 Brief Path Guard Implementation Plan

## Execution correction (final)

Этот раздел отражает финальную реализацию и **заменяет любые конфликтующие указания и snippets в
Task 1-4 ниже**. В частности, sidechain rows не исключаются, а две отдельные hook-команды не
регистрируются: наблюдаемый Claude Code запускает matching handlers параллельно.

### Task 5: Закрыть provenance, evidence и freshness gaps

**Files:**
- Modify: `plugin/hooks/brief_guard.py`
- Modify: `tests/hooks/test_brief_guard.py`

**TDD regressions:**
- Path B использует последний solve-task marker; markerless Path A со всеми `isSidechain=true`
  rows проходит только при exact provenance текущего `Write` по payload
  `agent_id/tool_use_id/tool_name/file_path` и assistant row
  `agentId/attributionSkill=rag-reviewer:solve-task`/tool block. Наблюдаемая attribution schema
  относится к Claude Code 2.1.209; при drift markerless path fail-closed.
- Transcript читается не более трёх раз до появления текущего assistant `Write` tool use;
  timeout даёт no-op, current tool result не требуется.
- Evidence берётся только из linked user tool result для direct/plugin-prefixed
  `search_codebase`, `related_symbols`, `callers`, `implementations`, `definition`; FastMCP string
  `result` разворачивается, а Bash, arbitrary, orphan и near-prefix results отвергаются.
- `[truncated]`, `[...truncated]` и `[…truncated]` дают no-op только в trusted result; cliff/rails
  notes не останавливают guard. Path matching, last-marker semantics и scope двух code/test секций
  остаются прежними.

**Verification command/result:**
- `.venv/bin/pytest -q tests/hooks/test_brief_guard.py` → `48 passed in 0.17s`.

### Task 6: Сериализовать hooks и зафиксировать packaging scope

**Files:**
- Create: `plugin/hooks/brief_post_write.py`
- Create: `tests/hooks/test_brief_post_write.py`
- Modify: `plugin/hooks/hooks.json`
- Modify: `tests/hooks/test_brief_cost.py`
- Packaging/generated updates: `.codex-plugin/plugin.json`,
  `plugin/.codex-plugin/plugin.json`, `uv.lock`

**TDD regressions:**
- `hooks.json` содержит ровно один `PostToolUse/Write` handler для `brief_post_write.py`.
- Wrapper в одном процессе передаёт один payload сначала `brief_cost.run`, затем
  `brief_guard.run`; исключения из cost и guard изолированы, malformed stdin и любой исход
  возвращают 0.
- Изменения plugin manifests/lock отражают упаковку нового plugin content и не добавляют runtime
  semantics guard.

**Verification commands/results:**
- `.venv/bin/pytest -q tests/hooks/test_brief_guard.py tests/hooks/test_brief_post_write.py tests/hooks/test_brief_cost.py`
  → `76 passed in 0.10s`.
- `.venv/bin/ruff check plugin/hooks/brief_guard.py plugin/hooks/brief_post_write.py tests/hooks/test_brief_guard.py tests/hooks/test_brief_post_write.py`
  → `All checks passed!`.

---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Помечать в solve-task brief ссылки на файлы, которые не подтверждены code-retrieval tool results текущего transcript-окна.

**Architecture:** Отдельный stdlib-only `brief_guard.py` парсит Claude Code JSONL, извлекает evidence только из `tool_result` code headers и идемпотентно аннотирует две code/test секции. Тонкая fail-open orchestration атомарно пишет файл; существующий `brief_cost.py` не меняется.

**Tech Stack:** Python 3.11+, stdlib (`json`, `os`, `re`, `tempfile`), Claude Code plugin hooks, pytest, Ruff.

---

## File Structure

- Create: `plugin/hooks/brief_guard.py` — transcript parsing, path matching, brief annotation и fail-open entrypoint.
- Create: `tests/hooks/test_brief_guard.py` — unit/e2e тесты нового hook.
- Modify: `plugin/hooks/hooks.json` — запуск guard после token-cost hook.
- Reference only: `plugin/hooks/brief_cost.py` — паттерн payload/JSONL/atomic write; файл не менять.

### Task 1: Transcript evidence parsing и path matching

> **Obsolete assumption:** snippets ниже, исключающие `isSidechain`, сохранены только как история
> первоначального плана. Финальная семантика задана Task 5 выше.

**Files:**
- Create: `tests/hooks/test_brief_guard.py`
- Create: `plugin/hooks/brief_guard.py`

- [ ] **Step 1: Write failing parser and matching tests**

Создать `tests/hooks/test_brief_guard.py` с динамической загрузкой hook и тестами:

```python
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOK_PATH = ROOT / "plugin" / "hooks" / "brief_guard.py"


def _load():
    spec = importlib.util.spec_from_file_location("brief_guard", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = previous
    return mod


bg = _load()


def test_tool_result_texts_excludes_plain_text_and_sidechain():
    rows = [
        {"type": "user", "message": {"content": [
            {"type": "text", "text": "// fake.py#x (fake.py:1-2)"},
            {"type": "tool_result", "content": "// real.py#f (real.py:1-2)"},
        ]}},
        {"type": "user", "isSidechain": True, "message": {"content": [
            {"type": "tool_result", "content": "// side.py#f (side.py:1-2)"},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": [
                {"type": "text", "text": "pkg/mod.py#f (pkg/mod.py:4)"},
            ]},
        ]}},
    ]
    texts = bg.tool_result_texts(rows, 0)
    assert texts == [
        "// real.py#f (real.py:1-2)",
        "pkg/mod.py#f (pkg/mod.py:4)",
    ]


def test_evidence_paths_require_code_header():
    paths = bg.evidence_paths([
        "// reviewer/index/store.py#ChunkStore (reviewer/index/store.py:10-40)\n"
        "task mentions invented.py:20",
        "tests/test_store.py#test_x (tests/test_store.py:7)",
    ])
    assert paths == {"reviewer/index/store.py", "tests/test_store.py"}


def test_path_matching_is_exact_or_safe_prefixed_suffix():
    observed = {"utils.py", "/tmp/worktree/reviewer/index/store.py", "a/utils.py"}
    assert bg.path_is_observed("utils.py", observed) is True
    assert bg.path_is_observed("reviewer/index/store.py", observed) is True
    assert bg.path_is_observed("store.py", observed) is False
    assert bg.path_is_observed("b/utils.py", observed) is False
```

- [ ] **Step 2: Run tests to verify RED**

Run: `.venv/bin/pytest -q tests/hooks/test_brief_guard.py`

Expected: collection fails because `plugin/hooks/brief_guard.py` does not exist.

- [ ] **Step 3: Implement minimal parsing and matching core**

Создать начало `plugin/hooks/brief_guard.py`:

```python
"""PostToolUse-хук: помечает неподтверждённые path:line ссылки solve-task brief."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile

WARNING = "⚠️ [файл не в результатах поиска]"
SKILL_MARKER = "skills/solve-task"
BASE_DIR_MARKER = "Base directory for this skill:"
CHECKED_SECTIONS = {"## Relevant code", "## Test exemplars"}

_HEADER_RE = re.compile(
    r"(?m)^(?://\s+)?\S*#\S+\s+\(([^()\s]+):\d+(?:-\d+)?\)"
)


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return ""


def tool_result_texts(lines: list, start_idx: int) -> list[str]:
    texts: list[str] = []
    for line in lines[start_idx + 1:]:
        if line.get("isSidechain"):
            continue
        content = (line.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            text = _block_text(block.get("content"))
            if text:
                texts.append(text)
    return texts


def normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def evidence_paths(texts: list[str]) -> set[str]:
    return {
        normalize_path(match.group(1))
        for text in texts
        for match in _HEADER_RE.finditer(text)
    }


def path_is_observed(cited: str, observed: set[str]) -> bool:
    cited = normalize_path(cited)
    if cited in observed:
        return True
    if "/" not in cited:
        return False
    return any(path.endswith("/" + cited) for path in observed)
```

- [ ] **Step 4: Run parser tests to verify GREEN**

Run: `.venv/bin/pytest -q tests/hooks/test_brief_guard.py`

Expected: 3 passed.

- [ ] **Step 5: Checkpoint**

Inspect `git diff -- plugin/hooks/brief_guard.py tests/hooks/test_brief_guard.py`. Do not commit unless the user explicitly requests it.

### Task 2: Scoped and idempotent brief annotation

**Files:**
- Modify: `tests/hooks/test_brief_guard.py`
- Modify: `plugin/hooks/brief_guard.py`

- [ ] **Step 1: Add failing annotation tests**

Append:

```python
def test_guard_brief_marks_only_missing_code_and_test_paths():
    brief = (
        "# Brief\n"
        "## Task\nmissing/task.py:1 stays\n"
        "## Relevant code\n"
        "- pkg/known.py:10 — known\n"
        "- pkg/missing.py:20 — missing\n"
        "## Subsystems\nmissing/subsystem.py:3 stays\n"
        "## Test exemplars\n- tests/test_missing.py:7 — missing\n"
        "## Constraints / open questions\nmissing/constraint.py:4 stays\n"
    )
    guarded, cited, missing = bg.guard_brief(brief, {"pkg/known.py"})
    assert "pkg/known.py:10 ⚠️" not in guarded
    assert f"pkg/missing.py:20 {bg.WARNING}" in guarded
    assert f"tests/test_missing.py:7 {bg.WARNING}" in guarded
    assert "missing/task.py:1 stays" in guarded
    assert "missing/subsystem.py:3 stays" in guarded
    assert "missing/constraint.py:4 stays" in guarded
    assert cited == ["pkg/known.py", "pkg/missing.py", "tests/test_missing.py"]
    assert missing == ["pkg/missing.py", "tests/test_missing.py"]


def test_guard_brief_is_idempotent_and_handles_multiple_citations():
    brief = "## Relevant code\n- a.py:1 calls b.py:2\n"
    once, _, _ = bg.guard_brief(brief, {"a.py"})
    twice, _, _ = bg.guard_brief(once, {"a.py"})
    assert once == twice
    assert once.count(bg.WARNING) == 1
    assert f"b.py:2 {bg.WARNING}" in once
```

- [ ] **Step 2: Run annotation tests to verify RED**

Run: `.venv/bin/pytest -q tests/hooks/test_brief_guard.py -k guard_brief`

Expected: FAIL because `guard_brief` is undefined.

- [ ] **Step 3: Implement scoped annotation**

Add after `path_is_observed`:

```python
_CITATION_RE = re.compile(r"(?<![\w/.-])([\w./+\\-]+\.[A-Za-z0-9]{1,6}):\d+")


def guard_brief(text: str, observed: set[str]) -> tuple[str, list[str], list[str]]:
    active = False
    cited: list[str] = []
    missing: list[str] = []
    out: list[str] = []

    for line in text.splitlines(keepends=True):
        heading = line.strip()
        if heading.startswith("## "):
            active = heading in CHECKED_SECTIONS
            out.append(line)
            continue
        if not active:
            out.append(line)
            continue

        def replace(match: re.Match) -> str:
            path = normalize_path(match.group(1))
            cited.append(path)
            if path_is_observed(path, observed):
                return match.group(0)
            missing.append(path)
            rest = line[match.end():]
            if rest.lstrip().startswith(WARNING):
                return match.group(0)
            return f"{match.group(0)} {WARNING}"

        out.append(_CITATION_RE.sub(replace, line))

    return "".join(out), cited, missing
```

- [ ] **Step 4: Run annotation tests to verify GREEN**

Run: `.venv/bin/pytest -q tests/hooks/test_brief_guard.py -k guard_brief`

Expected: 2 passed.

- [ ] **Step 5: Run all guard tests**

Run: `.venv/bin/pytest -q tests/hooks/test_brief_guard.py`

Expected: 5 passed.

### Task 3: Fail-open hook orchestration

**Files:**
- Modify: `tests/hooks/test_brief_guard.py`
- Modify: `plugin/hooks/brief_guard.py`

- [ ] **Step 1: Add failing end-to-end and no-op tests**

Добавить imports `json` и `os`, затем append:

```python
_SOLVE_USER = {"type": "user", "message": {"content": [
    {"type": "text", "text": "Base directory for this skill: skills/solve-task"},
]}}


def _write_transcript(tmp_path, rows):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _brief(tmp_path, body):
    path = tmp_path / "docs" / "superpowers" / "briefs" / "brief.md"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    return path


def _payload(brief, transcript):
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(brief)},
        "transcript_path": str(transcript),
    }


def test_run_marks_missing_path_and_is_idempotent(tmp_path):
    brief = _brief(tmp_path, "## Relevant code\n- known.py:1\n- missing.py:2\n")
    transcript = _write_transcript(tmp_path, [
        _SOLVE_USER,
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "// known.py#f (known.py:1-3)"},
        ]}},
    ])
    assert bg.run(_payload(brief, transcript)) == 0
    first = brief.read_text(encoding="utf-8")
    assert f"missing.py:2 {bg.WARNING}" in first
    assert bg.run(_payload(brief, transcript)) == 0
    assert brief.read_text(encoding="utf-8") == first


def test_run_noops_without_evidence_or_with_truncated_result(tmp_path):
    body = "## Relevant code\n- missing.py:2\n"
    brief = _brief(tmp_path, body)
    no_evidence = _write_transcript(tmp_path, [_SOLVE_USER])
    assert bg.run(_payload(brief, no_evidence)) == 0
    assert brief.read_text(encoding="utf-8") == body

    truncated = _write_transcript(tmp_path, [
        _SOLVE_USER,
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "// known.py#f (known.py:1)\n[truncated]"},
        ]}},
    ])
    assert bg.run(_payload(brief, truncated)) == 0
    assert brief.read_text(encoding="utf-8") == body


def test_run_noops_outside_briefs_and_without_solve_marker(tmp_path):
    outside = tmp_path / "spec.md"
    outside.write_text("## Relevant code\n- missing.py:2\n", encoding="utf-8")
    transcript = _write_transcript(tmp_path, [{"type": "user", "message": {"content": []}}])
    assert bg.run(_payload(outside, transcript)) == 0
    assert bg.run(_payload(_brief(tmp_path, "# Brief\n"), transcript)) == 0


def test_run_debug_lists_paths(tmp_path, monkeypatch, capsys):
    brief = _brief(tmp_path, "## Relevant code\n- missing.py:2\n")
    transcript = _write_transcript(tmp_path, [
        _SOLVE_USER,
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "// known.py#f (known.py:1)"},
        ]}},
    ])
    monkeypatch.setenv("BRIEF_GUARD_DEBUG", "1")
    bg.run(_payload(brief, transcript))
    err = capsys.readouterr().err
    assert "observed: known.py" in err
    assert "cited: missing.py" in err
    assert "missing: missing.py" in err
```

- [ ] **Step 2: Run orchestration tests to verify RED**

Run: `.venv/bin/pytest -q tests/hooks/test_brief_guard.py -k run`

Expected: FAIL because `run` is undefined.

- [ ] **Step 3: Implement fail-open orchestration and entrypoint**

Append to `brief_guard.py`:

```python
def _message_text(line: dict) -> str:
    content = (line.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return ""


def find_window_start(lines: list) -> int:
    start = -1
    for index, line in enumerate(lines):
        if line.get("type") != "user":
            continue
        text = _message_text(line)
        if BASE_DIR_MARKER in text and SKILL_MARKER in text:
            start = index
    return start


def _under_briefs(path: str) -> bool:
    normalized = os.path.normpath(path).replace(os.sep, "/")
    return (
        "/docs/superpowers/briefs/" in normalized
        or normalized.startswith("docs/superpowers/briefs/")
    )


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
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(json.loads(raw))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


def _write_text(path, text) -> None:
    directory = os.path.dirname(path) or "."
    fd, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _debug(label: str, paths) -> None:
    if os.environ.get("BRIEF_GUARD_DEBUG"):
        sys.stderr.write(f"brief_guard {label}: {', '.join(sorted(set(paths)))}\n")


def run(payload: dict) -> int:
    try:
        file_path = (payload.get("tool_input") or {}).get("file_path") or ""
        if not _under_briefs(file_path):
            return 0
        lines = _read_jsonl(payload.get("transcript_path") or "")
        start = find_window_start(lines)
        if start < 0:
            return 0
        results = tool_result_texts(lines, start)
        if any("[truncated]" in result for result in results):
            return 0
        observed = evidence_paths(results)
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
```

- [ ] **Step 4: Run all guard tests to verify GREEN**

Run: `.venv/bin/pytest -q tests/hooks/test_brief_guard.py`

Expected: 9 passed.

- [ ] **Step 5: Verify malformed JSONL stays fail-open**

В `test_run_marks_missing_path_and_is_idempotent` после `_write_transcript(...)` добавить:

```python
    valid_transcript = transcript.read_text(encoding="utf-8")
    transcript.write_text("not-json\n" + valid_transcript, encoding="utf-8")
```

Это гарантирует, что `_read_jsonl` пропускает битую строку и продолжает использовать валидные
tool results ниже.

Run: `.venv/bin/pytest -q tests/hooks/test_brief_guard.py::test_run_marks_missing_path_and_is_idempotent`

Expected: PASS.

### Task 4: Hook registration and regression verification

> **Obsolete assumption:** две команды в одном matcher не дают требуемого порядка, потому что
> Claude запускает matching handlers параллельно. Финальная регистрация wrapper задана Task 6.

**Files:**
- Modify: `tests/hooks/test_brief_guard.py`
- Modify: `plugin/hooks/hooks.json`

- [ ] **Step 1: Add failing hook registration test**

Append:

```python
def test_hooks_json_runs_cost_then_guard():
    hooks = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    entries = hooks["hooks"]["PostToolUse"]
    write = next(entry for entry in entries if entry.get("matcher") == "Write")
    commands = [hook["command"] for hook in write["hooks"]]
    assert len(commands) == 2
    assert "brief_cost.py" in commands[0]
    assert "brief_guard.py" in commands[1]
    assert all("${CLAUDE_PLUGIN_ROOT}" in command for command in commands)
```

- [ ] **Step 2: Run registration test to verify RED**

Run: `.venv/bin/pytest -q tests/hooks/test_brief_guard.py::test_hooks_json_runs_cost_then_guard`

Expected: FAIL because only `brief_cost.py` is registered.

- [ ] **Step 3: Register the second command**

Изменить `plugin/hooks/hooks.json` hooks array to:

```json
"hooks": [
  {
    "type": "command",
    "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/brief_cost.py\""
  },
  {
    "type": "command",
    "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/brief_guard.py\""
  }
]
```

- [ ] **Step 4: Run focused hook regression**

Run: `.venv/bin/pytest -q tests/hooks/test_brief_guard.py tests/hooks/test_brief_cost.py`

Expected: all tests pass.

- [ ] **Step 5: Run Ruff on changed Python files**

Run: `.venv/bin/ruff check plugin/hooks/brief_guard.py tests/hooks/test_brief_guard.py`

Expected: `All checks passed!`

- [ ] **Step 6: Run the full unit suite**

Run: `.venv/bin/pytest -q`

Expected: all non-integration tests pass.

- [ ] **Step 7: Inspect final scope**

Run: `git diff --check` and `git diff -- plugin/hooks/brief_guard.py plugin/hooks/hooks.json tests/hooks/test_brief_guard.py docs/superpowers/briefs/2026-07-21-PRI-172-solve-task-brief-path-guard.md docs/superpowers/specs/2026-07-21-pri-172-brief-path-guard-design.md docs/superpowers/plans/2026-07-21-pri-172-brief-path-guard.md`

Expected: no whitespace errors; only PRI-172 implementation and artifacts are shown. Do not alter or stage unrelated untracked user files.
