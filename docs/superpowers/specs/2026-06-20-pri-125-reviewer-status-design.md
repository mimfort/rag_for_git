# reviewer status — здоровье/свежесть индекса (PRI-125 / ID-125)

**Задача:** [PRI-125](https://ru.yougile.com/team/686c049c8af8/#PRI-125) · слой «Движок (reviewer CLI/MCP)» · оценка S
**Дата:** 2026-06-20

## Проблема

SHA последней индексации хранится в `index_meta`, индекс — мульти-бранч и мульти-репо,
но **нет команды показать состояние индекса**. Непонятно, насколько индекс свеж и можно ли
доверять ревью: какой SHA проиндексирован, отстаёт ли он от текущего кода, сколько чанков,
каким бэкендом построен граф, есть ли «висящие» overlay.

## Цель

Новая CLI-команда `reviewer status [PATH] [--repo] [--branch]`, которая по веткам отслеживаемого
репозитория выводит:

- last-indexed SHA (из `index_meta`) + когда проиндексировано (`updated_at`);
- дрейф vs текущий тип ветки в git: «свежо» / «отстаёт на N коммитов» / «дрейф неизвестен»;
- количество чанков (по `ref`);
- доступный бэкенд графа (SCIP / tree-sitter);
- число узлов графа по ветке;
- наличие overlay-рефов (`pr:*` / `local` / legacy `base`).

**Критерий приёмки:** по каждой ветке показывает «свежо / отстаёт на N коммитов»; **не тратит Voyage**.

## Решения по дизайну (зафиксированы в brainstorming)

1. **Источник git для дрейфа** — опциональный позиционный `PATH` (дефолт `"."`/cwd), как у `index`/`search`.
   Дрейф считается только если `PATH` — git-репо и indexed SHA достижим; иначе «дрейф неизвестен»,
   остальное (SHA, чанки, бэкенд, overlay) печатается всегда.
2. **Бэкенд графа** — определяется через `shutil.which("scip-python")` (как в `check`) и
   формулируется как «доступный бэкенд для следующей индексации» (не утверждение «чем построен
   текущий граф» — бэкенд нигде не персистится).
3. **Формат вывода** — только человекочитаемый (YAGNI). Машинное потребление (напр. задачей №6
   «самообновляемый индекс») идёт через переиспользование функции `gitutil.commits_behind`, не через
   парсинг CLI-вывода. Флаг `--json` намеренно не добавляем.
4. **Структура** — тонкий чистый билдер отчёта отдельно от рендера (подход B): данные собирает
   `build_status_report`, печатает CLI.

## Архитектура

### Новый модуль `reviewer/services/status.py`

Чистый билдер + датаклассы. **Не импортирует** `Settings`, `shutil`, эмбеддер — только `store`,
`graph`, `gitutil`. Это гарантирует «не тратит Voyage» и делает билдер юнит-тестируемым на фейках.

```python
from dataclasses import dataclass
from datetime import datetime

@dataclass
class BranchStatus:
    branch: str
    ref: str                    # base:<branch>
    indexed_sha: str | None     # None → ветка не проиндексирована
    updated_at: datetime | None
    chunks: int
    graph_nodes: int | None     # None → Neo4j недоступен (fail-soft)
    drift: int | None           # 0 → свежо; >0 → отстаёт; None → дрейф неизвестен

@dataclass
class OverlayStatus:
    ref: str                    # pr:N | local | legacy 'base'
    chunks: int

@dataclass
class RepoStatus:
    repo: str
    branches: list[BranchStatus]
    overlays: list[OverlayStatus]

def build_status_report(store, graph, repo, branches, repo_path) -> RepoStatus:
    ...
```

Алгоритм билдера:

- Для каждой `branch` из `branches`:
  - `ref = base_ref(branch)`;
  - `row = store.get_index_meta_row(repo, ref)` → `(sha, updated_at)` или `None`;
    - если `None`: `indexed_sha=None`, `updated_at=None`, `chunks=count_chunks`, `graph_nodes`,
      `drift=None` (нечего сравнивать);
  - `chunks = store.count_chunks(repo, ref)`;
  - `graph_nodes = graph.count_nodes(repo, branch)` в `try/except` → `None` при сбое Neo4j;
  - `drift`: пробуем `gitutil.commits_behind(repo_path, sha, cand)` для `cand` из
    `[branch, f"origin/{branch}"]` по порядку; первый не-`None` результат; иначе `None`.
- Overlay: `refs = store.list_refs(repo)`; **overlay = ref, не начинающийся с `"base:"`** → для
  каждого такого `r` строим `OverlayStatus(r, count_chunks(repo, r))`. Это правило захватывает
  `pr:<n>`, `local` и legacy `base` (без ветки); per-branch base-индексы (`base:<branch>`)
  исключаются.

> Уточнение по legacy `base`: ref `"base"` (без ветки) — это недомигрированный legacy-индекс. Он не
> привязан к ветке из `REVIEW_BRANCHES`, поэтому попадает в раздел overlay как сигнал «нужна
> `reviewer migrate-branches`».

### Аддитивные read-only методы (существующее поведение не меняется)

| Файл | Новый метод | Запрос |
|---|---|---|
| `reviewer/index/store.py` | `get_index_meta_row(repo, ref) -> tuple[str, datetime] \| None` | `SELECT sha, updated_at FROM index_meta WHERE repo=%s AND ref=%s`; тот же fail-soft на `psycopg.errors.UndefinedTable`, что и `get_index_meta` |
| `reviewer/index/store.py` | `count_chunks(repo, ref) -> int` | `SELECT count(*) FROM chunks WHERE repo=%s AND ref=%s` |
| `reviewer/index/store.py` | `list_refs(repo) -> list[str]` | `SELECT DISTINCT ref FROM chunks WHERE repo=%s ORDER BY ref` |
| `reviewer/graph/store.py` | `count_nodes(repo, branch) -> int` | `MATCH (s:Symbol {repo:$repo, branch:$branch}) RETURN count(s) AS n` |
| `reviewer/gitutil.py` | `commits_behind(repo, sha, ref) -> int \| None` | `git rev-list --count <sha>..<ref>`; `subprocess.CalledProcessError` → `None` |

Существующий `get_index_meta` (горячий путь `prepare_review`) **не трогаем** — добавляем отдельный
`get_index_meta_row`.

### Команда в `reviewer/entrypoints/cli.py`

Новая `@cli.command() status`, по образцу `check`/`index`:

```python
@cli.command()
@click.argument("path", default=".")
@click.option("--repo", "repo_tag", default=None, help="owner/name тег индекса; по умолчанию из git remote origin")
@click.option("--branch", "branch_opt", default=None, help="одна ветка; по умолчанию все из REVIEW_BRANCHES")
def status(path: str, repo_tag: str | None, branch_opt: str | None) -> None:
    """Показать здоровье/свежесть base-индекса по веткам (не тратит Voyage)."""
    s = Settings()
    repo = _resolve_repo(repo_tag, path, s)
    branches = [branch_opt] if branch_opt else s.review_branches_list()
    store = ChunkStore(s.pg_dsn, min_size=s.pg_pool_min_size, max_size=s.pg_pool_max_size)
    graph = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    try:
        report = build_status_report(store, graph, repo, branches, path)
        backend = "scip-python (точный)" if _shutil.which("scip-python") else "tree-sitter (fallback)"
        _render_status(report, backend)        # рендер через click.echo
    finally:
        store.close()
        graph.close()
```

Стора создаются **напрямую** (как в `check`), а не через `build_components`, — чтобы не
инстанцировать эмбеддер и не требовать ключ Voyage.

## Поток данных

```
status [PATH] [--repo] [--branch]
  └─ Settings → _resolve_repo(repo_tag, PATH) → repo
  └─ branches = [--branch] | review_branches_list()
  └─ ChunkStore + GraphStore (напрямую, без эмбеддера)
  └─ build_status_report(store, graph, repo, branches, PATH):
        per branch: get_index_meta_row → count_chunks → count_nodes(fail-soft)
                    → commits_behind(PATH, sha, branch|origin/branch)
        overlays:   list_refs(repo) − base:* → count_chunks
  └─ backend = which scip-python
  └─ _render_status(report, backend) → click.echo
```

## Обработка ошибок (fail-soft)

| Сбой | Поведение |
|---|---|
| `PATH` не git-репо / SHA вне истории / ветки нет локально | `commits_behind → None` → «дрейф неизвестен»; остальное печатается |
| `index_meta` нет записи или таблицы | ветка → «не проиндексирована»; без падения (как `get_index_meta`) |
| Neo4j недоступен | `count_nodes → None` → «узлы графа: —» + строка-предупреждение; команда продолжается |
| Postgres недоступен | осмысленных данных нет → понятное сообщение об ошибке + `exit 1` (как `check`) |
| `--branch` вне `REVIEW_BRANCHES` | статус всё равно показываем (диагностика валидна), без ошибки |

Принцип — как у `check`: Postgres критичен; Neo4j / git / scip-python информационны и fail-soft.
`exit 1` только при недоступном Postgres.

## Формат вывода (пример)

```
Репозиторий: mimfort/rag_for_git
Граф (бэкенд для индексации): tree-sitter (fallback, scip-python не найден)

Ветка main   [base:main]
  SHA:    7cd99bb  (проиндексировано 2026-06-18 14:02)
  Статус: ✓ свежо
  Чанки:  1843   Узлы графа: 1207

Ветка dev    [base:dev]
  SHA:    442e7ba  (проиндексировано 2026-06-15 09:30)
  Статус: ↗ отстаёт на 12 коммитов
  Чанки:  1850   Узлы графа: —  (Neo4j недоступен)

Overlay:
  pr:24   18 чанков
```

- Ветка без индекса → `SHA: — (не проиндексирована)`, статус/дрейф опускаются.
- Без git-клона → `Статус: дрейф неизвестен (нет git-клона)`.
- Нет overlay → раздел «Overlay» опускается.

## Тестирование

- **`tests/services/test_status.py` (unit)** — `build_status_report` на фейковых `store`/`graph`
  (in-memory, как в остальных unit-тестах). Кейсы: свежо (drift=0), отстаёт (drift=N), не
  проиндексирована (row=None), fail-soft Neo4j (`count_nodes` бросает → `graph_nodes=None`),
  выделение overlay из `list_refs` (отсев `base:*`, попадание `pr:*`/`local`/legacy `base`).
- **`tests/test_gitutil.py` (unit, расширить существующий)** — `commits_behind` на временном git-репо
  (`tmp_path` + пара коммитов): 0 при `HEAD==sha`, N после N коммитов, `None` для мусорного sha и
  для не-git-каталога.
- **Стораджевые smoke** (`tests/index/`, `tests/graph/`, маркер `integration` если нужен живой БД) —
  `count_chunks` / `list_refs` / `get_index_meta_row` / `count_nodes` возвращают ожидаемое.
- `ruff check .` чистый по затронутым файлам.

## Объём (что НЕ входит)

- Нет `--json` / машинного формата (YAGNI; №6 зовёт `commits_behind` напрямую).
- Не персистим бэкенд графа в `index_meta` (отдельная задача, если понадобится точность «чем
  построен граф»).
- Не трогаем `get_index_meta` и логику `prepare_review`.
- Не добавляем кросс-репо агрегаты — статус по одному `repo` за вызов.

## Затрагиваемые файлы

- `reviewer/services/status.py` — **новый** (билдер + датаклассы).
- `reviewer/entrypoints/cli.py` — новая команда `status` + рендер `_render_status`.
- `reviewer/index/store.py` — `get_index_meta_row`, `count_chunks`, `list_refs`.
- `reviewer/graph/store.py` — `count_nodes`.
- `reviewer/gitutil.py` — `commits_behind`.
- `tests/services/test_status.py` — **новый**; `tests/test_gitutil.py` — расширить.
- `README.md` / `CLAUDE.md` — упомянуть команду `reviewer status` в списке CLI (короткая правка).
