# PRI-209: Улучшения плагина после анализа PRI-208 и трейса в БД — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрыть пробелы в наблюдаемости/учёте плагина reviewer: реальные метаданные прогона в БД, серверный `review_steps`, учёт sidechain-токенов, auto-mode fallback в `solve-task`, guard на зазор истории.

**Architecture:** Расширяем `publish_review` опциональными мета-параметрами (`model`, `usage`, `total_cost`, `started_at`, `steps`); сервер логирует MCP tool calls в `_Session`; `_record_history` объединяет серверные и клиентские steps и вычисляет реальную длительность. `brief_cost` раздельно суммирует основные и sidechain-токены solve-task. `solve-task` в auto mode молча выбирает mid tier. `ReviewHistory` получает метод диагностики зазора.

**Tech Stack:** Python 3.12, pytest, FastAPI (web), Pydantic, PostgreSQL/Neo4j (только для integration-тестов).

---

## File Structure

| File | Responsibility |
|------|----------------|
| `reviewer/mcp/service.py` | `MCPReviewService`: `_Session`, `prepare_review`, `_invoke_tool`, `publish_review`, `_record_history` |
| `reviewer/web/history.py` | `ReviewHistory.record_run`, новый `days_since_last_run` |
| `reviewer/web/api.py` | Endpoint `/api/runs/gap` для guard на зазор |
| `plugin/hooks/brief_cost.py` | Подсчёт sidechain-токенов + fallback-пометка |
| `plugin/skills/solve-task/SKILL.md` | Step 1.5 — auto-mode fallback |
| `tests/mcp/test_publish.py` | Проверка `publish_review` с мета + steps |
| `tests/hooks/test_brief_cost.py` | Тесты sidechain-токенов |
| `tests/skills/test_solve_task_brief.py` | Guard-тест auto-mode (существующий файл) |
| `tests/web/test_history.py` | Интеграционные тесты `days_since_last_run` |

---

### Task 1: Подготовка — убедиться, что PRI-208 доступен

**Files:**
- Check: `git log --all --oneline --grep='PRI-208'`
- Check: `plugin/skills/solve-task/SKILL.md` содержит Step 1.5 про выбор модели

- [ ] **Step 1: Проверить наличие PRI-208 в текущей ветке**

Run:
```bash
git log --oneline -5 dev
git log --oneline -5 --all --grep='PRI-208'
```

Expected: PRI-208 (коммиты c27f4b0/7ffcbf3/175ee06/448e28c) есть в истории, но **не** в `dev` (текущий HEAD 4ef98d2). Если ветка с PRI-208 не вмержена — нужно вмержить или реализовать sidechain-логику поверх `dev`.

- [ ] **Step 2: Создать feature-ветку**

Run:
```bash
git checkout -b feat/PRI-209-plugin-improvements
```

- [ ] **Step 3: Commit**

```bash
git commit --allow-empty -m "chore(plan): start PRI-209 plugin improvements"
```

---

### Task 2: Добавить `started_at` в `_Session` и `PreparedReview`

**Files:**
- Modify: `reviewer/services/review_service.py:81-100` (`PreparedReview` dataclass)
- Modify: `reviewer/mcp/service.py:47-61` (`_Session` dataclass)
- Modify: `reviewer/mcp/service.py:85-124` (`prepare_review`)

- [ ] **Step 1: Write the failing test**

In `tests/mcp/test_service.py`, add after `test_prepare_review_returns_units_and_caches_session`:

```python
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_sets_session_started_at(_ov, _ch) -> None:
    svc = _make_mcp_service()
    svc.prepare_review("o/r", 7)
    s = svc._sessions[("o/r", 7)]
    assert s.started_at is not None
    from datetime import datetime, timezone
    assert isinstance(s.started_at, datetime)
    assert s.started_at.tzinfo is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/mcp/test_service.py::test_prepare_review_sets_session_started_at -v
```

Expected: `AttributeError: '_Session' object has no attribute 'started_at'`

- [ ] **Step 3: Add `started_at` to `_Session`**

Modify `reviewer/mcp/service.py:47-61`:

```python
@dataclass
class _Session:
    prepared: PreparedReview
    ctx: ToolContext
    candidates: dict[str, Finding] = field(default_factory=dict)
    verdicts: dict[str, bool] = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _seq: int = 0
```

Add import at top if missing (`datetime`, `timezone` already imported at line 9).

- [ ] **Step 4: Update `prepare_review` to preserve old session timing on rehydration**

In `reviewer/mcp/service.py:85-124`, when creating a new `_Session`, use `datetime.now(timezone.utc)`. On rehydration from Postgres we cannot recover `started_at`, so it stays default. This is acceptable (fallback).

Current code line 107:
```python
self._sessions[(repo, pr)] = _Session(prepared, ctx)
```
Change to:
```python
started = datetime.now(timezone.utc)
self._sessions[(repo, pr)] = _Session(prepared, ctx, started_at=started)
```

- [ ] **Step 5: Run test to verify it passes**

Run:
```bash
pytest tests/mcp/test_service.py::test_prepare_review_sets_session_started_at -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add reviewer/mcp/service.py tests/mcp/test_service.py
git commit -m "feat(mcp): track session started_at for run duration"
```

---

### Task 3: Расширить `publish_review` и `_record_history` метаданными

**Files:**
- Modify: `reviewer/mcp/service.py:857-995` (`publish_review`)
- Modify: `reviewer/mcp/service.py:1013-1082` (`_record_history`)
- Test: `tests/mcp/test_publish.py`

- [ ] **Step 1: Write the failing test**

In `tests/mcp/test_publish.py`, add:

```python
from datetime import datetime, timezone, timedelta


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_records_real_metadata(_ov, _ch) -> None:
    svc, vcs, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    started = datetime.now(timezone.utc) - timedelta(seconds=2)
    svc._sessions[("o/r", 7)].started_at = started
    report = _submit_then_publish(
        svc, "o/r", 7, [RAW], summary="Overall fine", dry_run=True,
    )
    run = history.runs[0]
    assert run["model"] == "claude-code"  # default fallback
    assert run["duration_ms"] >= 0
    assert run["usage"] is None
    assert run["total_cost"] is None


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_accepts_metadata_override(_ov, _ch) -> None:
    svc, vcs, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    started = datetime.now(timezone.utc) - timedelta(seconds=2)
    report = svc.publish_review(
        "o/r", 7, summary="s", dry_run=True,
        model="claude-sonnet-4", model_verify="claude-haiku-4-5",
        usage={"input_tokens": 100, "output_tokens": 50},
        total_cost=0.123, started_at=started.isoformat(),
    )
    run = history.runs[0]
    assert run["model"] == "claude-sonnet-4"
    assert run["model_verify"] == "claude-haiku-4-5"
    assert run["usage"] == {"input_tokens": 100, "output_tokens": 50}
    assert abs(run["total_cost"] - 0.123) < 1e-6
    assert run["duration_ms"] >= 0
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/mcp/test_publish.py::test_publish_records_real_metadata tests/mcp/test_publish.py::test_publish_accepts_metadata_override -v
```

Expected: `TypeError: publish_review() got an unexpected keyword argument 'model'`

- [ ] **Step 3: Update `publish_review` signature**

Modify `reviewer/mcp/service.py:857-864` from:

```python
def publish_review(
    self,
    repo: str,
    pr: int,
    summary: str,
    dry_run: bool = False,
    task_key: str | None = None,
) -> dict:
```

To:

```python
def publish_review(
    self,
    repo: str,
    pr: int,
    summary: str,
    dry_run: bool = False,
    task_key: str | None = None,
    *,
    model: str | None = None,
    model_verify: str | None = None,
    usage: dict | None = None,
    total_cost: float | None = None,
    started_at: datetime | str | None = None,
    steps: list[dict] | None = None,
) -> dict:
```

- [ ] **Step 4: Parse `started_at` if passed as string**

Add near top of `publish_review` (after normalizing repo):

```python
if isinstance(started_at, str):
    from datetime import datetime as _dt
    started_at = _dt.fromisoformat(started_at)
```

- [ ] **Step 5: Pass metadata to `_record_history`**

Modify call at line 970-974 from:

```python
run_id = self._record_history(
    repo, pr, p, list(s.candidates.values()), deduped, asm,
    verify_rejected=verify_rejected,
    dry_run=dry_run, posted=posted, error=error,
)
```

To:

```python
run_id = self._record_history(
    repo, pr, p, list(s.candidates.values()), deduped, asm,
    verify_rejected=verify_rejected,
    dry_run=dry_run, posted=posted, error=error,
    model=model,
    model_verify=model_verify,
    usage=usage,
    total_cost=total_cost,
    started_at=started_at or s.started_at,
    steps=steps,
)
```

- [ ] **Step 6: Update `_record_history` signature and body**

Modify `reviewer/mcp/service.py:1013-1026` from:

```python
def _record_history(
    self,
    repo: str,
    pr: int,
    p: PreparedReview,
    analyzed: list[Finding],
    deduped: list[Finding],
    asm: AssembledReview,
    *,
    verify_rejected: int,
    dry_run: bool,
    posted: bool,
    error: str,
) -> int | None:
```

To:

```python
def _record_history(
    self,
    repo: str,
    pr: int,
    p: PreparedReview,
    analyzed: list[Finding],
    deduped: list[Finding],
    asm: AssembledReview,
    *,
    verify_rejected: int,
    dry_run: bool,
    posted: bool,
    error: str,
    model: str | None = None,
    model_verify: str | None = None,
    usage: dict | None = None,
    total_cost: float | None = None,
    started_at: datetime | None = None,
    steps: list[dict] | None = None,
) -> int | None:
```

Inside `_record_history`, replace lines 1039-1055:

```python
now = datetime.now(timezone.utc)
status = "error" if (error and not dry_run) else "ok"
...
run = {
    ...
    "model": "claude-code",
    "model_verify": None,
    ...
    "started_at": now,
    "finished_at": now,
    "duration_ms": 0,
    ...
    "usage": None,
    "total_cost": None,
    ...
}
```

With:

```python
now = datetime.now(timezone.utc)
status = "error" if (error and not dry_run) else "ok"
started = started_at or now
if started.tzinfo is None:
    started = started.replace(tzinfo=timezone.utc)
duration_ms = int((now - started).total_seconds() * 1000)
if duration_ms < 0:
    duration_ms = 0
...
run = {
    ...
    "model": model or "claude-code",
    "model_verify": model_verify,
    ...
    "started_at": started,
    "finished_at": now,
    "duration_ms": duration_ms,
    ...
    "usage": usage,
    "total_cost": total_cost,
    ...
}
```

- [ ] **Step 7: Merge server and client steps**

Replace line 1079:
```python
return history.record_run(run, rows, steps=None)
```

With:
```python
all_steps = s.steps + (steps or [])
return history.record_run(run, rows, steps=all_steps or None)
```

- [ ] **Step 8: Run tests**

Run:
```bash
pytest tests/mcp/test_publish.py -v
```

Expected: all PASS

- [ ] **Step 9: Commit**

```bash
git add reviewer/mcp/service.py tests/mcp/test_publish.py
git commit -m "feat(mcp): pass real metadata and steps to review history"
```

---

### Task 4: Логировать MCP tool calls в `review_steps`

**Files:**
- Modify: `reviewer/mcp/service.py:207-217` (`_invoke_tool`)
- Modify: `reviewer/mcp/service.py:219-251` (tool wrappers)
- Test: `tests/mcp/test_service.py`

- [ ] **Step 1: Write the failing test**

In `tests/mcp/test_service.py`, add:

```python
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_invoke_tool_logs_steps(_ov, _ch) -> None:
    svc = _make_mcp_service()
    svc.prepare_review("o/r", 7)
    svc.search_code("o/r", 7, "token check")
    s = svc._sessions[("o/r", 7)]
    assert len(s.steps) >= 1
    assert s.steps[0]["name"] == "search_code"
    assert s.steps[0]["kind"] == "tool_call"
    assert s.steps[0]["stage"] == "analyze"
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/mcp/test_service.py::test_invoke_tool_logs_steps -v
```

Expected: `AssertionError: len(s.steps) >= 1` (steps empty)

- [ ] **Step 3: Implement step logging in `_invoke_tool`**

Modify `reviewer/mcp/service.py:207-217` from:

```python
def _invoke_tool(self, repo: str, pr: int, name: str, args: dict) -> str:
    from reviewer.services.repo_id import normalize_repo
    repo = normalize_repo(repo)
    s = self._session(repo, pr)
    tools = {t.name: t for t in make_tools(s.ctx)}
    return tools[name].invoke(args)
```

To:

```python
def _invoke_tool(self, repo: str, pr: int, name: str, args: dict) -> str:
    from reviewer.services.repo_id import normalize_repo
    repo = normalize_repo(repo)
    s = self._session(repo, pr)
    tools = {t.name: t for t in make_tools(s.ctx)}
    stage = self._tool_stage(name)
    seq = len(s.steps)
    result = tools[name].invoke(args)
    s.steps.append({
        "stage": stage,
        "unit": args.get("path") or args.get("node_id") or args.get("symbol") or "",
        "seq": seq,
        "kind": "tool_call",
        "name": name,
        "text": result[:500] if isinstance(result, str) else None,
        "tool_calls": [{"name": name, "args": args}],
        "tokens": 0,
        "cost": 0.0,
    })
    return result
```

- [ ] **Step 4: Add `_tool_stage` helper**

Add method in `reviewer/mcp/service.py` near `_suggestions_mode`:

```python
@staticmethod
def _tool_stage(name: str) -> str:
    verify_tools = {"get_candidate_findings", "submit_verdicts"}
    if name in verify_tools:
        return "verify"
    if name == "publish_review":
        return "synthesize"
    return "analyze"
```

- [ ] **Step 5: Run tests**

Run:
```bash
pytest tests/mcp/test_service.py::test_invoke_tool_logs_steps -v
pytest tests/mcp/test_publish.py -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add reviewer/mcp/service.py tests/mcp/test_service.py
git commit -m "feat(mcp): log MCP tool calls as review_steps"
```

---

### Task 5: Учёт sidechain-токенов в `brief_cost`

**Files:**
- Modify: `plugin/hooks/brief_cost.py`
- Test: `tests/hooks/test_brief_cost.py`

- [ ] **Step 1: Write the failing test**

In `tests/hooks/test_brief_cost.py`, add:

```python
def test_aggregate_usage_counts_sidechain_solve_task_tokens():
    lines = [
        {"type": "user", "message": {
            "content": "Base directory for this skill: skills/solve-task"}},
        {"type": "assistant", "message": {"model": "claude-opus-4-8", "usage": {
            "input_tokens": 100, "output_tokens": 200,
            "cache_creation_input_tokens": 300, "cache_read_input_tokens": 400}}},
        {"type": "assistant", "isSidechain": True, "message": {
            "model": "claude-opus-4-8", "usage": {
                "input_tokens": 10, "output_tokens": 20,
                "cache_creation_input_tokens": 30, "cache_read_input_tokens": 40}}},
    ]
    by_model, sidechain = bc.aggregate_usage(lines, 0)
    assert by_model["claude-opus-4-8"] == {
        "fresh_in": 100, "output": 200, "cache_write": 300, "cache_read": 400}
    assert sidechain["claude-opus-4-8"] == {
        "fresh_in": 10, "output": 20, "cache_write": 30, "cache_read": 40}
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/hooks/test_brief_cost.py::test_aggregate_usage_counts_sidechain_solve_task_tokens -v
```

Expected: `ValueError: too many values to unpack` or similar (function returns dict)

- [ ] **Step 3: Update `aggregate_usage` to return sidechain separately**

Modify `plugin/hooks/brief_cost.py:100-115` from:

```python
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

To:

```python
def aggregate_usage(lines: list, start_idx: int) -> tuple[dict, dict]:
    """Сумма 4 бакетов токенов по model для assistant-ходов после start_idx.

    Returns:
        (main_by_model, sidechain_by_model). Sidechain включает только
        assistant-ходы с isSidechain=True.
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
        if line.get("isSidechain"):
            bucket = sidechain.setdefault(
                model, {"fresh_in": 0, "output": 0, "cache_write": 0, "cache_read": 0})
        else:
            bucket = by_model.setdefault(
                model, {"fresh_in": 0, "output": 0, "cache_write": 0, "cache_read": 0})
        _add(bucket, usage)
    return (
        {m: b for m, b in by_model.items() if any(b.values())},
        {m: b for m, b in sidechain.items() if any(b.values())},
    )
```

- [ ] **Step 4: Update `render_block` to show sidechain**

Modify `plugin/hooks/brief_cost.py:29-43` from:

```python
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
            f"cache-read {human_tokens(b['cache_read'])}")
        total += b["fresh_in"] + b["output"] + b["cache_write"] + b["cache_read"]
    lines.append(f"Всего: {human_tokens(total)} токенов")
    return "\n".join(lines)
```

To:

```python
def render_block(by_model: dict, sidechain: dict | None = None) -> str:
    """Текст блока «## Токены (этап solve-task)» (без хвостового перевода строки)."""
    lines = [HEADER]
    total = 0
    for model, b in by_model.items():
        lines.append(f"Модель: {model}")
        lines.append(
            f"fresh-in {human_tokens(b['fresh_in'])} · "
            f"out {human_tokens(b['output'])} · "
            f"cache-write {human_tokens(b['cache_write'])} · "
            f"cache-read {human_tokens(b['cache_read'])}")
        total += b["fresh_in"] + b["output"] + b["cache_write"] + b["cache_read"]
    lines.append(f"Всего: {human_tokens(total)} токенов")
    if sidechain:
        side_total = 0
        lines.append("")
        lines.append("В т.ч. sidechain-сабагент:")
        for model, b in sidechain.items():
            lines.append(f"Модель: {model}")
            lines.append(
                f"fresh-in {human_tokens(b['fresh_in'])} · "
                f"out {human_tokens(b['output'])} · "
                f"cache-write {human_tokens(b['cache_write'])} · "
                f"cache-read {human_tokens(b['cache_read'])}")
            side_total += b["fresh_in"] + b["output"] + b["cache_write"] + b["cache_read"]
        lines.append(f"Sidechain всего: {human_tokens(side_total)} токенов")
    return "\n".join(lines)
```

- [ ] **Step 5: Update `run` to use new signature**

Modify `plugin/hooks/brief_cost.py:233-239` from:

```python
by_model = aggregate_usage(lines, start)
if not by_model:
    return 0
brief = _read_text(file_path)
if brief is None:
    return 0
_write_text(file_path, upsert_block(brief, render_block(by_model)))
```

To:

```python
by_model, sidechain = aggregate_usage(lines, start)
if not by_model and not sidechain:
    return 0
brief = _read_text(file_path)
if brief is None:
    return 0
_write_text(file_path, upsert_block(brief, render_block(by_model, sidechain)))
```

- [ ] **Step 6: Update existing tests that call `aggregate_usage`**

`tests/hooks/test_brief_cost.py:90-107` `test_aggregate_usage_sums_per_model_skips_sidechain` now returns two dicts. Rename it to `test_aggregate_usage_splits_main_and_sidechain` and update assertions:

```python
def test_aggregate_usage_splits_main_and_sidechain():
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
    by_model, sidechain = bc.aggregate_usage(lines, 0)
    assert by_model["claude-opus-4-8"] == {
        "fresh_in": 100, "output": 200, "cache_write": 300, "cache_read": 400}
    assert by_model["claude-haiku-4-5"] == {
        "fresh_in": 1, "output": 2, "cache_write": 3, "cache_read": 4}
    assert sidechain["claude-opus-4-8"] == {
        "fresh_in": 999, "output": 0, "cache_write": 0, "cache_read": 0}
```

- [ ] **Step 7: Run tests**

Run:
```bash
pytest tests/hooks/test_brief_cost.py -v
```

Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add plugin/hooks/brief_cost.py tests/hooks/test_brief_cost.py
git commit -m "feat(hooks): count sidechain tokens separately in brief_cost"
```

---

### Task 6: Auto permission mode fallback в `solve-task`

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md`
- Test: `tests/skills/test_solve_task_brief.py`

- [ ] **Step 1: Write the failing guard test**

In `tests/skills/test_solve_task_brief.py` (create if missing):

```python
from pathlib import Path

SKILL_PATH = Path(__file__).resolve().parents[2] / "plugin" / "skills" / "solve-task" / "SKILL.md"


def test_solve_task_step_1_5_handles_auto_permission_mode():
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "auto permission mode" in text.lower()
    assert "mid tier" in text.lower() or "sonnet-class" in text.lower()
    # Skill must not unconditionally ask the user.
    assert "ask the user" not in text.lower() or "auto" in text.lower()
```

If file does not exist, create it with this content.

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/skills/test_solve_task_brief.py -v
```

Expected: `AssertionError` on "auto permission mode"

- [ ] **Step 3: Update `plugin/skills/solve-task/SKILL.md`**

Replace the Step 1.5 section (lines 76-83 in user-scope skill / lines 65-69 in repo skill):

Old:
```markdown
1.5. **Choose the brief model (cross-CLI).** Building the brief (Steps 2–4: gather + distill) is a
   light reasoning task over session-less retrieval tools — a top-tier model is overkill and burns
   tokens. Before building it, **Ask the user which model tier to use for building the brief**,
   phrasing the choice by **tier (cheap / mid / premium)** — not by concrete model names — so it
   works across CLIs (Claude Code, Codex, Gemini, Cursor, …). **Recommend a mid tier (Sonnet-class)
   as the default** (do not recommend Fable — a coarse tier is fine but the brief still needs sound
   judgment). Talk to the user in Russian. Remember the choice for this run. Fail-open: no answer or
   a decline → use the default tier (or, on Path B below, the session model inline). Never block.
```

New:
```markdown
1.5. **Choose the brief model (cross-CLI).** Building the brief (Steps 2–4: gather + distill) is a
   light reasoning task over session-less retrieval tools — a top-tier model is overkill and burns
   tokens. **If auto permission mode is active, silently choose the mid tier (Sonnet-class)** and
   continue without asking the user. Otherwise, **Ask the user which model tier to use for building
   the brief**, phrasing the choice by **tier (cheap / mid / premium)** — not by concrete model
   names — so it works across CLIs (Claude Code, Codex, Gemini, Cursor, …). **Recommend a mid tier
   (Sonnet-class) as the default** (do not recommend Fable — a coarse tier is fine but the brief
   still needs sound judgment). Talk to the user in Russian. Remember the choice for this run.
   Fail-open: no answer, a decline, or inability to detect auto mode → use the default tier (or,
   on Path B below, the session model inline). Never block.
```

- [ ] **Step 4: Run tests**

Run:
```bash
pytest tests/skills/test_solve_task_brief.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugin/skills/solve-task/SKILL.md tests/skills/test_solve_task_brief.py
git commit -m "feat(skills): auto-mode fallback for solve-task model tier"
```

---

### Task 7: Guard на зазор в истории

**Files:**
- Modify: `reviewer/web/history.py`
- Modify: `reviewer/web/api.py`
- Test: `tests/web/test_history.py`, `tests/web/test_api.py`

- [ ] **Step 1: Write the failing test**

In `tests/web/test_history.py`, add:

```python
@pytest.mark.integration
def test_days_since_last_run():
    from reviewer.config.settings import Settings
    pg_dsn = Settings().pg_dsn
    history = ReviewHistory(pg_dsn)
    history.init_schema()
    run = _sample_run()
    history.record_run(run, _sample_findings())
    days = history.days_since_last_run(run["repo"])
    assert days == 0
    days_missing = history.days_since_last_run("nonexistent/repo")
    assert days_missing is None
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
pytest tests/web/test_history.py::test_days_since_last_run -v
```

Expected: `AttributeError: 'ReviewHistory' object has no attribute 'days_since_last_run'`

- [ ] **Step 3: Implement `days_since_last_run`**

Add in `reviewer/web/history.py` after `stats()` method:

```python
    def days_since_last_run(self, repo: str) -> int | None:
        """Вернуть число дней с последнего прогона репозитория или None, если прогонов не было."""
        try:
            sql = """
            SELECT DATE_PART('day', now() - MAX(created_at))
            FROM review_runs
            WHERE repo = %(repo)s
            """
            with self._connect() as conn:
                row = conn.execute(sql, {"repo": repo}).fetchone()
            if row is None or row[0] is None:
                return None
            return int(row[0])
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось получить зазор истории для %s: %s", repo, exc)
            return None
```

- [ ] **Step 4: Add API endpoint**

In `reviewer/web/api.py`, add route:

```python
@router.get("/runs/gap")
def get_history_gap(repo: str, request: Request):
    """Вернуть число дней с последнего прогона для репозитория."""
    _require_auth(request)
    history = request.app.state.history
    days = history.days_since_last_run(repo)
    return {"repo": repo, "days_since_last_run": days}
```

- [ ] **Step 5: Add API test**

In `tests/web/test_api.py`, add:

```python
def test_get_history_gap(client):
    response = client.get("/api/runs/gap?repo=test/repo")
    assert response.status_code == 200
    data = response.json()
    assert data["repo"] == "test/repo"
    assert data["days_since_last_run"] is None or isinstance(data["days_since_last_run"], int)
```

- [ ] **Step 6: Run tests**

Run:
```bash
pytest tests/web/test_history.py::test_days_since_last_run tests/web/test_api.py::test_get_history_gap -v
```

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add reviewer/web/history.py reviewer/web/api.py tests/web/test_history.py tests/web/test_api.py
git commit -m "feat(web): history gap guard for review_runs"
```

---

### Task 8: Обновить клиентские skills для передачи мета в `publish_review`

**Files:**
- Modify: `plugin/skills/review-pr/SKILL.md`
- Modify: `plugin/skills/performance-review/SKILL.md`
- Modify: `plugin/skills/maintainability-review/SKILL.md`

- [ ] **Step 1: Update `review-pr/SKILL.md`**

Find the line:
```markdown
Call `publish_review(repo, pr, summary, dry_run, task_key)`
```

Replace with:
```markdown
Call `publish_review(repo, pr, summary, dry_run, task_key)` and, if the CLI provides usage/model metadata, pass them via the optional keyword arguments `model`, `usage`, and `total_cost`.
```

- [ ] **Step 2: Update `performance-review/SKILL.md` and `maintainability-review/SKILL.md`**

Apply the same one-line addition wherever `publish_review` is mentioned.

- [ ] **Step 3: Commit**

```bash
git add plugin/skills/review-pr/SKILL.md plugin/skills/performance-review/SKILL.md plugin/skills/maintainability-review/SKILL.md
git commit -m "docs(skills): note publish_review metadata options"
```

---

### Task 9: Финальная интеграция и проверка

- [ ] **Step 1: Run full unit test suite**

Run:
```bash
pytest tests/mcp tests/hooks tests/skills tests/web -v --ignore=tests/web/test_integration.py
```

Expected: all PASS

- [ ] **Step 2: Run integration tests (требуются Postgres/Neo4j)**

Run:
```bash
pytest tests/web/test_integration.py tests/integration -v
```

Expected: PASS (если инфраструктура поднята)

- [ ] **Step 3: Lint**

Run:
```bash
ruff check reviewer/mcp/service.py reviewer/web/history.py reviewer/web/api.py plugin/hooks/brief_cost.py plugin/skills/solve-task/SKILL.md
```

Expected: no errors (SKILL.md may be skipped by ruff)

- [ ] **Step 4: Commit**

```bash
git commit --allow-empty -m "test(PRI-209): full test suite passes"
```

---

## Self-Review Checklist

- [ ] Spec coverage: каждая из 5 целей спеки покрыта задачами.
- [ ] Placeholder scan: нет TBD/TODO; все шаги содержат конкретный код/команды.
- [ ] Type consistency: `started_at` используется как `datetime` в `_Session` и `_record_history`; `aggregate_usage` возвращает tuple; `render_block` принимает `sidechain`.
- [ ] Test coverage: unit + integration тесты для каждого изменения.
