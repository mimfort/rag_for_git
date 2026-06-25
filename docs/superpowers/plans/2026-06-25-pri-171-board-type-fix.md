# PRI-171: board_type через .review.yml + per-board статистика

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Фикс путаницы `type` vs `mcp` в конфиге доски (тип берётся из `.review.yml` репо, а не из env) + per-board breakdown в ответе `sync_board`.

**Architecture:** Три слоя: (1) `SyncService.run()` получает `board_type` и фильтрует провайдеров, возвращает `by_board`; (2) MCP-слой (`MCPReviewService` + `mcp_server.py`) прокидывает `board_type` от вызывающего скилла; (3) скиллы (`sync-tasks`, `solve-task`) читают `task_board.type` из `.review.yml` и явно передают его — LLM больше не угадывает.

**Tech Stack:** Python 3.11+, FastMCP, pydantic-settings, pytest, Markdown (SKILL.md).

## Global Constraints

- Язык комментариев и строк — русский (в соответствии с соглашениями проекта).
- Коммиты без `Co-Authored-By`/упоминаний Claude; стиль — Conventional Commits на русском.
- `board_type=None` → синк всех провайдеров (backward-compat).
- `by_board` — новое поле, агрегаты в корне ответа сохраняются.
- Поле `task_board_type: str = ""` в pydantic **оставить** — `task_board_default()` его молча игнорирует.
- `.venv/bin/pytest -q` — базовый прогон (исключает integration-тесты).

---

### Task 1: SyncService — `board_type` фильтр + `by_board` breakdown

**Files:**
- Modify: `reviewer/tasks/sync.py:81-107`
- Modify: `tests/tasks/test_sync.py` (добавить три теста)

**Interfaces:**
- Produces: `SyncService.run(board=None, board_type=None, limit=None, purge_orphaned=False, keep_with_prs=True) -> dict` — в ответе появляется `by_board: list[dict]`; каждый элемент: `{board_type, board, enumerated, changed, embedded, refreshed, unchanged, failed}`.

- [ ] **Step 1: Написать три падающих теста**

Добавить в конец `tests/tasks/test_sync.py`:

```python
def test_board_type_filters_to_single_provider():
    yougile = FakeProvider([_raw("ID-1", 100)], board_type="yougile")
    youtrack = FakeProvider([_raw("TES-1", 200)], board_type="youtrack")
    ts, meta = FakeTaskService(), FakeMeta()
    result = SyncService([yougile, youtrack], ts, meta).run(board_type="youtrack")
    assert result["enumerated"] == 1
    assert ts.indexed == [["TES-1"]]
    assert len(result["by_board"]) == 1
    assert result["by_board"][0]["board_type"] == "youtrack"
    assert result["by_board"][0]["enumerated"] == 1


def test_board_type_none_syncs_all_providers():
    yougile = FakeProvider([_raw("ID-1", 100)], board_type="yougile")
    youtrack = FakeProvider([_raw("TES-1", 200)], board_type="youtrack")
    ts, meta = FakeTaskService(), FakeMeta()
    result = SyncService([yougile, youtrack], ts, meta).run(board_type=None)
    assert result["enumerated"] == 2
    assert len(result["by_board"]) == 2
    types = {b["board_type"] for b in result["by_board"]}
    assert types == {"yougile", "youtrack"}


def test_by_board_includes_counts_per_provider():
    prov = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)], board_type="yougile")
    meta = FakeMeta({("", "tasks:yougile:*"): "150"})  # ID-1 уже в курсоре
    ts = FakeTaskService()
    result = SyncService([prov], ts, meta).run()
    assert len(result["by_board"]) == 1
    entry = result["by_board"][0]
    assert entry["board_type"] == "yougile"
    assert entry["board"] == "*"
    assert entry["enumerated"] == 2
    assert entry["changed"] == 1
    assert entry["unchanged"] == 1
```

- [ ] **Step 2: Убедиться что тесты падают**

```bash
.venv/bin/pytest tests/tasks/test_sync.py::test_board_type_filters_to_single_provider tests/tasks/test_sync.py::test_board_type_none_syncs_all_providers tests/tasks/test_sync.py::test_by_board_includes_counts_per_provider -v
```

Ожидаем: FAILED — `TypeError: run() got an unexpected keyword argument 'board_type'`.

- [ ] **Step 3: Реализовать изменения в `reviewer/tasks/sync.py`**

Заменить метод `run` (строки 81–107):

```python
    def run(self, board=None, board_type=None, limit=None, purge_orphaned=False,
            keep_with_prs=True) -> dict:
        providers = (
            [p for p in self._providers if p.board_type == board_type]
            if board_type else self._providers
        )
        agg = {"enumerated": 0, "changed": 0, "embedded": 0, "refreshed": 0,
               "unchanged": 0, "failed": 0, "warnings": [], "cursor_advanced": False}
        all_active: list[str] = []
        by_board: list[dict] = []
        for provider in providers:
            active, one = self._sync_provider(provider, board, limit)
            all_active.extend(active)
            for k in ("enumerated", "changed", "embedded", "refreshed",
                      "unchanged", "failed"):
                agg[k] += one[k]
            agg["warnings"].extend(one["warnings"])
            agg["cursor_advanced"] = agg["cursor_advanced"] or one["cursor_advanced"]
            by_board.append({
                "board_type": provider.board_type,
                "board": board or "*",
                **{k: one[k] for k in ("enumerated", "changed", "embedded",
                                        "refreshed", "unchanged", "failed")},
            })

        partial = bool(limit)
        purge_summary = None
        if purge_orphaned and partial:
            agg["warnings"].append("purge пропущен: задан limit (active_keys неполный)")
        elif purge_orphaned:
            # purge по ОБЪЕДИНЕНИЮ ключей всех досок: задачи глобальны, иначе
            # одна доска вычистила бы задачи другой.
            pr = self._tasks.purge_orphaned_tasks(all_active, keep_with_prs=keep_with_prs)
            purge_summary = {"deleted": pr["deleted_store"] + pr["deleted_graph"],
                             "protected": pr["protected_prs"]}
            agg["warnings"].extend(pr.get("warnings") or [])
        agg["purge"] = purge_summary
        agg["by_board"] = by_board
        return agg
```

- [ ] **Step 4: Убедиться что тесты проходят**

```bash
.venv/bin/pytest tests/tasks/test_sync.py -v
```

Ожидаем: все PASSED.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/tasks/sync.py tests/tasks/test_sync.py
git commit -m "feat(tasks): board_type фильтр провайдеров + by_board в ответе SyncService.run()"
```

---

### Task 2: MCPReviewService + MCP-тул — прокинуть `board_type`

**Files:**
- Modify: `reviewer/mcp/service.py:290-309`
- Modify: `reviewer/entrypoints/mcp_server.py:102-112`

**Interfaces:**
- Consumes: `SyncService.run(board_type=...)` из Task 1.
- Produces: `MCPReviewService.sync_board(board, board_type, limit, purge_orphaned, keep_with_prs)` и MCP-тул `sync_board` с параметром `board_type`.

- [ ] **Step 1: Обновить `reviewer/mcp/service.py`**

Заменить метод `sync_board` (строки 290–309):

```python
    def sync_board(self, board: str | None = None, board_type: str | None = None,
                   limit: int | None = None,
                   purge_orphaned: bool = False, keep_with_prs: bool = True) -> dict:
        """Server-side ETL: перечислить доску по REST, нормализовать, проиндексировать.

        Доска/ключ не настроены → понятный error-summary (fail-soft), без падения.
        """
        sync = getattr(self.components, "sync_service", None)
        if sync is None:
            return {"status": "error",
                    "reason": "task board REST not configured — set YOUGILE_API_KEY or "
                              "YOUTRACK_TOKEN + YOUTRACK_BASE_URL in the reviewer-mcp env "
                              "(~/.config/rag-reviewer/.env), then reconnect. Yougile key: "
                              "configurator (Ctrl+~ → API) or POST /api-v2/auth/keys"}
        try:
            return sync.run(board=board, board_type=board_type, limit=limit,
                            purge_orphaned=purge_orphaned,
                            keep_with_prs=keep_with_prs)
        except Exception as e:
            log.warning("sync_board: сбой синка", exc_info=True)
            return {"status": "error", "reason": f"{type(e).__name__}: {e}"}
```

- [ ] **Step 2: Обновить MCP-тул в `reviewer/entrypoints/mcp_server.py`**

Заменить функцию `sync_board` (строки 102–112):

```python
    @mcp.tool()
    def sync_board(board: str | None = None, board_type: str | None = None,
                   limit: int | None = None,
                   purge_orphaned: bool = False, keep_with_prs: bool = True) -> dict:
        """Server-side ETL: enumerate the configured task board via REST, normalize,
        and index it (vector store + task graph). board_type limits sync to one board
        type ("youtrack" or "yougile") — take it from task_board.type in the repo's
        .review.yml, not from the deploy env. board limits to one project/board by
        name. Incremental via a per-board timestamp watermark; --limit disables purge
        and cursor advance. Returns counts summary with by_board breakdown per provider.
        board_type limits the sync to one board type (yougile|youtrack); board limits to one project by code prefix
        (e.g. PRI). Incremental via a per-(type,board) timestamp watermark; --limit
        disables purge and cursor advance. Returns a compact counts summary."""
        return service.sync_board(board, board_type, limit, purge_orphaned, keep_with_prs)
```

- [ ] **Step 3: Проверить прогон тестов MCP**

```bash
.venv/bin/pytest tests/mcp/ -q
```

Ожидаем: все PASSED (изменения backward-compatible).

- [ ] **Step 4: Коммит**

```bash
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py
git commit -m "feat(mcp): прокинуть board_type в sync_board; обновить error message"
```

---

### Task 3: `settings.task_board_default()` — тип из кредов, убрать `TASK_BOARD_TYPE`

**Files:**
- Modify: `reviewer/config/settings.py:131-146`
- Modify: `reviewer/install.py:92-96` (env template) и `install.py:202-207` (wizard)
- Modify: `tests/config/test_settings.py` (обновить/добавить тесты)

**Interfaces:**
- Produces: `task_board_default()` возвращает `type` из `configured_board_types()` (строка если один, список если несколько, ключа нет если ноль).

- [ ] **Step 1: Написать новые тесты и обновить устаревшие**

В `tests/config/test_settings.py`:

1. Удалить / заменить `test_task_board_default_from_env` и `test_task_board_default_partial` (они проверяли старое поведение — тип из `TASK_BOARD_TYPE`).

2. Добавить в конец файла:

```python
def test_task_board_default_type_single_from_creds(monkeypatch):
    for k in ("TASK_BOARD_TYPE", "YOUGILE_API_KEY", "TASK_BOARD_API_KEY",
              "YOUTRACK_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("YOUTRACK_TOKEN", "perm:test")
    monkeypatch.setenv("YOUTRACK_BASE_URL", "https://yt.example.com/api")
    monkeypatch.setenv("TASK_BOARD_MCP", "yougile")
    result = Settings(_env_file=None).task_board_default()
    assert result is not None
    assert result["type"] == "youtrack"


def test_task_board_default_type_list_when_both_creds(monkeypatch):
    monkeypatch.setenv("YOUGILE_API_KEY", "yg-key")
    monkeypatch.setenv("YOUTRACK_TOKEN", "perm:test")
    monkeypatch.setenv("YOUTRACK_BASE_URL", "https://yt.example.com/api")
    result = Settings(_env_file=None).task_board_default()
    assert result is not None
    assert result["type"] == ["yougile", "youtrack"]


def test_task_board_default_type_absent_when_no_creds(monkeypatch):
    for k in ("TASK_BOARD_TYPE", "YOUGILE_API_KEY", "TASK_BOARD_API_KEY",
              "YOUTRACK_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TASK_BOARD_MCP", "yougile")
    result = Settings(_env_file=None).task_board_default()
    assert result is not None
    assert "type" not in result


def test_task_board_default_ignores_task_board_type_env(monkeypatch):
    monkeypatch.setenv("TASK_BOARD_TYPE", "yougile")   # старый env, нет кредов
    for k in ("YOUGILE_API_KEY", "TASK_BOARD_API_KEY", "YOUTRACK_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    result = Settings(_env_file=None).task_board_default()
    # TASK_BOARD_TYPE игнорируется — type не попадает в ответ без кредов
    assert result is None or "type" not in (result or {})
```

- [ ] **Step 2: Убедиться что тесты падают**

```bash
.venv/bin/pytest tests/config/test_settings.py -v -k "type_single or type_list or type_absent or ignores_task_board"
```

Ожидаем: FAILED.

- [ ] **Step 3: Обновить `reviewer/config/settings.py`**

Заменить метод `task_board_default` (строки 131–146):

```python
    def task_board_default(self) -> dict | None:
        """Глобальный конфиг доски из env (фолбэк, когда в .review.yml нет task_board).

        Тип доски выводится из configured_board_types() — по факту наличия REST-кредов,
        а не из TASK_BOARD_TYPE (устарел; поле pydantic сохранено для совместимости).
        Возвращает dict в форме блока ``task_board`` из .review.yml (только непустые ключи)
        или ``None``, если ничего не задано.
        """
        cfg = {}
        types = self.configured_board_types()
        if len(types) == 1:
            cfg["type"] = types[0]
        elif len(types) > 1:
            cfg["type"] = types
        if self.task_board_mcp:
            cfg["mcp"] = self.task_board_mcp
        if self.task_board_key_pattern:
            cfg["key_pattern"] = self.task_board_key_pattern
        if self.task_board_url_template:
            cfg["url_template"] = self.task_board_url_template
        return cfg or None
```

- [ ] **Step 4: Удалить устаревшие тесты из `tests/config/test_settings.py`**

Удалить функции `test_task_board_default_from_env` и `test_task_board_default_partial` (они проверяли, что `TASK_BOARD_TYPE` попадает в ответ — новое поведение этого не делает).

Обновить `test_task_board_default_none_when_unset` — теперь достаточно очищать креды (не `TASK_BOARD_TYPE`):

```python
def test_task_board_default_none_when_unset(monkeypatch):
    for k in ("TASK_BOARD_MCP", "TASK_BOARD_KEY_PATTERN", "TASK_BOARD_URL_TEMPLATE",
              "YOUGILE_API_KEY", "TASK_BOARD_API_KEY", "YOUTRACK_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert Settings(_env_file=None).task_board_default() is None
```

- [ ] **Step 5: Убедиться что тесты проходят**

```bash
.venv/bin/pytest tests/config/test_settings.py -v
```

Ожидаем: все PASSED.

- [ ] **Step 6: Обновить `reviewer/install.py`**

В строковом шаблоне (`_ENV_TEMPLATE`, строки 92–96) заменить:

```python
# --- Доска задач (опционально; per-type креды, legacy-алиас TASK_BOARD_API_KEY/BASE) ---
TASK_BOARD_TYPE=
TASK_BOARD_MCP=
```

на:

```python
# --- Доска задач (опционально; per-type креды, legacy-алиас TASK_BOARD_API_KEY/BASE) ---
# Тип доски (yougile|youtrack) задаётся в .review.yml каждого репо (task_board.type),
# а не в env; TASK_BOARD_TYPE устарел и игнорируется.
TASK_BOARD_MCP=
```

В `WIZARD_GROUPS` удалить поле `TASK_BOARD_TYPE` из группы "Доска задач" (строки 203–207):

```python
# Удалить этот блок целиком:
EnvField(
    key="TASK_BOARD_TYPE",
    prompt_text="TASK_BOARD_TYPE (yougile | jira | ...)",
    default="",
),
```

- [ ] **Step 7: Запустить полный прогон тестов**

```bash
.venv/bin/pytest -q
```

Ожидаем: все PASSED.

- [ ] **Step 8: Коммит**

```bash
git add reviewer/config/settings.py reviewer/install.py tests/config/test_settings.py
git commit -m "feat(config): task_board_default() выводит тип из кредов; убрать TASK_BOARD_TYPE из wizard"
```

---

### Task 4: `sync-tasks` SKILL.md — `board_type` + per-board вывод

**Files:**
- Modify: `plugin/skills/sync-tasks/SKILL.md`
- Modify: `tests/skills/test_sync_tasks_guardrail.py`

**Interfaces:**
- Consumes: `sync_board(board_type=...)` из Task 2.

- [ ] **Step 1: Написать два падающих теста**

Добавить в конец `tests/skills/test_sync_tasks_guardrail.py`:

```python
def test_skill_passes_board_type_from_review_yml():
    text = SKILL.read_text(encoding="utf-8")
    assert "board_type" in text        # скилл передаёт board_type
    assert ".review.yml" in text       # читает из .review.yml


def test_skill_shows_by_board_breakdown():
    text = SKILL.read_text(encoding="utf-8")
    assert "by_board" in text          # обрабатывает per-board breakdown
```

- [ ] **Step 2: Убедиться что тесты падают**

```bash
.venv/bin/pytest tests/skills/test_sync_tasks_guardrail.py -v
```

Ожидаем: FAILED — `AssertionError` (строк нет в тексте).

- [ ] **Step 3: Обновить `plugin/skills/sync-tasks/SKILL.md`**

Заменить весь раздел `## Pipeline` (строки 32–79) на:

```markdown
## Pipeline

1. **Resolve `board_type` from `.review.yml`.** Run `git rev-parse --show-toplevel`
   to find the repo root. Read `<root>/.review.yml` and extract `task_board.type`
   (e.g. `"youtrack"`). Fallback chain:
   - `.review.yml` not found or has no `task_board` block → call `get_board_config()`
     and read `task_board.type` from the deploy default.
   - Still not resolved → use `board_type=null` (syncs all configured boards).

2. **Call the tool once.** Map the parsed arguments to a single call:

   ```
   sync_board(
       board_type=<type from step 1 or null>,
       board=<--board or null>,
       limit=<--limit or null>,
       purge_orphaned=<True if --purge-orphaned else False>,
       keep_with_prs=<False if --no-keep-with-prs else True>,
   )
   ```

   The server enumerates the board over REST (incremental via a per-board timestamp
   watermark — a repeat sync touches ~0 tasks), normalizes every task into a
   `TaskBrief`, indexes changed ones in a single Voyage batch (dedup by
   `content_hash`), auto-links PRs found in descriptions
   (`:Task-[:IMPLEMENTED_BY]->:PR`), and optionally purges orphans.

3. **Print the summary (in Russian).** The tool returns a counts dict with an optional
   `by_board` key. If `by_board` is present, report per-board first, then total:

   ```
   Синк завершён:
     youtrack / PRI: 64 задачи, изменено 2 (эмбеддинги: 0), без изменений 62
   Итого: 64 задачи, изменено 2.
   ```

   If `by_board` is absent (old server): report aggregate counts only —
   «N задач на доске, изменено M (эмбеддинги: K), без изменений U, ошибок F».
   If `purge` is present, add «Purge: D удалено, P защищено (есть PR-история)».
   Surface any `warnings`.

4. **Handle the error case.** If the tool returns `{"status": "error", "reason": ...}`,
   the board is not configured server-side. Tell the user (in Russian) to add the board
   credentials to the reviewer-mcp env file — the canonical `~/.config/rag-reviewer/.env`
   (NOT the repo `./.env`: reviewer-mcp runs with an arbitrary CWD and reads the XDG file
   first) — namely `YOUGILE_API_KEY` (for Yougile) or `YOUTRACK_TOKEN` +
   `YOUTRACK_BASE_URL` (for YouTrack), plus `TASK_BOARD_KEY_PATTERN` /
   `TASK_BOARD_URL_TEMPLATE` for normalization. Then reconnect the MCP server
   (`/mcp` reconnect or restart Claude Code — env is read at process start) and retry.

   For **Yougile**, also explain how to obtain `YOUGILE_API_KEY`:
   - **Configurator (easiest):** in Yougile press `Ctrl + ~` (or Projects → gear ⚙ next to
     the company name → «Настроить») → API settings → generate/copy the key.
   - **REST:** `POST https://yougile.com/api-v2/auth/keys` with `{login, password,
     companyId}` (get `companyId` via `Ctrl + Alt + Q`, or `POST /api-v2/auth/companies`);
     `POST /api-v2/auth/keys/get` lists existing keys.

   Do not attempt to read the board yourself, and never ask the user to paste the key into
   the chat — it belongs in the env file only.
```

Также обновить строку 3 (description в frontmatter):

```markdown
description: Warm the task graph & vector store by indexing a board into the reviewer MCP server. Use when the user asks to sync/index tasks ("sync tasks", "index the board", "просиндексируй задачи") so search_tasks/get_task_context have a corpus. Requires the reviewer MCP server with board credentials configured server-side (YOUGILE_API_KEY or YOUTRACK_TOKEN).
```

- [ ] **Step 4: Убедиться что тесты проходят**

```bash
.venv/bin/pytest tests/skills/test_sync_tasks_guardrail.py -v
```

Ожидаем: все PASSED.

- [ ] **Step 5: Коммит**

```bash
git add plugin/skills/sync-tasks/SKILL.md tests/skills/test_sync_tasks_guardrail.py
git commit -m "feat(skills): sync-tasks читает board_type из .review.yml, показывает by_board"
```

---

### Task 5: `solve-task` SKILL.md — `board_type` в preflight

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md` (только step 0.3)
- Modify: `tests/skills/test_preflight_guardrail.py`

**Interfaces:**
- Consumes: `task_board.type` из `.review.yml` (резолвится в step 1 скилла — уже есть).
- Consumes: `sync_board(board_type=...)` из Task 2.

- [ ] **Step 1: Написать падающий тест**

Добавить в конец `tests/skills/test_preflight_guardrail.py`:

```python
def test_solve_task_preflight_passes_board_type():
    text = SOLVE.read_text(encoding="utf-8")
    # preflight sync_board должен передавать board_type из task_board.type
    assert "board_type" in text
```

- [ ] **Step 2: Убедиться что тест падает**

```bash
.venv/bin/pytest tests/skills/test_preflight_guardrail.py::test_solve_task_preflight_passes_board_type -v
```

Ожидаем: FAILED.

- [ ] **Step 3: Обновить `plugin/skills/solve-task/SKILL.md` — step 0.3**

Найти в файле строку (в разделе Step 0, пункт 3):

```
   3. **Warm the task corpus.** Call `sync_board(board=null, limit=null, purge_orphaned=false)` —
```

Заменить на:

```
   3. **Warm the task corpus.** Call
      `sync_board(board_type=<task_board.type or null>, board=null, limit=null, purge_orphaned=false)` —
      `task_board.type` резолвится из `.review.yml` репо (читается в step 1 — Config).
      `null` если `.review.yml` не задаёт тип (синк всех настроенных провайдеров).
```

- [ ] **Step 4: Убедиться что все guardrail-тесты проходят**

```bash
.venv/bin/pytest tests/skills/ -v
```

Ожидаем: все PASSED.

- [ ] **Step 5: Финальный прогон всех тестов**

```bash
.venv/bin/pytest -q
```

Ожидаем: все PASSED, 0 failed.

- [ ] **Step 6: Коммит**

```bash
git add plugin/skills/solve-task/SKILL.md tests/skills/test_preflight_guardrail.py
git commit -m "feat(skills): solve-task preflight передаёт board_type из .review.yml в sync_board"
```
