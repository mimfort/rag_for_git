# Спека Фазы 4: скилл `/solve-task`

Дата: 2026-06-13
Статус: одобрен (брейншторм с пользователем)
Базовый дизайн: `2026-06-12-claude-code-migration-design.md` (раздел «Фаза 4»)
Фундамент: фаза 2 (плейбуки `task-context-<type>.md`, контракт `TaskBrief`), фаза 3
(тулы `index_task`/`search_tasks`/`get_task_context`, граф+RAG по задачам)

## Цель

Скилл `/solve-task <ключ | свободный текст>` **дисциплинированно собирает контекст**
под задачу и передаёт управление штатному циклу разработки superpowers. Он читает
задачу с доски (если есть ключ и подключена доска), тянет связанные и семантически
похожие задачи с их PR и кодом, ищет релевантный код по формулировке, **сводит только
релевантное** в структурированный бриф и **входит в `superpowers:brainstorming`** с этим
брифом как seed. Скилл — фронт-энд сбора контекста, он **не заменяет разработку**
(brainstorming → writing-plans → subagent-driven-development/TDD).

## Не-цели (вне scope фазы 4)

- Собственный цикл планирования/реализации/тестов — это делает superpowers (скилл лишь
  цепляет его, не дублирует).
- Новые доменные тулы по задачам (уже есть `index_task`/`search_tasks`/`get_task_context`).
- Авто-создание задач/PR на доске; запись в доску (скилл только читает).
- Мульти-репо (инвариант single-repo сохраняется).
- Расширение графа кода или ревью-пайплайна.

## Ключевые решения (зафиксированы с пользователем)

| Вопрос | Решение |
|---|---|
| Поиск кода без PR-сессии | Новый **session-less** MCP-тул `search_codebase(query)` — гибрид-поиск по base-индексу (`search_code` привязан к PR-сессии и для `/solve-task` не годится). |
| Передача в разработку | Скилл собирает бриф и **входит в `superpowers:brainstorming`** с брифом как seed (далее штатно writing-plans → subagent-driven-development). Тесная интеграция «наша система + superpowers», без дублирования цикла. |
| Вход скилла | Ключ задачи (`PRI-4`) **ИЛИ** свободный текст (`«добавить logout»`). При свободном тексте + подключённой доске — тоже ищем похожие задачи (`search_tasks`, дочитка с доски). Деградация без доски естественная. |
| Чтение доски | Скилл через board MCP по плейбукам `review-pr/references/task-context-<type>.md` (переиспользуем фазу 2). Python на доску не ходит. |
| Формат брифа | Структурированный markdown, фикс-секции + явный фильтр релевантности (включать элемент только если прямо влияет на реализацию). |
| Деградация | fail-open: нет доски / задача не найдена / Neo4j ↓ / пустой корпус → идём с тем, что есть (минимум `search_codebase` + описание), отмечаем пробелы, всё равно передаём в brainstorming. |

## Новый MCP-тул `search_codebase`

`search_code` (фаза 1) привязан к PR-сессии (`prepare_review` → `ToolContext` с
`overlay_ref`/`changed_paths`); у `/solve-task` PR нет. Нужен репо-глобальный поиск по
**стабильному base-индексу**.

```
search_codebase(query: str, top_k: int = 10) -> str
```

- Репо-глобальный, без сессии (как `search_tasks`/`get_task_context` фазы 3).
- Реализация (зеркало `Retriever.retrieve`, но **base-only** и сидинг графа от хитов):
  `embedder.embed_query(query)` → `store.hybrid_search` base-only (`changed_paths=[]`,
  несуществующий `overlay_ref` `"__none__"` — условие `(ref='base' AND path∉changed) OR
  ref=overlay` отбирает все base-строки) → **graph-expansion от топ-хитов** (seeds = верхние
  `node_id` результата, НЕ changed-файлы; `hops=1`) → **rerank** (Voyage `rerank-2.5`) →
  `ContextPack` (сниппеты `node_id`/path/строки/текст, ограничено `max_tool_result_chars`).
- **Граф и реранкер — fail-soft** (как `retrieve`): Neo4j недоступен/ошибка → деградация до
  чистого гибрида; реранкера нет / мало кандидатов / граф ничего не добавил → RRF-порядок.
- Делегат `MCPReviewService.search_codebase` поверх `components.embedder`+`components.store`
  (или маленький метод-обёртка `Retriever.search_base` — план выберет).
- Fail-soft: Postgres-проблема / пусто → `"(ничего не найдено)"` + warning.

Зеркалит контракт и тесты `search_tasks` (фаза 3).

## Скилл `plugin/skills/solve-task/SKILL.md` (английский)

Вход: `$ARGUMENTS` — ключ задачи (по `key_pattern`) **или** свободный текст.

1. **Конфиг.** Скилл сам читает `.review.yml:task_board` из рабочего репо (у `/solve-task`
   нет `prepare_review`, прокидывающего конфиг). Нет блока / доска не подключена →
   board-less режим (молча).
2. **Идентификация задачи.**
   - Аргумент матчит `key_pattern` И доска подключена → читает задачу по плейбуку
     `../review-pr/references/task-context-<type>.md` → board-agnostic `TaskBrief`
     → `index_task(TaskBrief)` (персист в граф+корпус, идемпотентно).
   - Иначе (свободный текст / нет доски / нет ключа) → берёт `$ARGUMENTS` как описание
     задачи; доску не читает.
3. **Сбор контекста** (best-effort, fail-open):
   - есть ключ/бриф → `get_task_context(key)` (связанные задачи → их PR → затронутый код);
   - `search_tasks("<title>. <первые строки описания>")` → семантически похожие задачи;
     при подключённой доске — дочитать детали топ-релевантных похожих задач с доски;
   - `search_codebase("<описание задачи>")` → релевантный существующий код.
4. **Бриф решения.** Структурированный markdown с фильтром релевантности (включай элемент
   только если он прямо влияет на реализацию; прочее отбрось, отметь сколько отброшено):
   - **Task** — key/title/требования/критерии (или формулировка пользователя при board-less);
   - **Related work** — релевантные связанные/похожие задачи + их PR (что переиспользовать/
     чему следовать);
   - **Relevant code** — файлы/символы «что трогать / чему подражать» + почему;
   - **Constraints / open questions** — ограничения, неясности, пробелы контекста.
5. **Передача.** Показать бриф пользователю, затем **войти в `superpowers:brainstorming`**,
   передав бриф как seed/контекст. Дальше — штатный цикл superpowers (brainstorming →
   writing-plans → subagent-driven-development/TDD). Скилл на этом свою роль завершает.

## Деградация (fail-open)

| Ситуация | Поведение |
|---|---|
| `task_board` не задан / доска не подключена | board-less: контекст из `search_tasks` (если корпус наполнен) + `search_codebase` + формулировка; бриф отмечает «доска недоступна». |
| Ключ дан, но задача не найдена / MCP доски упал | как board-less по формулировке-аргументу; пометка в брифе. |
| Neo4j ↓ | `get_task_context`/`index_task`-граф деградируют (фаза 3, fail-open); бриф строится из `search_tasks`+`search_codebase`. |
| Корпус задач пуст (нет `/sync-tasks`/прошлых ревью) | `search_tasks` пуст; бриф из доски (если ключ) + `search_codebase`. |
| Postgres ↓ | `search_codebase`/`search_tasks` пусты + warning; бриф минимальный из доски/формулировки; передача в brainstorming всё равно происходит. |

Скилл никогда не падает: при любом пробеле он сводит, что есть, отмечает дефицит и
передаёт в brainstorming.

## Переиспользование

- **Чтение доски** — плейбуки `review-pr/references/task-context-<type>.md` (без дублей;
  относительный путь, как в `/sync-tasks`).
- **Тулы задач** — `index_task`/`search_tasks`/`get_task_context` (фаза 3).
- **Цикл разработки** — `superpowers:brainstorming`/`writing-plans`/`subagent-driven-development`.

## Тестирование

### Unit (Python, фейки, без сети)
- `Retriever.search_base`: base-only запрос (пустой `changed_paths`, overlay `≠ base`),
  graph-expansion сидится от топ-хитов, rerank применяется; fail-soft (Neo4j ↓ → чистый
  гибрид; реранкера нет → RRF-порядок); пусто → пустой `ContextPack`.
- `MCPReviewService.search_codebase` делегат; регистрация MCP-тула (как `search_tasks`/
  `test_server_tools.py`).

### Integration (маркер `integration`, живой Postgres, фейк-эмбеддер)
- Проиндексировать base-чанки → `search_codebase` находит релевантный символ (зеркало
  `tests/index/test_store_hybrid.py` / `tests/tasks/test_integration.py`).

### E2E / ручное (Sonnet/Haiku; Opus берегу)
- Живой Yougile MCP: `/solve-task PRI-4` → читает задачу, собирает бриф (связанные/похожие
  задачи + код), входит в brainstorming.
- `/solve-task «добавить logout»` board-less → бриф из `search_tasks`+`search_codebase`,
  brainstorming.
- Деградация: отключённый Neo4j / без доски → бриф собирается, пометки корректны, передача
  происходит.

Внешние сервисы (доска, Voyage) — только в E2E/ручном (конвенция проекта).

## Влияние на существующий код

**Меняется:**
- `reviewer/mcp/service.py` — метод `search_codebase` (поверх embedder+store).
- `reviewer/entrypoints/mcp_server.py` — регистрация тула `search_codebase`.
- `reviewer/retrieval/retriever.py` *или* `reviewer/index/store.py` — маленький хелпер
  base-only поиска (план выберет минимально инвазивный вариант; вероятно тонкий метод
  `Retriever.search_base` или прямой вызов `store.hybrid_search` с base-параметрами).
- `README.md` — короткая заметка про `/solve-task`.

**Добавляется:**
- Скилл `plugin/skills/solve-task/SKILL.md`.
- Тесты: `tests/mcp/` (делегат + регистрация тула), integration (base-поиск).

**Без изменений:** ревью-пайплайн фаз 1/2, граф/RAG по задачам фазы 3 (только потребляются),
`prepare_review`/`publish_review`, freshness base/overlay.

## Открытые вопросы для плана (не блокируют дизайн)

- Где жить base-поиску: тонкий `Retriever.search_base(query, top_k)` vs прямой
  `store.hybrid_search` в `MCPReviewService.search_codebase`. План выберет (вероятно метод
  на `Retriever` для тестируемости и переиспользования query-эмбеда/кэша).
- Точный «нейтральный» `overlay_ref` для base-only (`"__none__"` или пустая строка) — чтобы
  гарантированно не матчить реальный overlay; план зафиксирует и покроет тестом.
- Нужен ли скиллу top-k похожих задач дочитывать с доски всегда или только при тонком
  `description` (как criteria-нюанс в Yougile-плейбуке) — план уточнит в тексте скилла.

## Источники

- Фундамент: `2026-06-13-phase3-task-graph-rag-design.md` (тулы задач, граф),
  `plugin/skills/review-pr/references/task-context-*.md` (чтение доски),
  `plugin/skills/sync-tasks/` (образец скилла, переиспользующего плейбуки).
- Существующее: `reviewer/index/store.py::hybrid_search` (base/overlay WHERE),
  `reviewer/retrieval/retriever.py` (ContextPack), `reviewer/mcp/service.py` (делегаты,
  session-less тулы фазы 3), `superpowers:brainstorming` (точка входа в разработку).
