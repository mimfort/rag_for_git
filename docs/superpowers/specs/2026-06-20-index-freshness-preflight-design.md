# Проверка свежести base-индекса в session-less скилах (PRI-141 / ID-141)

**Дата:** 2026-06-20
**Задача:** [PRI-141](https://ru.yougile.com/team/686c049c8af8/#PRI-141) — «solve-task (preflight) + ask (warn-only): проверка свежести индекса»
**Статус:** дизайн утверждён, готов к плану реализации

## Проблема

Session-less скилы собирают контекст на **base-индексе**, но не проверяют его свежесть → молча
работают по устаревшему коду. Затронуты ровно два скила:

- `solve-task` — `search_codebase` на base, без проверки;
- `ask` — `search_codebase` на base, без проверки.

Ревью-скилы (`review-pr`, `maintainability-review`, `performance-review`) **уже прикрыты**: идут
через `prepare_review`, где встроен досинк (сверка `indexed_sha` vs `base_sha` + досинк изменённых
файлов и графа через GitHub compare API). Их **не трогаем**.

## Цель

Дать обоим скилам способ узнать дрейф base-индекса относительно git HEAD и сообщить о нём
пользователю — с разной строгостью под разный профиль скила:

- `solve-task` (подготовка к разработке, латентность терпима) — **блокирующий preflight с
  подтверждением** и опциональной переиндексацией;
- `ask` (Q&A, латентно-чувствителен) — **необязательный warn-баннер**, раз за сессию.

## Не-цели

- Авто-переиндексация без подтверждения (в `solve-task`); любой reindex/блокировка в `ask`.
- Трогать ревью-скилы (`review-pr`/`maintainability`/`performance`) — у них досинк уже встроен в
  `prepare_review`.
- Отдельный MCP-тул `index_status` — переиспользуем CLI, т.к. reindex всё равно идёт через
  `uvx … reviewer index`.

## Архитектура

Три части. Часть 1 — общая инфраструктура (машиночитаемый статус), части 2-3 — её потребители
(правки двух SKILL.md).

```
reviewer status --json  ──┬──▶  solve-task Step 0 (drift → подтверждение → reindex)
   (общая инфра)          └──▶  ask Step 0      (drift → warn-баннер, раз за сессию)
```

Данные о свежести **уже** полностью собраны в `reviewer/services/status.py`
(`build_status_report` → `RepoStatus`): `drift`, `chunks`, `graph_nodes`, `indexed_sha`,
`updated_at`, `ref` по веткам + overlays. Нужен только машиночитаемый рендер — поэтому
session-less проверка остаётся «бесплатной» по Voyage (читает `index_meta` + локальный git).

## Часть 1 — `reviewer status --json` (общая инфра)

### `reviewer/services/status.py`

Добавить чистую функцию-рендер рядом с `render_status`:

```python
def render_status_json(report: RepoStatus) -> str:
    """Машиночитаемый JSON по RepoStatus (для скилов-потребителей)."""
```

Форма вывода:

```json
{
  "repo": "owner/name",
  "branches": [
    {
      "branch": "dev",
      "ref": "base:dev",
      "indexed_sha": "def5678901abc...",
      "updated_at": "2026-06-18T14:02:00",
      "chunks": 1850,
      "graph_nodes": 1190,
      "drift": 12
    }
  ],
  "overlays": [
    {"ref": "pr:24", "chunks": 18}
  ]
}
```

Правила сериализации:

- `indexed_sha` — **полный** SHA (не усечён до 7 символов — потребитель машинный), `null` если
  ветка не проиндексирована.
- `updated_at` — ISO 8601 (`datetime.isoformat()`) либо `null`.
- `graph_nodes` — `null` при недоступном Neo4j.
- `drift` — `null` при неизвестном дрейфе (нет клона / нет записи индекса), целое ≥ 0 иначе.
- `backend` (человеко-метка «scip/tree-sitter») в JSON **не включается** — это не данные, а
  подсказка для текстового вывода; список требуемых полей задачи его не содержит.
- `json.dumps(payload, ensure_ascii=False, indent=2)`.

Решение: выделенная функция (а не `dataclasses.asdict` + `default=str`) — явный контроль формы,
детерминированная сериализация `datetime`, развязка JSON-контракта с именами/порядком полей
датакласса.

### `reviewer/entrypoints/cli.py` (команда `status`, ~233-255)

- Добавить флаг: `@click.option("--json", "as_json", is_flag=True, default=False, help="машиночитаемый JSON вместо текста")`.
- В конце команды — ветвление вывода:

  ```python
  if as_json:
      click.echo(render_status_json(report))
  else:
      click.echo(render_status(report, backend))
  ```

- `build_status_report`, резолв repo/branches и обработка `psycopg.OperationalError` не меняются.
  `backend` вычисляется как сейчас (вызов `_shutil.which`) и используется только в текстовой ветке;
  JSON-ветка его игнорирует.

## Часть 2 — `solve-task` Step 0 Preflight

Файл: `plugin/skills/solve-task/SKILL.md`. Новый **Step 0 — Preflight**, перед текущим шагом 1
(Config). Step 0 определяет путь репо (`git rev-parse --show-toplevel`) и ветку
(`git branch --show-current`, сверка с `REVIEW_BRANCHES`; если ветка вне списка — первичная)
**один раз**; шаг 3 (Gather context) переиспользует ту же ветку для `search_codebase`.

1. **Свежесть base-индекса.**
   `uvx --from rag-reviewer reviewer status <path> --branch <b> --json` → распарсить `drift` по
   целевой ветке:
   - `drift == 0` → идём дальше;
   - `drift > 0` → показать «индекс отстаёт на N коммитов» и **спросить подтверждение**:
     переиндексировать сейчас?
     - **Да** → делегировать в `/reviewer_sync-codebase` (`--path <path> --ref <branch>`); он
       переиндексирует и отрепортит проблемы; затем продолжить;
     - **Нет** → продолжить на устаревшем индексе, отметив gap в разделе «Constraints / open
       questions» итогового brief'а;
   - `drift` неизвестен (`null` — нет клона/записи) → не блокируем, помечаем.

2. **Отчёт о проблемах — в стиле `sync-codebase`.** Если `reviewer status` падает (Postgres / MCP /
   Neo4j недоступны, нет индекса, `uvx` отсутствует): сообщить чего не хватает + команду починки.
   **Fail-open** — скил не падает, продолжает (на устаревшем/неизвестном индексе, board-less при
   необходимости).

3. **Прогрев корпуса задач.** `sync_board(board=null, limit=null, purge_orphaned=false)` —
   инкрементальный (timestamp-watermark), дёшев при заполненном корпусе. Доска не сконфигурирована
   или `status=error` → подсказка по `TASK_BOARD_*`, но продолжаем board-less.

Решения: stale → подтверждение (не авто — бережём Voyage 3 RPM / 10K TPM); проблемы → как в
`sync-codebase`; `sync_board` инкрементально на старте.

После Step 0 существующий пайплайн (Config → Identify → Gather → Distill → Handoff) идёт без
изменений.

## Часть 3 — `ask` warn-only

Файл: `plugin/skills/ask/SKILL.md`. Лёгкая проверка свежести между шагами 1 (Resolve repo/branch) и
2 (Search). Поведение **облегчённое** (не как в `solve-task`):

- Проверка **только на первом** code-вопросе в сессии. Опора на **память разговора**: если в этом
  разговоре свежесть уже проверялась — пропустить (скилы не хранят состояние между вызовами; цена
  «один спавн на сессию» обеспечивается инструкцией скила, а не персистентным маркером).
- Иначе: `uvx --from rag-reviewer reviewer status <path> --branch <b> --json`, прочитать `drift`.
  При `drift > 0` — **одна строка-баннер**:
  «⚠ индекс отстаёт на N коммитов, ответ может не учитывать свежие изменения →
  `/reviewer_sync-codebase`».
- **Без** блокировки, авто-reindex, подтверждения и `sync_board` (задачи к `ask` не относятся).
- Стоимость ≈ 0 по Voyage (читает `index_meta` + локальный git); единственная цена — один спавн
  `reviewer status` на сессию.
- **Fail-open:** любая ошибка проверки → молча без баннера (Q&A латентно-чувствителен, проверка
  свежести его не должна задерживать/ломать).

## Тестирование

### Unit (фейки, без сети) — `tests/services/test_status.py`

- `test_render_status_json` — построить `RepoStatus` с ветками всех видов (fresh `drift=0`, behind
  `drift>0`, not-indexed `indexed_sha=None`, neo4j-down `graph_nodes=None`, no-git `drift=None`) +
  overlay; `json.loads(render_status_json(rep))`; проверить: `drift` по веткам, `null`-ы для
  None-полей, `updated_at` в ISO, **полный** (не усечённый) SHA, overlays.
- `test_status_command_json` — CLI-smoke через `CliRunner().invoke(cli, ["status", ".", "--repo",
  "a/x", "--json"])` (с `monkeypatch` `build_status_report`, как `test_status_command_smoke`):
  `exit_code == 0`, `json.loads(res.output)` парсится.

### Guard-тесты контента скилов — `tests/skills/test_preflight_guardrail.py` (новый)

Паттерн `tests/skills/test_sync_tasks_guardrail.py` (читать `SKILL.md`, ассертить подстроки):

- `solve-task/SKILL.md` содержит: маркер Step 0 / Preflight, `reviewer status`, `--json`,
  `sync_board(`, делегирование в `reviewer_sync-codebase`.
- `ask/SKILL.md` содержит: warn-баннер про дрейф и `--json`; **не** содержит блокировки/`sync_board`
  (страховка от копипасты строгого поведения solve-task в ask).

### Прогон

`ruff check .` без новых замечаний; `pytest -q` зелёный (integration по умолчанию исключён).

## Порядок реализации (TDD)

1. **Часть 1** — `render_status_json` + флаг `--json` в CLI; сразу unit-тесты (`test_render_status_json`,
   `test_status_command_json`).
2. **Часть 2** — Step 0 в `solve-task/SKILL.md`.
3. **Часть 3** — warn-only в `ask/SKILL.md`.
4. **Тесты** — guard-тесты `tests/skills/test_preflight_guardrail.py`.
5. `ruff check .` + `pytest -q`.

## Критерии приёмки (из задачи)

- [ ] `reviewer status --json` отдаёт валидный JSON с `drift` по веткам; покрыт unit-тестом на фейках.
- [ ] `solve-task` Step 0: при `drift > 0` показывает дрейф и спрашивает подтверждение; «Да» →
  reindex через `sync-codebase`; недоступность сервисов → отчёт в стиле `sync-codebase`, fail-open.
- [ ] `solve-task`: `sync_board` на старте; ненастроенная доска → подсказка + продолжение board-less.
- [ ] `ask`: на первом code-вопросе сессии при `drift > 0` выводит warn-баннер; не блокирует, не
  реиндексирует, не зовёт `sync_board`; на последующих вопросах не перепроверяет.
- [ ] Язык кода/сообщений — русский; `ruff check .` без новых замечаний; `pytest -q` зелёный.

## Затронутые файлы

| Файл | Изменение |
|---|---|
| `reviewer/services/status.py` | + `render_status_json(report)` |
| `reviewer/entrypoints/cli.py` | + флаг `--json` у команды `status`, ветвление вывода |
| `plugin/skills/solve-task/SKILL.md` | + Step 0 Preflight |
| `plugin/skills/ask/SKILL.md` | + warn-only проверка свежести |
| `tests/services/test_status.py` | + `test_render_status_json`, `test_status_command_json` |
| `tests/skills/test_preflight_guardrail.py` | новый — guard-тесты обоих скилов |
