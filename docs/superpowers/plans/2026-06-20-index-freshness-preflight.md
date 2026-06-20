# Index Freshness Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать session-less скилам `solve-task` и `ask` способ обнаружить устаревший base-индекс (drift) и сообщить о нём пользователю, опираясь на новый машиночитаемый `reviewer status --json`.

**Architecture:** Часть 1 — общая инфра: чистый рендер `render_status_json(report)` поверх уже существующего `RepoStatus` + флаг `--json` у CLI-команды `status`. Часть 2-3 — её потребители: `solve-task` получает блокирующий Step 0 Preflight (drift → подтверждение → reindex через `/reviewer_sync-codebase` + прогрев корпуса `sync_board`), `ask` — облегчённый warn-баннер раз за сессию. Проверка «бесплатна» по Voyage (читает `index_meta` + локальный git).

**Tech Stack:** Python 3.11+, Click (CLI), pytest (unit на фейках), Markdown (SKILL.md скилы Claude Code).

## Global Constraints

- **Язык user-facing строк — русский** (сообщения о дрейфе, баннер, подсказки). Литералы в коде/SKILL.md, которые видит пользователь, — на русском.
- **Прозу SKILL.md писать по-английски** — тела скилов в этом репо на английском (токен-эффективность); по-русски только литеральные user-facing строки внутри них (как в существующих `ask`/`solve-task`).
- **Коммиты:** Conventional Commits на русском (`feat(...)`, `test(...)`, `docs(...)`), **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude).
- **Ветка разработки — `dev`** (текущая; новые коммиты идут в неё).
- **Финальные гейты:** `ruff check .` без **новых** замечаний (repo-wide clean не гарантирован — проверять изменённые пути); `pytest -q` зелёный (integration по умолчанию исключён).
- **Не-цели:** не трогать ревью-скилы (`review-pr`/`maintainability`/`performance`); не добавлять авто-reindex без подтверждения; не вводить MCP-тул `index_status`.
- Инструменты запускать из venv: `.venv/bin/pytest`, `.venv/bin/ruff`.

---

## File Structure

| Файл | Ответственность | Действие |
|---|---|---|
| `reviewer/services/status.py` | сбор + рендер статуса base-индекса | + `render_status_json(report)` |
| `reviewer/entrypoints/cli.py` | CLI-команды | + флаг `--json` у `status`, ветвление вывода |
| `tests/services/test_status.py` | unit статуса (фейки) | + `test_render_status_json_shapes_payload`, `test_status_command_json` |
| `plugin/skills/solve-task/SKILL.md` | скил подготовки контекста задачи | + Step 0 Preflight |
| `plugin/skills/ask/SKILL.md` | скил Q&A по коду | + warn-only проверка свежести |
| `tests/skills/test_preflight_guardrail.py` | guard-тесты контента скилов | новый файл |

---

## Task 1: `reviewer status --json` (рендер + флаг + unit-тесты)

Общая инфраструктура. Рендер и флаг тесно связаны (флаг без рендера и рендер без флага бессмысленны) — одна задача.

**Files:**
- Modify: `reviewer/services/status.py` (добавить `import json` + функцию `render_status_json`)
- Modify: `reviewer/entrypoints/cli.py:18` (импорт), `reviewer/entrypoints/cli.py:233-255` (команда `status`)
- Test: `tests/services/test_status.py` (добавить 2 теста + `import json`)

**Interfaces:**
- Consumes: существующие `RepoStatus`, `BranchStatus`, `OverlayStatus`, `build_status_report` (`reviewer/services/status.py:15-71`); паттерн CLI-теста `FakeStore`/`FakeGraph`/`CliRunner` + `monkeypatch.setattr(cli_mod, "build_status_report", ...)` (`tests/services/test_status.py:83-93`).
- Produces: `render_status_json(report: RepoStatus) -> str` — pretty-JSON (`{repo, branches:[{branch, ref, indexed_sha, updated_at, chunks, graph_nodes, drift}], overlays:[{ref, chunks}]}`); полный SHA, `updated_at` ISO-строка/`null`, `None`→`null`, `backend` не включается. CLI-флаг `--json`/`as_json: bool`.

- [ ] **Step 1: Написать падающий unit-тест рендера**

В `tests/services/test_status.py` добавить `import json` первой строкой и расширить импорт из `reviewer.services.status` именем `render_status_json`. Текущая строка 4:

```python
from reviewer.services.status import build_status_report, OverlayStatus, render_status, RepoStatus, BranchStatus
```

заменить на:

```python
from reviewer.services.status import build_status_report, OverlayStatus, render_status, render_status_json, RepoStatus, BranchStatus
```

И добавить тест в конец файла:

```python
def test_render_status_json_shapes_payload():
    dt = datetime(2026, 6, 18, 14, 2)
    rep = RepoStatus(
        repo="a/x",
        branches=[
            BranchStatus("main", "base:main", "abc1234567def", dt, 1843, 1207, 0),
            BranchStatus("dev", "base:dev", "def5678901abc", dt, 1850, None, 12),
            BranchStatus("old", "base:old", None, None, 0, None, None),
            BranchStatus("nogit", "base:nogit", "aaa1111222bbb", dt, 10, 5, None),
        ],
        overlays=[OverlayStatus("pr:24", 18)])
    payload = json.loads(render_status_json(rep))
    assert payload["repo"] == "a/x"
    by = {b["branch"]: b for b in payload["branches"]}
    assert by["main"]["drift"] == 0
    assert by["main"]["indexed_sha"] == "abc1234567def"      # полный SHA, не усечён
    assert by["main"]["updated_at"] == "2026-06-18T14:02:00"  # ISO 8601
    assert by["dev"]["drift"] == 12
    assert by["dev"]["graph_nodes"] is None                   # Neo4j недоступен → null
    assert by["old"]["indexed_sha"] is None                   # не проиндексирована
    assert by["old"]["updated_at"] is None
    assert by["nogit"]["drift"] is None                       # дрейф неизвестен
    assert payload["overlays"] == [{"ref": "pr:24", "chunks": 18}]
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/services/test_status.py -q`
Expected: FAIL — `ImportError: cannot import name 'render_status_json' from 'reviewer.services.status'` (ошибка сбора).

- [ ] **Step 3: Реализовать `render_status_json`**

В `reviewer/services/status.py` добавить `import json` в блок импортов (после `from datetime import datetime`):

```python
import json
```

И добавить функцию рядом с `render_status` (например, перед `render_status` или сразу после неё):

```python
def render_status_json(report: RepoStatus) -> str:
    """Машиночитаемый JSON по RepoStatus (для скилов-потребителей).

    Полный SHA (не усечён — потребитель машинный), datetime → ISO 8601,
    None → null. `backend` в JSON не включается: это подсказка только для
    текстового вывода, в список требуемых полей не входит.
    """
    payload = {
        "repo": report.repo,
        "branches": [
            {
                "branch": b.branch,
                "ref": b.ref,
                "indexed_sha": b.indexed_sha,
                "updated_at": b.updated_at.isoformat() if b.updated_at else None,
                "chunks": b.chunks,
                "graph_nodes": b.graph_nodes,
                "drift": b.drift,
            }
            for b in report.branches
        ],
        "overlays": [{"ref": o.ref, "chunks": o.chunks} for o in report.overlays],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
```

- [ ] **Step 4: Прогнать — тест рендера зелёный**

Run: `.venv/bin/pytest tests/services/test_status.py::test_render_status_json_shapes_payload -v`
Expected: PASS.

- [ ] **Step 5: Написать падающий CLI-тест флага `--json`**

В `tests/services/test_status.py` добавить тест (рядом с `test_status_command_smoke`):

```python
def test_status_command_json(monkeypatch):
    dt = datetime(2026, 6, 18, 14, 2)
    rep = RepoStatus(
        repo="a/x",
        branches=[BranchStatus("main", "base:main", "abc1234567def", dt, 5, 3, 0)],
        overlays=[])
    monkeypatch.setattr(cli_mod, "build_status_report", lambda *a, **k: rep)
    res = CliRunner().invoke(cli_mod.cli, ["status", ".", "--repo", "a/x", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["repo"] == "a/x"
    assert payload["branches"][0]["drift"] == 0
    assert payload["branches"][0]["indexed_sha"] == "abc1234567def"
```

- [ ] **Step 6: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/services/test_status.py::test_status_command_json -v`
Expected: FAIL — `res.exit_code` == 2 (Click: `Error: No such option: --json`), ассерт `== 0` не проходит.

- [ ] **Step 7: Добавить флаг `--json` в CLI**

В `reviewer/entrypoints/cli.py` строку 18 импорта:

```python
from reviewer.services.status import build_status_report, render_status
```

заменить на:

```python
from reviewer.services.status import build_status_report, render_status, render_status_json
```

Добавить опцию и параметр сигнатуры. Текущие строки 237-239:

```python
@click.option("--branch", "branch_opt", default=None,
              help="одна ветка; по умолчанию все из REVIEW_BRANCHES")
def status(path: str, repo_tag: str | None, branch_opt: str | None) -> None:
```

заменить на:

```python
@click.option("--branch", "branch_opt", default=None,
              help="одна ветка; по умолчанию все из REVIEW_BRANCHES")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="машиночитаемый JSON вместо текста")
def status(path: str, repo_tag: str | None, branch_opt: str | None,
           as_json: bool) -> None:
```

Текущие строки 253-255 (вычисление `backend` + вывод):

```python
    backend = ("scip-python (точный)" if _shutil.which("scip-python")
               else "tree-sitter (fallback, scip-python не найден)")
    click.echo(render_status(report, backend))
```

заменить на (JSON-ветка раньше, `backend` считается только для текста):

```python
    if as_json:
        click.echo(render_status_json(report))
        return
    backend = ("scip-python (точный)" if _shutil.which("scip-python")
               else "tree-sitter (fallback, scip-python не найден)")
    click.echo(render_status(report, backend))
```

- [ ] **Step 8: Прогнать — CLI-тест зелёный**

Run: `.venv/bin/pytest tests/services/test_status.py -q`
Expected: PASS (все тесты файла, включая старые `test_status_command_smoke`/`test_render_status_shapes_output`).

- [ ] **Step 9: Линт изменённых файлов**

Run: `.venv/bin/ruff check reviewer/services/status.py reviewer/entrypoints/cli.py tests/services/test_status.py`
Expected: без замечаний.

- [ ] **Step 10: Коммит**

```bash
git add reviewer/services/status.py reviewer/entrypoints/cli.py tests/services/test_status.py
git commit -m "feat(status): машиночитаемый reviewer status --json (drift по веткам)"
```

---

## Task 2: `solve-task` Step 0 Preflight

Markdown-правка скила + guard-тест содержимого (TDD: red guard → правка → green).

**Files:**
- Create: `tests/skills/test_preflight_guardrail.py`
- Modify: `plugin/skills/solve-task/SKILL.md` (вставить Step 0 перед пунктом «1. Config», после `## Pipeline`)

**Interfaces:**
- Consumes: `reviewer status --json` из Task 1; MCP-тул `sync_board(board, limit, purge_orphaned, keep_with_prs)` (сигнатура — `plugin/skills/sync-tasks/SKILL.md:33-54`); скил-делегат `/reviewer_sync-codebase`.
- Produces: guard-функция `test_solve_task_has_preflight` в новом файле (Task 3 допишет в него `test_ask_has_warn_only_freshness`).

- [ ] **Step 1: Написать падающий guard-тест для solve-task**

Создать `tests/skills/test_preflight_guardrail.py`:

```python
"""Guardrail: скилы solve-task и ask проверяют свежесть base-индекса (PRI-141).

solve-task — блокирующий Step 0 Preflight (drift → подтверждение → reindex,
+ sync_board прогрев корпуса задач). ask — облегчённый warn-only баннер,
без sync_board/reindex/блокировки.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOLVE = ROOT / "plugin" / "skills" / "solve-task" / "SKILL.md"
ASK = ROOT / "plugin" / "skills" / "ask" / "SKILL.md"


def test_solve_task_has_preflight():
    text = SOLVE.read_text(encoding="utf-8")
    assert "Preflight" in text                              # Step 0 добавлен
    assert "reviewer status" in text and "--json" in text   # читает машиночитаемый статус
    assert "drift" in text                                  # проверяет дрейф
    assert "sync_board(" in text                            # прогрев корпуса задач
    assert "reviewer_sync-codebase" in text                 # делегирование reindex
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_preflight_guardrail.py::test_solve_task_has_preflight -v`
Expected: FAIL — `assert "Preflight" in text` (раздела ещё нет в SKILL.md).

- [ ] **Step 3: Вставить Step 0 в `solve-task/SKILL.md`**

В `plugin/skills/solve-task/SKILL.md` после строки `## Pipeline` (и пустой строки под ней), перед `1. **Config.**`, вставить новый пункт (проза — английская, user-facing строки — русские):

```markdown
0. **Preflight (index freshness + task-corpus warm-up).** Run this BEFORE anything else.
   First resolve, once, the repo path (`git rev-parse --show-toplevel`) and the working branch
   (`git branch --show-current`; if it is in `REVIEW_BRANCHES` use it, else the primary branch) —
   step 3 reuses the same branch for `search_codebase`.

   1. **Base-index freshness.** Run
      `uvx --from rag-reviewer reviewer status <path> --branch <branch> --json` and read `drift`
      for that branch:
      - `drift == 0` → continue;
      - `drift > 0` → tell the user (in Russian) «индекс отстаёт на N коммитов» and **ask for
        confirmation**: reindex now? **Yes** → delegate to `/reviewer_sync-codebase`
        (`--path <path> --ref <branch>`), which reindexes and reports problems, then continue;
        **No** → continue on the stale index and record the gap under **Constraints / open
        questions** in the brief;
      - `drift == null` (no clone / no index record) → do not block; note it in the brief.
   2. **Problem report — in the style of `sync-codebase`.** If `reviewer status` fails (Postgres /
      reviewer MCP / Neo4j unreachable, no index, or `uvx` missing): tell the user (in Russian)
      what is missing and the command to fix it. **Fail-open** — never abort; continue on the
      stale/unknown index.
   3. **Warm the task corpus.** Call `sync_board(board=null, limit=null, purge_orphaned=false)` —
      incremental (timestamp watermark), cheap when the corpus is warm. Board not configured or
      `status=error` → print the `TASK_BOARD_*` hint and continue board-less.

   Decisions: stale → confirmation, never auto (Voyage free tier is 3 RPM / 10K TPM); failures →
   reported like `sync-codebase`; `sync_board` runs incrementally at start.

```

(Существующие пункты 1-5 остаются как есть — нумерация 0..5 читается естественно, перенумеровывать не нужно.)

- [ ] **Step 4: Прогнать — guard-тест зелёный**

Run: `.venv/bin/pytest tests/skills/test_preflight_guardrail.py::test_solve_task_has_preflight -v`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add plugin/skills/solve-task/SKILL.md tests/skills/test_preflight_guardrail.py
git commit -m "feat(solve-task): Step 0 preflight — проверка свежести индекса + прогрев корпуса"
```

---

## Task 3: `ask` warn-only проверка свежести

**Files:**
- Modify: `plugin/skills/ask/SKILL.md` (вставить freshness-check между шагом 1 «Resolve repo/branch» и шагом 2 «Search»)
- Modify: `tests/skills/test_preflight_guardrail.py` (дописать `test_ask_has_warn_only_freshness`)

**Interfaces:**
- Consumes: `reviewer status --json` из Task 1; guard-файл из Task 2.
- Produces: `test_ask_has_warn_only_freshness` — проверяет наличие warn-баннера и **отсутствие** вызова `sync_board(` (облегчённый режим).

- [ ] **Step 1: Дописать падающий guard-тест для ask**

В конец `tests/skills/test_preflight_guardrail.py` добавить:

```python
def test_ask_has_warn_only_freshness():
    text = ASK.read_text(encoding="utf-8")
    assert "--json" in text and "reviewer status" in text   # читает машиночитаемый статус
    assert "отстаёт на" in text                             # warn-баннер про дрейф (рус.)
    assert "reviewer_sync-codebase" in text                 # баннер указывает на reindex-скил
    # облегчённый режим: НЕ зовёт sync_board и не реиндексирует
    assert "sync_board(" not in text
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_preflight_guardrail.py::test_ask_has_warn_only_freshness -v`
Expected: FAIL — `assert "отстаёт на" in text` (баннера ещё нет в `ask/SKILL.md`).

- [ ] **Step 3: Вставить freshness-check в `ask/SKILL.md`**

В `plugin/skills/ask/SKILL.md`, между концом пункта `1. **Resolve repo/branch.**` (после строки про `branch` и DEFAULT_REPO) и началом пункта `2. **Search.**`, вставить (проза — английская, баннер — русский):

```markdown
   **Freshness check (first code question of the session only).** After resolving repo/branch and
   ONLY on the first code question in this conversation — rely on conversation memory: if you have
   already checked index freshness earlier in this session, skip this — run
   `uvx --from rag-reviewer reviewer status <path> --branch <branch> --json` and read `drift`. If
   `drift > 0`, emit exactly **one banner line**, in Russian:
   «⚠ индекс отстаёт на N коммитов, ответ может не учитывать свежие изменения → `/reviewer_sync-codebase`».
   Do NOT block, reindex, ask for confirmation, or call `sync_board` — this is warn-only. Cost ≈ 0
   Voyage (reads `index_meta` + local git). **Fail-open:** any error → skip the banner silently
   (Q&A is latency-sensitive).

```

- [ ] **Step 4: Прогнать — оба guard-теста зелёные**

Run: `.venv/bin/pytest tests/skills/test_preflight_guardrail.py -q`
Expected: PASS (2 теста: solve-task + ask).

- [ ] **Step 5: Коммит**

```bash
git add plugin/skills/ask/SKILL.md tests/skills/test_preflight_guardrail.py
git commit -m "feat(ask): warn-only баннер устаревшего индекса (раз за сессию)"
```

---

## Task 4: Финальный гейт (линт + полный прогон)

Сводная проверка по всем изменениям. Коммит только если что-то пришлось чинить.

**Files:** —

- [ ] **Step 1: Линт изменённых путей**

Run: `.venv/bin/ruff check reviewer/services/status.py reviewer/entrypoints/cli.py tests/services/test_status.py tests/skills/test_preflight_guardrail.py`
Expected: без замечаний. (Repo-wide `ruff check .` может показывать предсуществующие замечания — критерий «без новых».)

- [ ] **Step 2: Полный прогон unit-тестов**

Run: `.venv/bin/pytest -q`
Expected: PASS, без падений (integration по умолчанию исключён).

- [ ] **Step 3: При необходимости — коммит правок**

Если шаги 1-2 потребовали исправлений:

```bash
git add -A
git commit -m "fix: устранить замечания линта/тестов preflight-свежести"
```

Если правок не было — задача завершена без дополнительного коммита.

---

## Self-Review

**1. Spec coverage** (по разделам `2026-06-20-index-freshness-preflight-design.md`):
- Часть 1 (`render_status_json` + `--json`) → Task 1 ✓
- Часть 2 (solve-task Step 0: drift/подтверждение/делегирование/sync_board/fail-open) → Task 2 ✓
- Часть 3 (ask warn-only, раз за сессию, без sync_board) → Task 3 ✓
- Тесты (unit рендера + CLI-smoke + guard оба скила) → Task 1 (unit) + Task 2/3 (guard) ✓
- Критерии приёмки (ruff/pytest) → Task 4 ✓

**2. Placeholder scan:** код показан полностью в каждом шаге; «N коммитов» — литерал баннера (рантайм-подстановка скилом), не плейсхолдер плана.

**3. Type consistency:** `render_status_json(report: RepoStatus) -> str` — одно имя во всех задачах; флаг `--json`→`as_json: bool`; guard-токены (`"Preflight"`, `"sync_board("`, `"отстаёт на"`, `"reviewer_sync-codebase"`, `"--json"`) совпадают с литералами, вставляемыми в SKILL.md (различение `sync_board(` с скобкой: присутствует в solve-task, отсутствует в ask, где употреблён только bare `sync_board`).
