# configure-review подсказывает context_limits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Научить скилл `reviewer_configure-review` рекомендовать per-repo блок `context_limits`
(PRI-202) по профилю репозитория, а размер доски для `search_tasks` брать из графа новым read-only
MCP-тулом `count_tasks(project)` с фолбэком на вопрос пользователю.

**Architecture:** Две независимые части. (1) Тонкий вертикальный слайс `count_tasks`:
`TaskGraph.count` (Cypher COUNT) → `TaskService.count_tasks` (fail-soft) → `MCPReviewService.count_tasks`
(обёртка `{"count": int}`) → регистрация FastMCP-тула. (2) Прозаические эвристики в
`plugin/skills/configure-review/SKILL.md`: классификатор профиля из git-скана → бандл-пресет всех
ручек `context_limits`; охраняется guard-тестами. Скилл вызывает `count_tasks` best-effort.

**Tech Stack:** Python 3.11+, Neo4j (`execute_query`), FastMCP (`@mcp.tool()`), pytest, ruff
(line-length 100). Тесты — на фейках, без внешних сервисов.

## Global Constraints

- Язык кода/докстрингов/комментариев — **русский** (стиль проекта). Тело SKILL.md — английское
  (токены), как у остальных скиллов.
- Коммиты: Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`/Claude).
- ruff line-length 100, target py311. Прогонять `.venv/bin/ruff check .` перед коммитом.
- Unit-тесты не дёргают внешние API; `pytest` по умолчанию исключает `-m integration`.
- `context_limits` читается **только** из `.review.yml` (env-слоя нет); отсутствие ключа = дефолт.
- `count_tasks` — read-only, **без похода на доску** (только Neo4j-граф), fail-soft → 0.

---

## File Structure

- `reviewer/tasks/graph.py` — + метод `TaskGraph.count(project="") -> int` (Cypher COUNT по `:Task`).
- `reviewer/tasks/service.py` — + `TaskService.count_tasks(project=None) -> int` (fail-soft поверх графа).
- `reviewer/mcp/service.py` — + `MCPReviewService.count_tasks(project=None) -> dict` (обёртка `{"count"}`).
- `reviewer/entrypoints/mcp_server.py` — + регистрация тула `count_tasks`; бамп числа тулов в докстрингах.
- `plugin/skills/configure-review/SKILL.md` — эвристики `context_limits` (профиль→бандл), best-effort
  `count_tasks` + фолбэк, no-rebuild-нота, scope, frontmatter, ослабление «standalone».
- `tests/tasks/test_graph.py` — + тесты `count` (скоуп, чтение записи, пусто).
- `tests/tasks/test_service.py` — + `_FakeGraph.count` и тесты `count_tasks` (делегат/None/сбой).
- `tests/mcp/test_server.py` — + `"count_tasks"` в эталонное множество тулов; бамп докстринга.
- `tests/mcp/test_server_tools.py` — + тест проброса `project` в `count_tasks`-тул.
- `tests/skills/test_configure_review_skill.py` — обновить standalone-тест; + профили/no-rebuild.
- `tests/test_review_yml_example.py` — + `assert "context_limits" in data`.
- `.review.yml` — блок `context_limits` уже добавлен в рабочем дереве (коммитится в Task 2).

---

## Task 1: Серверный тул `count_tasks(project)` (граф → сервис → MCP → регистрация)

**Files:**
- Modify: `reviewer/tasks/graph.py` (после `list_keys`, ~строка 164)
- Modify: `reviewer/tasks/service.py` (рядом с `get_task_context`, ~строка 261)
- Modify: `reviewer/mcp/service.py` (рядом с `get_task`, ~строка 271)
- Modify: `reviewer/entrypoints/mcp_server.py` (после `get_task`-тула, ~строка 137; докстринги строк 19 и заголовок теста)
- Test: `tests/tasks/test_graph.py`, `tests/tasks/test_service.py`, `tests/mcp/test_server.py`, `tests/mcp/test_server_tools.py`

**Interfaces:**
- Produces:
  - `TaskGraph.count(self, project: str = "") -> int`
  - `TaskService.count_tasks(self, project: str | None = None) -> int`
  - `MCPReviewService.count_tasks(self, project: str | None = None) -> dict` → `{"count": int}`
  - FastMCP-тул `count_tasks(project: str | None = None) -> dict`
- Consumes: существующий `self._driver.execute_query(query, **params) -> (records, summary, keys)`;
  `self._graph` в `TaskService` (None если Neo4j не подключён); `self.components.task_service`.

- [ ] **Step 1: Failing test — `TaskGraph.count` скоуп + чтение записи + пусто**

В `tests/tasks/test_graph.py` добавить (рядом с `test_list_keys_scoped_by_project`):

```python
def test_count_scoped_by_project():
    d = _FakeDriver(records=[{"n": 5}])
    n = TaskGraph(d).count(project="PRI")
    assert n == 5
    query, params = d.calls[0]
    assert params["project"] == "PRI"
    assert "t.project = $project" in query
    assert "count(t)" in query


def test_count_all_when_no_project():
    d = _FakeDriver(records=[{"n": 12}])
    assert TaskGraph(d).count() == 12
    _query, params = d.calls[0]
    assert params["project"] == ""


def test_count_zero_when_empty():
    assert TaskGraph(_FakeDriver(records=[])).count() == 0
```

- [ ] **Step 2: Run — verify RED**

Run: `.venv/bin/pytest tests/tasks/test_graph.py -k count -q`
Expected: FAIL — `AttributeError: 'TaskGraph' object has no attribute 'count'`.

- [ ] **Step 3: Implement `TaskGraph.count`**

В `reviewer/tasks/graph.py` после `list_keys` (перед `delete_tasks`) добавить:

```python
    def count(self, project: str = "") -> int:
        """Число :Task проекта (синкнутые; стабы без project не в счёт). project='' → все.

        Read-only COUNT — не ходит на доску. Единый запрос с OR-скоупом: при project=''
        условие вырождается во «все узлы» (PRI-170)."""
        records, _, _ = self._driver.execute_query(
            "MATCH (t:Task) WHERE ($project = '' OR t.project = $project) "
            "RETURN count(t) AS n",
            project=project)
        return int(records[0]["n"]) if records else 0
```

- [ ] **Step 4: Run — verify GREEN**

Run: `.venv/bin/pytest tests/tasks/test_graph.py -k count -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Failing test — `TaskService.count_tasks` (делегат / None / сбой)**

В `tests/tasks/test_service.py`:
1. В `_FakeGraph.__init__` добавить параметр и трекер (в сигнатуру `def __init__(self, context=None, raise_on=(), pr_keys=(), keys=(), count=0):`):

```python
        self._count = count
        self.count_project = "unset"
```

2. Добавить метод в `_FakeGraph` (рядом с `list_keys`):

```python
    def count(self, project=""):
        if "count" in self._raise_on:
            raise RuntimeError("neo4j down")
        self.count_project = project
        return self._count
```

3. Добавить тесты (в конец файла):

```python
def test_count_tasks_delegates_scoped():
    store, graph, emb = _FakeStore(), _FakeGraph(count=7), _FakeEmbedder()
    assert TaskService(store, graph, emb).count_tasks("PRI") == 7
    assert graph.count_project == "PRI"


def test_count_tasks_zero_when_no_graph():
    store, emb = _FakeStore(), _FakeEmbedder()
    assert TaskService(store, None, emb).count_tasks("PRI") == 0


def test_count_tasks_fail_soft_on_graph_error():
    store, graph, emb = _FakeStore(), _FakeGraph(raise_on=("count",)), _FakeEmbedder()
    assert TaskService(store, graph, emb).count_tasks() == 0
```

- [ ] **Step 6: Run — verify RED**

Run: `.venv/bin/pytest tests/tasks/test_service.py -k count_tasks -q`
Expected: FAIL — `AttributeError: 'TaskService' object has no attribute 'count_tasks'`.

- [ ] **Step 7: Implement `TaskService.count_tasks`**

В `reviewer/tasks/service.py` после `get_task_context` (перед `get_task`) добавить:

```python
    def count_tasks(self, project: str | None = None) -> int:
        """Число проиндексированных :Task проекта (best-effort). Граф None/сбой → 0.

        Read-only: считает узлы графа, не ходит на доску. Источник размера доски для
        рекомендации context_limits.search_tasks в скилле configure-review."""
        if self._graph is None:
            return 0
        try:
            return int(self._graph.count(project or ""))
        except Exception:
            log.warning("count_tasks: сбой графа (project=%s)", project, exc_info=True)
            return 0
```

(Проверить, что `log` уже импортирован в модуле — он используется в `get_task_context`.)

- [ ] **Step 8: Run — verify GREEN**

Run: `.venv/bin/pytest tests/tasks/test_service.py -k count_tasks -q`
Expected: PASS (3 passed).

- [ ] **Step 9: Failing test — MCP-слой: тул зарегистрирован + проброс project**

1. В `tests/mcp/test_server.py::test_server_registers_all_tools` добавить в множество `names ==`
   строку (например после `"get_task",`):

```python
        "count_tasks",
```

2. В `tests/mcp/test_server_tools.py` добавить тест:

```python
def test_count_tasks_tool_forwards_project():
    import asyncio

    svc = _service()
    svc.count_tasks.return_value = {"count": 42}
    server = create_server(svc)
    asyncio.run(server.call_tool("count_tasks", {"project": "PRI"}))
    svc.count_tasks.assert_called_once_with(project="PRI")
```

- [ ] **Step 10: Run — verify RED**

Run: `.venv/bin/pytest tests/mcp/test_server.py::test_server_registers_all_tools tests/mcp/test_server_tools.py::test_count_tasks_tool_forwards_project -q`
Expected: FAIL — множество тулов не содержит `count_tasks` / тул не зарегистрирован.

- [ ] **Step 11: Implement — MCP service + регистрация тула**

1. В `reviewer/mcp/service.py` после `get_task` (перед `board_config`) добавить:

```python
    def count_tasks(self, project: str | None = None) -> dict:
        """Число :Task проекта из графа (best-effort, read-only). Возвращает {"count": int}.

        Источник размера доски для рекомендации context_limits в configure-review;
        граф недоступен/сбой → {"count": 0}, вызывающий фолбэкает на вопрос."""
        return {"count": self.components.task_service.count_tasks(project)}
```

2. В `reviewer/entrypoints/mcp_server.py` после блока тула `get_task` (после строки ~137) добавить:

```python
    @mcp.tool()
    def count_tasks(project: str | None = None) -> dict:
        """Count indexed :Task nodes in the task graph, scoped to a board project
        (code prefix, e.g. PRI); empty/None = all projects. Read-only, no board call.
        Returns {"count": int}; 0 when the graph is unavailable (caller falls back)."""
        return service.count_tasks(project=project)
```

3. Бампнуть числа тулов в докстрингах (косметика — проверяется только множество):
   - `reviewer/entrypoints/mcp_server.py` строка ~19: `с 29 тулами` → `с 30 тулами`.
   - `tests/mcp/test_server.py::test_server_registers_all_tools` докстринг: `ровно 30` → `ровно 31`.

- [ ] **Step 12: Run — verify GREEN**

Run: `.venv/bin/pytest tests/mcp/test_server.py::test_server_registers_all_tools tests/mcp/test_server_tools.py::test_count_tasks_tool_forwards_project -q`
Expected: PASS (2 passed).

- [ ] **Step 13: Lint + весь unit-прогон затронутых модулей**

Run: `.venv/bin/ruff check reviewer/ tests/tasks/ tests/mcp/`
Run: `.venv/bin/pytest tests/tasks/test_graph.py tests/tasks/test_service.py tests/mcp/test_server.py tests/mcp/test_server_tools.py -q`
Expected: ruff clean по затронутым файлам; все тесты PASS.

- [ ] **Step 14: Commit**

```bash
git add reviewer/tasks/graph.py reviewer/tasks/service.py reviewer/mcp/service.py \
        reviewer/entrypoints/mcp_server.py \
        tests/tasks/test_graph.py tests/tasks/test_service.py \
        tests/mcp/test_server.py tests/mcp/test_server_tools.py
git commit -m "feat(tasks): read-only тул count_tasks(project) — размер доски из графа"
```

---

## Task 2: configure-review рекомендует `context_limits` по профилю репо

**Files:**
- Modify: `plugin/skills/configure-review/SKILL.md`
- Modify: `tests/skills/test_configure_review_skill.py`
- Modify: `tests/test_review_yml_example.py`
- Modify: `.review.yml` (коммит уже добавленного блока `context_limits`)

**Interfaces:**
- Consumes: MCP-тул `count_tasks(project)` из Task 1 (скилл вызывает его в шаге 5c, best-effort).
- Produces: (только прозаические гарантии в SKILL.md — проверяются guard-тестами) наличие шага 5c,
  имён профилей `tiny-util`/`standard`/`large / monorepo`, фразы `no rebuild needed`, `count_tasks`,
  `falls back to asking`.

- [ ] **Step 1: Failing tests — guard configure-review**

В `tests/skills/test_configure_review_skill.py`:
1. **Заменить** тест `test_skill_is_standalone_no_mcp` на:

```python
def test_skill_standalone_baseline_with_optional_count_tasks():
    text = SKILL.read_text(encoding="utf-8")
    assert "no reviewer MCP" in text                 # baseline остаётся автономным
    assert "count_tasks" in text                     # ...кроме опционального замера доски
    assert "falls back to asking" in text            # и явного фолбэка на вопрос
```

2. Добавить тесты:

```python
def test_skill_recommends_context_limits_profiles():
    text = SKILL.read_text(encoding="utf-8")
    assert "context_limits" in text
    for profile in ("tiny-util", "standard", "large / monorepo"):
        assert profile in text, f"скилл не описывает профиль {profile}"


def test_skill_context_limits_needs_no_rebuild():
    text = SKILL.read_text(encoding="utf-8")
    assert "no rebuild needed" in text
```

В `tests/test_review_yml_example.py::test_example_review_yml_documents_new_keys` добавить строку:

```python
    assert "context_limits" in data
```

- [ ] **Step 2: Run — verify RED**

Run: `.venv/bin/pytest tests/skills/test_configure_review_skill.py tests/test_review_yml_example.py -q`
Expected: FAIL — новые фразы/ключ ещё не в SKILL.md; `context_limits` уже в `.review.yml` (эта строка
пройдёт), но skills-тесты падают.

- [ ] **Step 3: SKILL.md — ослабить «standalone» (шапка, строки ~11–12)**

Заменить абзац:

```
Standalone: uses only `git` and file editing — **no reviewer MCP / Postgres / Neo4j** — so it works
on a fresh repo before the first index.
```

на:

```
Standalone baseline: uses `git` and file editing — works on a fresh repo before the first index.
The single optional exception is sizing `context_limits.search_tasks`: the skill may call the
reviewer MCP tool `count_tasks(project)` when it is connected; if not (fresh repo / no reviewer MCP /
older deploy / empty graph) it **falls back to asking** the user. Everything else needs
**no reviewer MCP / Postgres / Neo4j**.
```

- [ ] **Step 4: SKILL.md — frontmatter description + Scope**

1. Во frontmatter `description` (строка 3) в скобочный список настраиваемых ключей добавить
   `context_limits (retrieval breadth per repo profile)` — например после
   `ignore for noisy *tracked* paths`:
   `…ignore for noisy *tracked* paths, context_limits retrieval breadth per repo profile) …`.
2. В разделе **Scope** (после буллета `paths.ignore`, перед `task_board`) добавить буллет:

```
- `context_limits` — per-repo retrieval breadth (search_codebase / search_tasks / graph limits,
  PRI-202), recommended from a **repo profile**. Written as a full documented block.
```

- [ ] **Step 5: SKILL.md — новый шаг 5c (после 5b, перед шагом 6)**

Вставить:

````
5c. **`context_limits` — retrieval breadth via a repo profile (PRI-202).** Classify the repo into
   one **profile** from the step-2 structure scan and map it to a full, documented `context_limits`
   block. Write **all** knobs (even when equal to code defaults) — the block is self-documenting,
   matching this repo's own `.review.yml`.

   **Profile from git signals** (no churn — churn drives cluster depth, not retrieval breadth):
   - `N` = number of tracked `.py` files (from step 2).
   - `pkgs` = number of large top-level packages (large ≈ > 50 `.py`; a monorepo signal — several
     independent roots like `services/*`, `packages/*`).

   | Profile | Condition | Meaning |
   |---|---|---|
   | tiny-util | `N < 80` and one package | narrow context, save Voyage |
   | standard (default) | `80 ≤ N ≤ 800` | == code default constants |
   | large / monorepo | `N > 800` OR `pkgs ≥ 3` large | wider rail so broad tasks aren't clipped |

   **Preset bundles** (search_codebase + graph):
   ```
                       floor ceiling ratio abs_floor pool  ann   | hops callers_topk
   tiny-util             3     8     0.60   0.35     20   0.65   |  1        20
   standard (=default)   4    15     0.50   0.30     30   0.65   |  1        25
   large / monorepo      4    25     0.45   0.30     40   0.60   |  1        30
   ```
   Strong signal (scale-driven): `ceiling`, `candidate_pool`, `callers_topk`. Weak signal
   (score-shape): `ratio` / `abs_floor` / `ann_distance_max` — near default, nudged directionally;
   annotate them in the yml «directional, weak — tune after watching the cliff notes».
   `graph.hops` stays 1 in every profile (2 explodes cost).

   **`search_tasks.{floor,ceiling}` from board size** (orthogonal to the repo profile):
   | Board | Condition | floor / ceiling |
   |---|---|---|
   | small | < 150 tasks | 3 / 8 |
   | medium | 150–800 | 3 / 10 |
   | large | 800+ | 4 / 14 |

   Get the count **best-effort**: call `count_tasks(project)` (reviewer MCP; `project` from step 5b).
   Success and `count > 0` → bucket silently. reviewer MCP absent / tool missing (older deploy) /
   `count == 0` (corpus never synced) → **fall back** to asking the user (small / medium / large).
   Never block on it.

   Emit the full block with explanatory comments (mirror the root `.review.yml`). Merge like every
   other key (step 7) — never clobber.
````

- [ ] **Step 6: SKILL.md — шаг 8 no-rebuild нота**

В раздел **8. Suggest rebuild commands** добавить буллет (после существующих про ignore/depth):

```
- `context_limits` changed → **no rebuild needed.** It is read live server-side
  (`_resolve_context_limits`) at review / solve-task time; the effect applies on the next run from
  the branch the `.review.yml` is committed to. Do NOT suggest a reindex/resummarize for it.
```

- [ ] **Step 7: Run — verify GREEN (guard-тесты)**

Run: `.venv/bin/pytest tests/skills/test_configure_review_skill.py tests/test_review_yml_example.py -q`
Expected: PASS.

- [ ] **Step 8: Полный unit-прогон + напоминание про существующие guard-и**

Run: `.venv/bin/pytest tests/skills/ tests/policy/test_context_limits.py -q`
Expected: PASS (в т.ч. неизменённые guard-и скиллов не сломаны — прежний
`test_skill_scope_is_the_four_context_keys` продолжает проходить, т.к. проверяет присутствие ключей,
а не их полноту).

- [ ] **Step 9: Commit (вкл. уже добавленный блок в `.review.yml`)**

```bash
git add plugin/skills/configure-review/SKILL.md \
        tests/skills/test_configure_review_skill.py \
        tests/test_review_yml_example.py \
        .review.yml
git commit -m "feat(configure-review): рекомендация context_limits по профилю репо + размер доски из графа"
```

---

## Self-Review

**1. Spec coverage:**
- Профили-пресеты + классификатор (`N`/`pkgs`) → Task 2, Step 5. ✅
- Полный документированный блок всегда → Task 2, Step 5 («write all knobs»). ✅
- Честность силы сигнала (score-shape weak, hops=1) → Task 2, Step 5. ✅
- `search_tasks` из размера доски best-effort + фолбэк → Task 2, Step 5 (`count_tasks`). ✅
- Серверный тул `count_tasks` (граф→сервис→MCP→регистрация, fail-soft) → Task 1. ✅
- no-rebuild нюанс → Task 2, Step 6. ✅
- Scope/standalone-ослабление/frontmatter → Task 2, Steps 3–4. ✅
- Тесты: graph/service/mcp/skills/review_yml → покрыты в обоих тасках. ✅
- `.review.yml` несёт блок → добавлен ранее, коммитится Task 2 Step 9. ✅

**2. Placeholder scan:** нет TBD/«handle edge cases»/«similar to Task N» — весь код и тексты выписаны.

**3. Type consistency:** `count(project="") -> int` → `count_tasks(project=None) -> int` →
`MCPReviewService.count_tasks(...) -> {"count": int}` → тул возвращает тот же dict. Имена/сигнатуры
согласованы между тасками. `_FakeGraph.count(project="")` совпадает с реальным. Guard-фразы в тестах
(`falls back to asking`, `no rebuild needed`, `tiny-util`, `large / monorepo`, `count_tasks`) дословно
присутствуют в правках SKILL.md.

**Особое внимание при исполнении:**
- `tests/mcp/test_server.py` сверяет **точное множество** тулов — без строки `"count_tasks"` в
  множестве Task 1 упадёт (это и есть RED-шаг). Докстринг-числа не проверяются (косметика).
- Прежний `test_skill_is_standalone_no_mcp` **заменяется** (не дополняется) — иначе конфликт смысла.
