# Brief — PRI-179 Graph: INHERITS/IMPLEMENTS edges + implementations MCP tool (spike-gated)
url: https://ru.yougile.com/team/686c049c8af8/#PRI-179

## Task
- **Проблема:** graph-deepening в solve-task видит только CALLS-соседей. Для OO/registry/dispatch-задач («добавь провайдера/handler») subclass/override невидимы; `related_symbols` (undirected, `CALLS|IMPLEMENTS|TESTED_BY`) смешивает всё в кучу и не даёт directed «кто реализует/наследует X».
- **Цель:** directed session-less тул `implementations` (входящие IMPLEMENTS) + сохранность IMPLEMENTS-рёбер при инкрементальном self-heal + hint в solve-task для OO-задач + тесты.
- **Spike-gated → спайк ПРОЙДЕН (2026-07-20).** `scip-python` 0.6.6 ставит `is_implementation` на обычное наследование `class Sub(Base)`, на override метода (`Sub#greet→Base#greet`), на `class MyError(Exception)` (внешний base отбрасывается — ок) и на ABC (`Impl→Iface`). Значит `scip.py:42-51` **уже** производит внутрирепные IMPLEMENTS-рёбра на полном `reviewer index` с SCIP → **дешёвый Phase 1a жизнеспособен, Phase 1b (extraction в tree-sitter) для read-тула НЕ требуется**.
- **Критерии приёмки:** `implementations_detailed` (directed incoming IMPLEMENTS, позитив+пустой); тул `implementations` зарегистрирован и fail-soft (`"(implementations не найдены)"`); IMPLEMENTS не деградируют при self-heal; hint в solve-task; тесты store/service/graph_sync.

## Related work
- **ID-148** [done] — компактный вывод графовых тулов (`find_callers`/`get_related_symbols`): `file:line` + сниппет + тип ребра. Новый `implementations` **обязан** повторить этот формат — переиспользовать `format_neighbors` (он уже умеет тип `IMPLEMENTS`), как это делает `callers` (`mcp/service.py:678`).
- (dropped 6: ID-132 TS/JS — другой механизм; ID-145/206 — про confidence/blast-radius находок, не построение рёбер; ID-158/154 — diff/skeleton, не graph; ID-178 — reranker fallback, не graph edges. Связанных задач/PR у PRI-179 нет — задача изолирована.)

## Subsystems
- **reviewer/graph** — ядро правок: `backend.py` (выбор SCIP/tree-sitter), `builder.py` (tree-sitter, CALLS-only), `scip.py` (уже эмитит IMPLEMENTS), `store.py` (Neo4j GraphStore, branch-scoped, expand/callers).
- **reviewer/services** — `graph_sync` инкрементальный патч графа при prepare: переразбирает изменённые .py, сохраняя входящие CALLS; **тут узкое место IMPLEMENTS** (см. Open questions).
- **reviewer/tools** — `format_neighbors` (file:line + тип ребра CALLS/IMPLEMENTS + дистанция, fail-open); паттерн вывода для нового тула.
- (dropped: reviewer/index, reviewer/tasks — не трогаются.)

## Relevant code
- `reviewer/graph/scip.py:42-51` — IMPLEMENTS-эмиссия (спайк-подтверждена; внешний base → `symbol_to_node.get`→None→ребро отбрасывается, ок). Здесь же критерий спайка: задокументировать вывод спайка комментарием.
- `reviewer/graph/store.py:96-106` — `callers_detailed` (входящие CALLS, `[{"id","rel":"CALLS"}]`, ORDER BY id) → **клонировать в `implementations_detailed`** с `(c)-[:IMPLEMENTS]->(s)` и `rel:"IMPLEMENTS"`.
- `reviewer/graph/store.py:86-94` — `callers` (set-возврат, directed incoming) — образец Cypher-направленности.
- `reviewer/graph/store.py:178-185` — `delete_outgoing_calls` → **клонировать в `delete_outgoing_implements`** (`[r:IMPLEMENTS]`), НО применять осторожно (см. Open questions).
- `reviewer/graph/store.py:108-127` — `expand_detailed` (undirected, уже ходит по IMPLEMENTS) — это то, что сейчас использует `related_symbols`; показывает, почему directed-тул нужен.
- `reviewer/mcp/service.py:661-681` — session-less `callers` → **клонировать в `implementations`**: тот же `_resolve_repo_branch`/graph-None-guard/`format_neighbors(cap=cl.graph.callers_topk)`, `empty_msg="(implementations не найдены)"`.
- `reviewer/entrypoints/mcp_server.py:208-213` — регистрация `callers` через `@mcp.tool()` → добавить аналог `implementations` (docstring EN + краткий русский смысл в теле, см. соглашения).
- `reviewer/services/graph_sync.py:29,39` — `build_graph_from_files` (tree-sitter, CALLS-only) + `delete_outgoing_calls`; точка, где решается судьба IMPLEMENTS при PR.
- `plugin/skills/solve-task/SKILL.md` Step 3 — добавить hint: для OO/registry-задач звать `implementations` (directed) вместо/вместе с undirected `related_symbols`.
- (dropped: `builder.py` — трогаем только если выберут Phase 1b; `in_degree`/`find_symbol`/`symbols_for_paths` — не нужны.)

## Test exemplars
- `tests/graph/` — тесты `store` (Neo4j): upsert/expand/callers, изоляция по repo/branch → добавить `implementations_detailed` (позитив: `Sub-[IMPLEMENTS]->Base` → запрос по Base вернёт Sub; пустой). `@pytest.mark.integration` если реально ходит в Neo4j.
- `tests/tools/` — тесты `format_neighbors` (file:line, тип ребра CALLS/IMPLEMENTS, cap-усечение, fail-open «вне индекса») → тул `implementations` наследует этот вывод; `test_service.py` для session-less тула (мок graph → `implementations_detailed`).
- `tests/skills/` — guard-тесты сборки промптов solve-task → проверить, что hint не ломает include-сборку.
- (dropped 0.)

## Constraints / open questions
- **[Ключевая развилка] `delete_outgoing_implements` в self-heal при Phase 1a — ОПАСЕН.** tree-sitter (`build_graph_from_files`) НЕ эмитит IMPLEMENTS. Если в `graph_sync` вызвать `delete_outgoing_implements`, исходящие IMPLEMENTS изменённого класса будут удалены и НЕ переэмитятся → станет хуже, чем сейчас (сейчас self-heal их не трогает → они выживают stale-but-present). Варианты для brainstorming: **(a)** read-тул без правки self-heal — IMPLEMENTS остаются точны после полного `reviewer index`, устаревают для изменённых классов (совпадает с уже задокументированным в CLAUDE.md инвариантом «полная точность графа — ручным reviewer index с SCIP»); **(b)** Phase 1b — портировать extraction `superclasses` в `builder.py`, тогда self-heal переэмитит IMPLEMENTS и `delete_outgoing_implements` корректен (больше кода/тестов). Пользователь выбрал «Продолжить» (полный Phase 1a) — но этот шаг требует явного решения a/b до реализации.
- **IMPLEMENTS существуют только с SCIP.** `GRAPH_BACKEND=auto`; в этом окружении `scip-python` 0.6.6 в PATH → SCIP. При отсутствии scip-python бэкенд — tree-sitter (CALLS-only), тул вернёт `(implementations не найдены)` (корректный fail-soft).
- **Ценность умеренная:** `related_symbols` уже обходит IMPLEMENTS (undirected). Прирост — directed-фокус для OO/registry-задач, не новая способность.
- **Правка плагина → пересборка манифестов.** Изменение `plugin/skills/solve-task/SKILL.md` меняет payload-digest → прогнать `update_codex_plugin_manifest.py`, иначе красные install-тесты.
- **Доки:** новый тул → обновить оба README (`README.md` + `README.ru.md`) и таблицу модуля `reviewer/tools` в CLAUDE.md.
- **Индекс dev отстаёт на 6 коммитов** (drift=6) на момент сбора брифа — цитаты выше сверены прямым чтением рабочего дерева, не индексом.
- **Стиль:** русские докстринги/комментарии/сообщения; Conventional Commits на русском без self-attribution.

Собран на: session-model (Opus 4.8), режим: inline

## Токены (этап solve-task)
Модель: claude-opus-4-8
fresh-in 87 · out 84.1K · cache-write 329.5K · cache-read 3.4M
Всего: 3.8M токенов
