# PRI-179 — directed `implementations` graph tool (Phase 1a)

- **Задача:** https://ru.yougile.com/team/686c049c8af8/#PRI-179
- **Бриф:** `docs/superpowers/briefs/2026-07-20-PRI-179-implements-edges-implementations-tool.md`
- **Скоуп:** Вариант A — read-тул, self-heal не трогаем.
- **Дата:** 2026-07-21

## Проблема

Кодовый граф хранит рёбра `IMPLEMENTS` (`Sub -[:IMPLEMENTS]-> Base`), которые SCIP-бэкенд
эмитит на наследование `class X(Y)` и на override метода (подтверждено спайком Phase 0,
2026-07-20: `scip-python` 0.6.6 ставит `is_implementation`; `reviewer/graph/scip.py:42-51`
уже строит эти рёбра на полном `reviewer index` с SCIP).

Session-less графовый слой (`reviewer/mcp/service.py`) отдаёт только:
- `callers` — directed входящие `CALLS`;
- `related_symbols` — **undirected** обход `CALLS|IMPLEMENTS|TESTED_BY` (2 hops), всё вперемешку.

Для OO/registry/dispatch-задач в solve-task («добавь провайдера / handler») нет directed-запроса
«кто наследует/реализует X». `related_symbols` смешивает наследников с вызывающими и тестами и не
различает направление. Пробел: directed incoming `IMPLEMENTS`.

## Решение

Добавить directed session-less тул `implementations(node_id)` — входящие `IMPLEMENTS`, по образцу
`callers`. Ничего в построении/синхронизации графа не меняем: рёбра уже есть после полного
`reviewer index` с SCIP.

### Семантика

- `implementations("path#Base")` → **подклассы** `Base` (кто наследует).
- `implementations("path#Base.method")` → **override-ы** метода (тот же запрос; SCIP эмитит
  `Sub#greet -[:IMPLEMENTS]-> Base#greet`).
- Внешние базовые типы (`Exception`, `abc.ABC`) не являются узлами графа репо → не возвращаются.
- Направление — только incoming (наследники/реализации `X`). Обратную сторону (суперклассы `X`)
  сознательно НЕ покрываем — для неё остаётся undirected `related_symbols`.

## Изменения по слоям

### 1. `reviewer/graph/store.py` — `implementations_detailed`

Клон `callers_detailed` (store.py:96-106), ребро `IMPLEMENTS`, направление входящее:

```python
def implementations_detailed(self, repo: str, node_ids: list[str], *,
                             branch: str = "") -> list[dict]:
    """Реализации/наследники символов — входящие IMPLEMENTS.
    Элементы: {"id": <node_id>, "rel": "IMPLEMENTS"}, упорядочены по id."""
    records, _, _ = self._driver.execute_query(
        "UNWIND $ids AS sid "
        "MATCH (c:Symbol {repo: $repo, branch: $branch})-[:IMPLEMENTS]->"
        "(s:Symbol {repo: $repo, branch: $branch, id: sid}) "
        "RETURN DISTINCT c.id AS id ORDER BY id",
        ids=list(node_ids), repo=repo, branch=branch)
    return [{"id": r["id"], "rel": "IMPLEMENTS"} for r in records]
```

Контракт результата (`[{"id", "rel"}]`) идентичен `callers_detailed` → переиспользуется
`format_neighbors` без правок (он уже умеет тип `IMPLEMENTS`).

### 2. `reviewer/mcp/service.py` — session-less `implementations`

Клон `callers` (service.py:661-681):

```python
def implementations(self, repo: str, node_id: str,
                    branch: str | None = None) -> str:
    """Кто реализует/наследует символ node_id ('path#fqn') — входящие IMPLEMENTS,
    без PR-сессии. На элемент: file:line + строка определения + [IMPLEMENTS].
    Класс → подклассы; метод → override-ы. Точно после полного reviewer index с SCIP."""
    rb = self._resolve_repo_branch(repo, branch)
    if isinstance(rb, str):
        return rb
    repo, resolved = rb
    if self.components.graph is None:
        return "(граф недоступен)"
    cl = self._resolve_context_limits(repo, resolved)
    try:
        found = self.components.graph.implementations_detailed(
            repo, [node_id], branch=resolved)
    except Exception:
        log.warning("implementations: сбой графа", exc_info=True)
        return "(implementations не найдены)"
    return format_neighbors(
        found, store=self.components.store, repo=repo, branch=resolved,
        overlay_ref=None, changed_paths=[], empty_msg="(implementations не найдены)",
        cap=cl.graph.callers_topk)
```

### 3. `reviewer/entrypoints/mcp_server.py` — регистрация

По образцу `callers` (mcp_server.py:208-213):

```python
@mcp.tool()
def implementations(repo: str, node_id: str, branch: str | None = None) -> str:
    """Implementers/subclasses of a symbol node_id 'path#fqn' over the base index
    (incoming IMPLEMENTS, no PR session). A class node -> its subclasses; a method
    node -> its overrides. Each item: node_id + (file:line) + one-line definition
    snippet + [IMPLEMENTS]. Accurate after a full `reviewer index` with SCIP.
    branch defaults to the primary tracked branch."""
    return service.implementations(repo, node_id, branch)
```

### 4. `plugin/skills/solve-task/SKILL.md` — hint (Step 3, graph-deepening)

Дописать в блок графовых тулов: для OO/registry/dispatch-задач звать `implementations(node_id)`
(directed «кто наследует/реализует X») вместо/вместе с undirected `related_symbols`. Fail-soft
`(implementations не найдены)` — не фатально.

## Тесты

- `tests/graph/` (integration, реальный Neo4j) — `implementations_detailed`:
  - позитив: `Sub -[IMPLEMENTS]-> Base` → запрос по `Base` вернёт `Sub`;
  - override: запрос по `Base#method` вернёт `Sub#method`;
  - пустой: символ без наследников → `[]`;
  - изоляция repo/branch (как в существующих store-тестах).
- `tests/tools/test_service.py` — session-less `implementations` (мок graph):
  - формат вывода через `format_neighbors` (file:line + `[IMPLEMENTS]`);
  - fail-soft: `graph is None` → `(граф недоступен)`; исключение → `(implementations не найдены)`;
  - пустой результат → `(implementations не найдены)`.
- `tests/skills/` — guard: hint в solve-task не ломает include-сборку промпта.

## Что сознательно НЕ делаем

- `reviewer/services/graph_sync.py` не меняем. Шаг задачи «`delete_outgoing_implements` в
  self-heal» отклонён: tree-sitter self-heal не переэмитит IMPLEMENTS, поэтому удаление исходящих
  IMPLEMENTS у изменённых классов стёрло бы корректные выжившие рёбра (строго хуже по покрытию).
- `reviewer/graph/builder.py` не меняем (Phase 1b — extraction superclasses в tree-sitter — вне
  скоупа).
- PR-session вариант тула (`reviewer/tools/`) не добавляем.

## Известное ограничение

IMPLEMENTS точны после полного `reviewer index` с SCIP. При инкрементальном self-heal:
- неизменённое наследование **переживает** PR (self-heal трогает только CALLS);
- **новый** подкласс в PR — невидим (tree-sitter не эмитит IMPLEMENTS);
- **сменённое** наследование (`Sub(Base)` → `Sub(Other)`) оставляет stale-ребро.

Полная точность восстанавливается ручным `reviewer index` с SCIP — совпадает с уже
задокументированным инвариантом графа в CLAUDE.md. Отражаем это в докстринге тула.

## Доки / манифест

- `README.md` + `README.ru.md` — новый session-less тул в списке инструментов.
- `CLAUDE.md` — таблица модуля `reviewer/tools` (перечень session-less вариантов).
- `update_codex_plugin_manifest.py` — прогнать (правка `plugin/skills/solve-task/SKILL.md` меняет
  payload-digest → иначе красные install-тесты).

## Стиль

Русские докстринги/комментарии/сообщения. Conventional Commits на русском, без self-attribution.
