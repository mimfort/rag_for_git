# Brief — PRI-251 Задачи-развёртки: ретрив не перечисляет семейство однотипных файлов (implementations слеп к классам и Protocol)
https://ru.yougile.com/team/686c049c8af8/#PRI-251

## Task
PRI-251: диагностика PRI-246 показала, что на bulk-задачах (широкий blast radius: PRI-223 24%/25, PRI-215 36%/14, PRI-225 39%/18, PRI-196 50%/10 против медианы 61%) core-recall низкий не из-за нехватки потолка выдачи (predicted не упирается в ceiling=15), а потому что `implementations` не отвечает на «кто ещё реализует контракт»: класс-уровневые IMPLEMENTS-рёбра не эмитятся (только на переопределения методов), Protocol-контракты структурно не размечаются никаким бэкендом. Нужно: (1) закрыть пробел класс-уровневых IMPLEMENTS в SCIP/tree-sitter, (2) дать альтернативный сигнал для Protocol-семейств (реестр/общая база/каталог), (3) явный полный-или-помеченный-частичным способ перечислить семейство, (4) научить solve-task распознавать задачи-развёртки структурным сигналом («топ-хит — член семейства из N»), (5) закрепить тестами на реальных семействах репо (11 адаптеров досок, 2 Protocol-провайдера), (6) измерить эффект харнессом PRI-250 до/после.

## Related work
- PRI-179 — ввела `IMPLEMENTS`/`implementations` MCP-тул (PR #122, done); `reviewer/graph/scip.py:42-51` уже читает `si.relationships`/`is_implementation`, но, как выяснено сейчас, SCIP не проставляет этот флаг на сам класс, только на переопределения методов — это исходная точка расследования шага 1 (почему scip-python не эмитит implementation на `class X(Y)`).
- PRI-246 — спайк-источник задачи (PR #197, done); дал числа baseline (core-recall, отсутствие корреляции размера брифа с широтой задачи) и выявил механизм провала на bulk-задачах, разобранный в описании PRI-251.
- PRI-250 — офлайн-харнесс метрик (PR #198, done, уже смержен в dev — `eval/solve_task_metrics_history.jsonl`/`solve_task_metrics_report.md` присутствуют и обновляются); даёт готовую команду `python -m eval.solve_task_metrics snapshot`/`compare --back N` для замера core-recall до/после правки — именно им нужно закрывать критерий приёмки №4.
(dropped 0: обе прямо связанные задачи из get_task_context учтены)

## Subsystems
- reviewer/tasks — 11 адаптеров досок регистрируются целиком через `BoardProviderRegistry` (`reviewer/tasks/boards/registry.py`); 8 наследуют `RestBoardBase` явно и номинально, 3 легаси (`yougile`, `youtrack`, `jira`) держат свою httpx-обвязку без общей базы — сигнал принадлежности для них не наследование, а членство в реестре/каталог `reviewer/tasks/boards/`.
- reviewer/policy — `ContextLimits`/`CodebaseLimits` (`floor=4, ceiling=15, ratio=0.5, abs_floor=0.3, candidate_pool=30`) определяют, где ретрив режет выдачу; подтверждает вывод спайка, что потолок не является узким местом.
- reviewer/tools — форматирование графовых соседей (`format_neighbors`, cap=`callers_topk`) — точка, куда добавлять пометку «частичный список / N членов семейства».
(опущено: reviewer/services, reviewer/config, reviewer/tasks-тесты не процитированы отдельно — они уже покрыты Relevant code/Test exemplars ниже)

## Relevant code
- reviewer/graph/scip.py:42-51 (`parse_scip`) — цикл `for si in doc.symbols: for rel in si.relationships: if rel.is_implementation`, эмитит `IMPLEMENTS` только когда relationship стоит на самом символе; для класса, унаследовавшего базу без переопределения методов, scip-python relationship на класс не ставит — эмпирически подтверждено (см. Constraints). Это первая точка правки шага 1.
- reviewer/graph/store.py:108-120 (`GraphStore.implementations_detailed`) — Cypher `MATCH (c)-[:IMPLEMENTS]->(s) RETURN c.id`; направленный обход входящих IMPLEMENTS, класс→подклассы. Менять не нужно, если рёбра появятся выше; нужно менять/дублировать, если решение — синтетическое ребро другого типа или отдельный тул.
- reviewer/graph/builder.py:158-199 (`build_graph_from_files`, tree-sitter) — сейчас эмитит только CALLS; извлечение `class_definition.superclasses` и резолвинг через уже существующий import-aware `_resolve_call` — путь для добора наследования tree-sitter'ом (альтернатива из шага 1, если SCIP-фикс недостижим).
- reviewer/mcp/service.py:683-705 (`MCPReviewService.implementations`) — session-less MCP-тул, fail-soft `"(implementations не найдены)"`; эмпирически именно эта пустая строка воспроизведена на `restbase.py#RestBoardBase` (см. Constraints). Здесь же (или в `format_neighbors`) добавлять пометку частичности из критерия приёмки №3.
- reviewer/tasks/boards/registry.py:106-249 (`BoardProviderRegistry`) — `registered_types()` возвращает все 11 типов в порядке регистрации; готовый структурный источник «кто ещё такой же» для легаси-адаптеров без общей базы (шаг 2 задачи).
- reviewer/tasks/boards/restbase.py#RestBoardBase — базовый класс транспорта; blast radius по графу (`related_symbols`) — 8 прямых CALLS-соседей на distance=1: `AsanaBoard` (asana.py:165), `ClickUpBoard` (clickup.py:251), `GitHubIssuesBoard` (github.py:322), `KaitenBoard` (kaiten.py:306), `LinearBoard` (linear.py:276), `TrelloBoard` (trello.py:211), `WeeekBoard` (weeek.py:246), `YandexTrackerBoard` (yandex_tracker.py:271) — ровно восемь, как заявлено в задаче; связаны CALLS, не IMPLEMENTS.
- reviewer/retrieval/retriever.py:152-226 (`Retriever.search_base`) — adaptive cliff-cutoff ретрива для `/solve-task`; здесь естественная точка для структурного детектора «топ-хит — член семейства из N» (шаг 4 задачи), т.к. уже проходит через `hits`/`graph_ids`/`select_by_cliff`.
- reviewer/graph/backend.py:66-97 (`build_code_graph`) — оркестратор бэкенда SCIP/tree-sitter; полная переиндексация (clear+upsert) при `reviewer index`, значит любая правка эмиссии рёбер требует пересборки графа для проверки.
(dropped 0: каждый найденный сниппет прямо информирует один из шагов задачи)

## Test exemplars
- tests/mcp/test_server_tools.py:148-156 (`test_implementations_tool_forwards`) — паттерн мока `svc.implementations.return_value`, проверка форвардинга аргументов в MCP-тул; шаблон для нового теста «частичный ответ помечен».
- tests/graph/test_backend.py:21-56 (`test_graph_from_scip_bytes_edges_and_leaf_nodes`) — паттерн сборки минимального `scip_pb2.Index` вручную (occurrences DEF/CALL) для юнит-теста парсера без реального scip-python; годится как шаблон для теста класс-уровневого IMPLEMENTS.
- tests/graph/test_backend_integration.py:53-68 (`test_build_with_scip_real`, `@pytest.mark.skipif(shutil.which("scip-python") is None)`) — integration-тест с реальным scip-python на temp-репо; нужный паттерн для проверки, действительно ли реальный scip-python 0.6.6 ставит relationship на класс (эмпирическая часть шага 1).
- tests/tasks/boards/test_restbase.py:11,55,66 (`_board`, `test_task_url_uses_template_and_tolerates_empty_template`, `test_close_closes_underlying_transport`) — существующий фикстурный паттерн для тестов на `RestBoardBase`-семействе.
(dropped 0)

## Constraints / open questions
- Premise задачи эмпирически подтверждён на только что полностью пересобранном (SCIP) индексе: `implementations(repo="mimfort/rag_for_git", branch="dev", node_id="reviewer/tasks/boards/restbase.py#RestBoardBase")` вернул ровно `"(implementations не найдены)"` — тихая пустота, как описано в задаче. `related_symbols` на том же узле находит все 8 наследников на distance=1, но исключительно с типом `[CALLS]`, не `[IMPLEMENTS]` — граф видит связь, просто не той разметкой.
- PRI-250 (харнесс метрик) уже смержен в dev (PR #198): команды `python -m eval.solve_task_metrics snapshot|stats|compare --back N|forecast`, история `eval/solve_task_metrics_history.jsonl`, отчёт `eval/solve_task_metrics_report.md` — оба файла помечены как изменённые в текущем git status (свежий срез уже посчитан на dev), т.е. измерять эффект правки шагом «снять срез сейчас → сделать правку → снять срез снова → compare --back 1» можно прямо сейчас без дополнительной подготовки.
- `search_codebase` по запросам "BoardProviderRegistry" и "SCIP parser IMPLEMENTS" оба упёрлись в cliff-обрез (15 из 68 и 8 из 16 соответственно) — по 27 и 6 релевантных результатов остались за обрезом; при необходимости перевызвать с бо́льшим `top_k`/ceiling для более полного охвата семейства адаптеров или альтернативных SCIP-признаков.
- Три легаси-адаптера (yougile, youtrack, jira) не наследуют `RestBoardBase` — для них ни класс-уровневый IMPLEMENTS-фикс, ни tree-sitter-наследование не дадут связи; единственный доступный сигнал их принадлежности к семейству — членство в `BoardProviderRegistry` (шаг 2/критерий приёмки №2), а не граф вызовов/наследования.
- Protocol-контракты (`TaskBoardProvider` в boards/base.py:110, `VCSProvider` в vcs/base.py:89) структурно не наследуются адаптерами → ни SCIP, ни tree-sitter в принципе не дадут для них рёбер IMPLEMENTS без явной номинальной типизации; решение обязано быть alternate-signal, не graph-native.
- Инструменты не найдены недоступными; `definition`/`callers` по scip.py/store.py не вызывались отдельно — покрыты через `search_codebase` сниппетами выше, повторный вызов не требовался.

Собран на: mid (sonnet), сборка: subagent

## Токены (этап solve-task)
Модель: claude-opus-5
fresh-in 150 · out 28.9K · cache-write 215.6K · cache-read 6.8M
Всего: 7M токенов
