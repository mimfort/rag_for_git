# PRI-296. Приёмка отбора тестовых примеров

## Методика

Замер выполнен на рабочем коде с реализацией PRI‑296 (`b15335e38763e50a86036bce393ec4af9e03a185`) и на одном базовом индексе `mimfort/rag_for_git`, ветка `dev`, SHA `bfcefdf96b9ea67beea766aa75adf0acbbcbcdc2`. Обе стороны использовали одни и те же подзапросы, хранилища ParadeDB и Neo4j и действующие лимиты. Смешанный режим воспроизводит прежний вызов внутреннего поиска с `include_tests=True, strict=True`. Режим `tests_only` вызывает источник секции `Test exemplars` из `prepare_task_context` и оставляет тестовые пути между расширением графа и файловым отбором. Скрипт сверяет его набор путей с секцией в полном ответе `prepare_task_context`, полученном с `warm_board=False`.

Пути извлекались из заголовков отрисованных фрагментов и считались один раз на секцию. Классификатор `_is_test_path` считает путь тестовым, если в нём есть сегмент `tests` либо имя файла начинается с `test_` или заканчивается на `_test.py`. Пересечение считалось по точному совпадению путей с выпущенной секцией `code` той же карточки.

Каждая пара использовала один и тот же список из `_test_queries`. Он строится из сохранённой карточки с помощью штатного построителя подзапросов и префикса «как тестируется: ». SHA‑256 рассчитан по упорядоченному списку, сериализованному через `json.dumps(queries, ensure_ascii=False)` в UTF‑8.

Векторная модель — `voyage-code-3`. Эффективные лимиты `code_section` — `max_files=20`, `max_chunks_per_file=1`, `chars_per_file=975`, `max_augmented_files=3`. Источники подмешивания участвовали только в штатной сборке `code`. Гибридный поиск запрашивал до 30 кандидатов на подзапрос (`candidate_pool=30`) и применял порог ANN `ann_distance_max=0.65` к кандидатам без лексического совпадения. Для расширения графа использовались до 15 начальных вершин (`ceiling=15`) и один переход (`hops=1`).

| Карточка | Подзапросы | SHA‑256 списка |
| --- | ---: | --- |
| PRI-278 | 10 | `ae2c133476aa8a3ccdd8d4743d639bbc4dfa8c74664e95af955f9daaf39bc303` |
| PRI-291 | 8 | `e1e69557e7e84f7017727b256e8d1e8aad390b71687d4f8acf1bbf631deea7f6` |
| PRI-256 | 8 | `f84209beb1d22e2320aa33741fcd5622624a4cf065bb5b4fe3b9315877eeb56c` |

Локальное измерение `perf_counter` охватывало вызов поиска секции, включая запросы к хранилищам, расширение графа, отбор и рендеринг. До серии прогревался кэш эмбеддингов и выполнялся один прогревочный вызов каждого режима. Для каждой карточки собрано десять пар с чередованием порядка режимов. В таблице приведены медианы, диапазоны и медиана разностей по парам (`tests_only` − смешанный режим). Чтение карточки, построение полного ответа и разбор путей выполнялись за пределами измеряемого вызова.

## Ручная разметка

Для каждой карточки вручную отобраны четыре файла по её теме и поведению тестов на зафиксированном SHA. Для PRI‑278 ориентиром служила оценка состава контекста, для PRI‑291 — счётчики файлового отбора и видимые пути, для PRI‑256 — файловый бюджет и диверсификация. Покрытие — доля этих четырёх путей, которые присутствуют в выдаче. Разметка ограничена выбранными файлами и оценивает совпадение по путям. Релевантность отдельных фрагментов и полнота по всему множеству подходящих тестов не измерялись.

| Карточка | Путь | Сценарий на зафиксированном SHA |
| --- | --- | --- |
| PRI-278 | `tests/eval/test_context_paths.py` | Извлечение путей из заголовков секции, обработка пустой выдачи и обрезанных заголовков. |
| PRI-278 | `tests/eval/test_live_boundary.py` | Связь eval-поиска со штатными компонентами, лимитами и подмешиванием. |
| PRI-278 | `tests/eval/test_replay.py` | Прогон карточек, расчёт метрик и фиксация идентификатора индекса. |
| PRI-278 | `tests/metrics/test_context_core.py` | Получение эталонных путей из графового окружения и исключение изменённых путей. |
| PRI-291 | `tests/mcp/test_context_limits_wiring.py` | Передача лимитов поиска и файловой секции в мультизапросный поиск. |
| PRI-291 | `tests/mcp/test_prepare_task_context.py` | Сборка секций ответа и передача подзапросов. |
| PRI-291 | `tests/mcp/test_server.py` | Регистрация `prepare_task_context` и `search_codebase` в публичном MCP-интерфейсе. |
| PRI-291 | `tests/retrieval/test_multiquery.py` | Применение файлового лимита, квот и рендеринг фрагментов после отбора. |
| PRI-256 | `tests/eval/test_live_boundary.py` | Передача параметров файлового отбора из eval-прогона в поиск. |
| PRI-256 | `tests/mcp/test_context_limits_wiring.py` | Использование независимых настроек файлового бюджета в сервисе. |
| PRI-256 | `tests/policy/test_context_limits.py` | Значения и расчёт файлового бюджета, число фрагментов на файл. |
| PRI-256 | `tests/retrieval/test_multiquery.py` | Разнообразие по файлам, лимит путей и число фрагментов на файл. |

## Результаты

| Карточка | Тестовые пути, смешанный → только тесты | Ручная разметка, смешанный → только тесты | Пересечение с `code`, смешанный → только тесты |
| --- | --- | --- | --- |
| PRI-278 | 14/20 (70%) → 20/20 (100%) | 2/4 → 2/4 | 5 → 0 из 20 путей `code` |
| PRI-291 | 11/20 (55%) → 20/20 (100%) | 4/4 → 4/4 | 6 → 0 из 20 путей `code` |
| PRI-256 | 15/20 (75%) → 20/20 (100%) | 4/4 → 4/4 | 5 → 0 из 20 путей `code` |

Все 22 слота, занятые обычным кодом в смешанных выдачах, заполнили тестовые пути. Все тестовые пути из смешанных выдач сохранились. Для PRI‑278 обе стороны вернули `tests/eval/test_replay.py` и `tests/metrics/test_context_core.py`. Для PRI‑291 и PRI‑256 покрыты все размеченные файлы. У всех трёх полных ответов список `gaps` был пуст.

## Задержка

| Карточка | Смешанный, медиана (мин.–макс.), мс | Только тесты, медиана (мин.–макс.), мс | Медиана разностей по парам (мин.–макс.), мс | Только тесты быстрее |
| --- | ---: | ---: | ---: | ---: |
| PRI-278 | 641,2 (574,4…915,4) | 647,4 (601,0…997,1) | +0,6 (−134,1…+376,3) | 5/10 |
| PRI-291 | 453,9 (442,6…496,9) | 455,6 (423,6…461,2) | −1,9 (−39,4…+13,9) | 5/10 |
| PRI-256 | 1011,0 (570,3…1899,5) | 857,7 (580,6…1355,2) | −62,6 (−641,6…+129,6) | 6/10 |

В каждой серии задержка менялась в обе стороны. Для PRI‑278 и PRI‑291 медиана разностей близка к нулю. Для PRI‑256 медиана разностей составила −62,6 мс, а диапазон разностей оказался шире, чем для двух других карточек. Параллельно на той же машине выполнялись тесты из других рабочих деревьев. Эта нагрузка могла влиять на задержку отдельных вызовов даже при чередовании режимов. Числа описывают этот прогретый прогон в условиях общей нагрузки. Эффекты при холодном кэше и на других задачах требуют отдельных замеров.

## Пути выдачи

Общие пути и два списка различий в каждой карточке полностью задают состав обеих секций. Метка «пересечение» относится к путям, попавшим также в `code`. Порядок ранжирования в списках заменён алфавитным.

### PRI-278

**В обоих режимах (14)**

- `tests/eval/test_history.py`
- `tests/eval/test_replay.py`
- `tests/eval/test_replay_report.py`
- `tests/mcp/test_prepare_task_context.py`
- `tests/metrics/test_context_core.py`
- `tests/metrics/test_recall.py`
- `tests/policy/test_context_limits.py`
- `tests/retrieval/test_retriever_branch.py`
- `tests/retrieval/test_search_base.py`
- `tests/services/test_brief_quality.py`
- `tests/skills/test_solve_task_brief.py`
- `tests/tasks/test_service.py`
- `tests/tasks/test_service_batch.py`
- `tests/web/test_history.py`

**Только в смешанном (6)**

- `reviewer/index/store.py` — пересечение с `code`
- `reviewer/mcp/task_context.py`
- `reviewer/metrics/brief_quality/classify.py` — пересечение с `code`
- `reviewer/metrics/brief_quality/recall.py` — пересечение с `code`
- `reviewer/policy/context_limits.py` — пересечение с `code`
- `reviewer/retrieval/retriever.py` — пересечение с `code`

**Только в режиме тестов (6)**

- `tests/eval/test_forecast.py`
- `tests/index/test_store_hybrid.py`
- `tests/metrics/test_reexport_guard.py`
- `tests/retrieval/test_multiquery.py`
- `tests/skills/test_summarize_subsystems.py`
- `tests/test_rrf_k_single_source.py`

### PRI-291

**В обоих режимах (11)**

- `tests/eval/test_live_boundary.py`
- `tests/index/test_index_meta_edges.py`
- `tests/mcp/test_context_limits_wiring.py`
- `tests/mcp/test_prepare_task_context.py`
- `tests/mcp/test_server.py`
- `tests/policy/test_context_limits.py`
- `tests/retrieval/test_multiquery.py`
- `tests/retrieval/test_solve_task_isolation.py`
- `tests/services/test_brief_quality.py`
- `tests/tasks/test_service.py`
- `tests/tasks/test_sync.py`

**Только в смешанном (9)**

- `reviewer/config/branches.py`
- `reviewer/mcp/task_context.py`
- `reviewer/policy/context_limits.py` — пересечение с `code`
- `reviewer/retrieval/multiquery.py` — пересечение с `code`
- `reviewer/retrieval/retriever.py` — пересечение с `code`
- `reviewer/services/brief_quality.py` — пересечение с `code`
- `reviewer/services/repo_id.py`
- `reviewer/tasks/service.py` — пересечение с `code`
- `reviewer/web/history.py` — пересечение с `code`

**Только в режиме тестов (9)**

- `tests/entrypoints/test_cli.py`
- `tests/mcp/test_service.py`
- `tests/mcp/test_subsystem_summaries.py`
- `tests/retrieval/test_retriever_branch.py`
- `tests/skills/test_configure_review_skill.py`
- `tests/skills/test_preflight_guardrail.py`
- `tests/tasks/boards/test_attachments.py`
- `tests/tasks/test_pr_backlink.py`
- `tests/web/test_history.py`

### PRI-256

**В обоих режимах (15)**

- `tests/eval/test_history.py`
- `tests/eval/test_live_boundary.py`
- `tests/eval/test_replay.py`
- `tests/eval/test_snapshot.py`
- `tests/eval/test_variants.py`
- `tests/mcp/test_context_limits_wiring.py`
- `tests/mcp/test_prepare_task_context.py`
- `tests/metrics/test_recall.py`
- `tests/policy/test_context_limits.py`
- `tests/retrieval/test_multiquery.py`
- `tests/retrieval/test_solve_task_isolation.py`
- `tests/services/test_brief_quality.py`
- `tests/skills/test_summarize_subsystems.py`
- `tests/tasks/boards/test_attachments.py`
- `tests/tasks/test_search_codebase_integration.py`

**Только в смешанном (5)**

- `reviewer/mcp/task_context.py` — пересечение с `code`
- `reviewer/metrics/brief_quality/briefs.py` — пересечение с `code`
- `reviewer/policy/context_limits.py` — пересечение с `code`
- `reviewer/retrieval/multiquery.py` — пересечение с `code`
- `reviewer/retrieval/retriever.py` — пересечение с `code`

**Только в режиме тестов (5)**

- `tests/eval/test_forecast.py`
- `tests/eval/test_replay_report.py`
- `tests/metrics/test_context_core.py`
- `tests/retrieval/test_search_base.py`
- `tests/skills/test_solve_task_brief.py`

## Воспроизведение и проверки

Из корня проекта с установленными зависимостями и штатными настройками Reviewer, подключённого к закреплённому индексу `dev` и хранилищу карточек проекта `PRI`, запустить:

```bash
python -m eval.pri296.compare
```

Скрипт сверяет SHA индекса до и после прогона и печатает JSON с путями и измерениями. Для сравнения с отчётом нужно сверить SHA списка подзапросов каждой карточки. Карточки читаются из локального хранилища задач, поэтому редактирование карточки может изменить запрос при том же SHA индекса.

Модульный прогон `pytest -q` на реализации PRI‑296 дал 4708 пройденных, 1 пропущенный, 137 исключённых тестов и 4 предупреждения. Регрессии проверяют файлы из хвоста обычной выдачи, соседей графа от производственных вершин, штатную диагностику `(ничего не найдено)` при пустом наборе тестовых кандидатов и подключение режима к секции. Отдельная проверка публичного `include_tests=True` покрывает смешанную выдачу.
