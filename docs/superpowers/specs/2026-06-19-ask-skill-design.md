# Дизайн: скил `ask` (grounded Q&A по кодовой базе) + session-less граф

- **Задача:** PRI-118 / ID-118 «4. Q&A / онбординг по кодовой базе» (колонка «Плагин/агент (скилы)», оценка M).
- **Дата:** 2026-06-19.
- **Статус:** одобрен (brainstorming), готов к writing-plans.

## Цель

Дать новый Claude Code-скил `plugin/skills/ask/` для **grounded-вопросов по кодовой базе** (онбординг/Q&A),
а не ревью PR. Поток: вопрос → семантико-лексический поиск → точечное раскрытие по графу → подтверждение
исходника → ответ с цитатами `path:line`, **без выдуманных путей**.

**Критерий приёмки (из задачи):** вопрос «где аутентификация» возвращает конкретные файлы/символы с
обоснованием; ответы grounded (со ссылками), без галлюцинированных путей.

## Контекст и развилка (зачем Path B)

Существующие примитивы:
- `search_codebase(repo, query, top_k, branch)` — **session-less** MCP-тул (`mcp_server.py:109`). Под капотом
  `Retriever.search_base` (`retriever.py:60-98`) уже делает гибрид (RRF) + graph-expansion 1 hop + Voyage rerank.
  Возвращает чанки с заголовками `path#fqn (path:start-end)`.
- Графовые тулы `get_related_symbols` / `find_callers` / `get_definition` и `read_file` **привязаны к PR-сессии**
  (`repo, pr` → `service._session(repo, pr)`, требует `prepare_review`). Для Q&A без PR через MCP недоступны.

Почему не «просто grep» (нативный поиск Claude Code):
- **Семантика** бьёт через разрыв словаря: «где аутентификация» найдёт `verify_token`/`check_credentials` даже
  без слова «auth». Grep требует заранее знать нейминг — это и есть онбординг-выигрыш.
- **Граф** даёт точность за счёт типов: точные callers/implements против ложных срабатываний/пропусков grep
  (`self.method()`, алиасы импорта, одноимённые методы).
- **Экономия токенов** на расплывчатых вопросах: reranked top-k чанков с кодом внутри → меньше последующих
  чтений и тупиков. Для точной строки grep дешевле → скил **дополняет** grep, а не заменяет (fail-open на grep).

**Решение: Path B** — добавить session-less варианты графовых тулов. Цена умеренная: графовые операции
(`components.graph.expand/callers/find_symbol`) PR-overlay не требуют и **не тратят Voyage-токены** (чистый Neo4j);
session-less вариант зеркаля́ет уже существующий паттерн `search_codebase` (который зовёт
`components.retriever.search_base` напрямую, `service.py:326-351`). Переиспользуется будущими скилами PRI-119
(PR walkthrough) и PRI-126 (радиус поражения) — строим один раз на три скила.

**Вне объёма (follow-up):** режим «карта подсистемы» (N-hop обход от точки входа) — выносится в отдельную
итерацию, логично слить с PRI-119/PRI-126, которым нужен тот же session-less обход. Session-less `read_file`
**не добавляем** — harness `Read` читает исходник с диска (репо открыто как проект).

## Секция 1 — Сервер: 3 session-less графовых MCP-тула

### Сервисный слой (`reviewer/mcp/service.py`)

Все методы session-less, fail-soft (`try/except` + `log.warning` + текстовая заглушка), с общим резолвом
repo/branch.

- **Рефактор:** вынести из `search_codebase` приватный хелпер
  `_resolve_repo_branch(repo, branch) -> tuple[str, str] | str`:
  - repo: `repo or default_repo`; `normalize_repo`; пустой/некорректный → строка-ошибка;
  - branch: если задан и не в `REVIEW_BRANCHES` → строка-ошибка; иначе `branch or primary_branch()`;
  - возвращает `(repo, resolved_branch)` либо текст ошибки (как сейчас отдаёт `search_codebase`).
  - `search_codebase` переписать поверх хелпера (поведение не меняется — покрыто существующими тестами).
- `related_symbols(repo, node_id, branch=None) -> str`
  → `components.graph.expand(repo, [node_id], hops=2, branch=resolved)` → отсортированные id или `(нет связей)`.
- `callers(repo, node_id, branch=None) -> str`
  → `components.graph.callers(repo, [node_id], branch=resolved)` → или `(вызовов не найдено)`.
- `definition(repo, symbol, branch=None) -> str`
  → `graph.find_symbol(repo, symbol, branch=resolved)` → `store.fetch_nodes(repo, ids[:3], overlay_ref=None,
  changed_paths=[], base_ref=base_ref(resolved))` → фолбэк `retriever.search_base(repo, symbol, top_k=3,
  branch=resolved)`. Зеркало `code_tools.get_definition`, но без overlay.

### MCP-слой (`reviewer/entrypoints/mcp_server.py`)

Тонкие обёртки `@mcp.tool()` над сервисными методами. **Имена не должны совпадать** с session-bound тулами
(`get_related_symbols`/`find_callers`/`get_definition` заняты — FastMCP требует уникальности). Имена:

| Тул | Сигнатура | Назначение |
|---|---|---|
| `related_symbols` | `(repo, node_id, branch=None)` | соседи по графу (calls/implements/tests) |
| `callers` | `(repo, node_id, branch=None)` | прямые вызывающие (impact) |
| `definition` | `(repo, symbol, branch=None)` | определение символа (graph → index → semantic) |

Докстринги по-английски, как у соседних тулов.

### Тесты

`tests/mcp/` (или существующий файл сервиса) — unit на 3 метода с фейковыми `graph`/`store`/`retriever`
(по образцу `tests/tools/test_code_tools.py`):
- happy-path делегирование в `components.graph.*` с правильными `(repo, branch)`;
- резолв branch: валидная из `REVIEW_BRANCHES` / невалидная (ошибка-строка) / дефолт (primary);
- fail-soft: исключение в графе → заглушка, не пробрасывается;
- `definition` фолбэк на `retriever.search_base`, когда граф пуст.

## Секция 2 — Скил `plugin/skills/ask/SKILL.md`

**Язык:** тело SKILL.md — **английский** (экономия токенов при загрузке, как `solve-task`/`sync-codebase`),
но скил **явно инструктирует агента отвечать пользователю по-русски**.

**Frontmatter:**
- `name: reviewer_ask`
- `description` с триггерами EN+RU: «ask about the codebase», «explain how X works», «where is X»,
  «как устроено…», «где у нас…», «объясни код». Требует построенный base-индекс + граф.

**Inputs:** `$ARGUMENTS` — свободный вопрос по коду.

**Pipeline:**
1. **Resolve repo/branch** — как `solve-task`: `git remote get-url origin` → `owner/name`; текущая ветка
   (`git branch --show-current`), если она в `REVIEW_BRANCHES`, иначе omit → первичная.
2. **Search** — `search_codebase(repo, question, branch)` (уже 1-hop граф + rerank). Из заголовков чанков
   `path#fqn (path:start-end)` извлечь node_id и строки.
3. **Expand (по необходимости)** — для ключевых символов точечно `related_symbols` / `callers` / `definition`.
   Не обходить всё подряд — только то, что нужно для ответа.
4. **Confirm source** — harness `Read` по `path:line` с диска для проверки и точных цитат.
5. **Answer (adaptive):**
   - по умолчанию: прямой ответ 2-4 предложения + список «доказательств» `path:line` с однострочным «почему»;
   - широкий вопрос («объясни подсистему X»): развернуть в разделы (Краткий ответ / Ключевые символы /
     Поток / Связанные места).

**Grounding-контракт (жёсткий, = критерий приёмки):** цитировать **только** пути, реально вернувшиеся из
тулов и подтверждённые `Read`. Непроверенный путь в ответ не попадает; ничего не выдумывать.

**Fail-open:** Postgres/Neo4j/индекс недоступны → деградация на harness `Grep`/`Glob`/`Read` по диску + явное
предупреждение «семантика/граф недоступны, лексический поиск». Никогда не падать.

**Notes:**
- Предусловие — `reviewer index` построен. Точность графа зависит от бэкенда: SCIP → `IMPLEMENTS` + точные
  `CALLS`; tree-sitter → `CALLS` по имени.
- Граница с PRI-119: `ask` отвечает в терминал на вопрос по базе; walkthrough постит гид по PR. Раздельно.
- «Карта подсистемы» (N-hop) — follow-up.

## Секция 3 — Документация

- `mcp_server.py` докстринг `create_server`: «15 тулов» → «18 тулов».
- `CLAUDE.md` (таблица модулей, строка `reviewer/tools/`) и `README.md`: упомянуть 3 session-less графовых тула
  рядом с `search_codebase`.
- `plugin.json` не трогаем — скилы авто-дискаверятся из `plugin/skills/`.

## Порядок реализации (для writing-plans)

1. Рефактор `_resolve_repo_branch` + переписать `search_codebase` поверх него (зелёные существующие тесты).
2. 3 сервисных метода + unit-тесты.
3. 3 MCP-тула в `mcp_server.py` + обновить докстринг счётчика тулов.
4. `plugin/skills/ask/SKILL.md`.
5. Доки (`CLAUDE.md`, `README.md`).
6. Ручная проверка критерия приёмки: «где аутентификация» → grounded-ответ с `path:line` (нужны поднятые
   Postgres/Neo4j + построенный индекс).

## Открытые вопросы

Нет (имена тулов `related_symbols`/`callers`/`definition` подтверждены; объём B зафиксирован).
