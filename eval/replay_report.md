# Replay-метрики ретрива solve-task

Прогон от 2026-08-17T20:35:48.511514+00:00, репозиторий `mimfort/rag_for_git`, ветка `dev`.

## Идентичность прогона

- **до**: вариант `similar_paths`, коммит `79e5fafe7570186747dadfbae08f37386e25e12e`, indexed_sha `951e791246db4577a3913d07b75001bc32ff9969`, корпус 59
- **после**: вариант `similar_paths+subsystem_paths`, коммит `79e5fafe7570186747dadfbae08f37386e25e12e`, indexed_sha `951e791246db4577a3913d07b75001bc32ff9969`, корпус 59

## Агрегат

| Метрика | до | после | Δ |
|---|---|---|---|
| core-recall (медиана) | 0.75 | 0.6667 | -0.08333 |
| core-recall (среднее) | 0.6602 | 0.6388 | -0.02144 |
| core-recall bulk (ядро ≥ 10) | 0.3889 | 0.3333 | -0.05556 |
| bulk N | 5 | 5 | +0 |
| precision (медиана) | 0.3333 | 0.3333 | +0 |
| предсказано файлов (медиана) | 12 | 12 | +0 |
| задач измерено | 43 | 43 | +0 |
| без точки измерения | 0 | 0 | +0 |

## Статусы задач

| Статус | Задач |
|---|---|
| measured | 43 |
| empty_core_denominator | 10 |
| no_ground_truth | 6 |
| task_not_in_store | 0 |
| retrieval_failed | 0 |

## Дельта по задачам

| Ключ | Статус | до | после | Δ | приобретено | потеряно |
|---|---|---|---|---|---|---|
| PRI-249 | measured | 1 | 0.6667 | -0.3333 | `reviewer/agent/assemble.py`, `reviewer/services/branch.py` | `reviewer/entrypoints/mcp_server.py`, `reviewer/web/history.py` |
| PRI-255 | measured | 1 | 0.6667 | -0.3333 | `reviewer/index/_retry.py`, `reviewer/retrieval/cliff.py` | `reviewer/index/embeddings.py`, `reviewer/tasks/boards/attachments.py` |
| PRI-217 | measured | 0.25 | 0.5 | +0.25 | `reviewer/tasks/boards/markup.py`, `reviewer/tasks/boards/registry.py` | `reviewer/tasks/boards/base.py`, `reviewer/tasks/boards/youtrack.py` |
| PRI-213 | measured | 0.8571 | 0.7143 | -0.1429 | `reviewer/tasks/boards/jira.py`, `reviewer/tasks/boards/trello.py` | `reviewer/config/settings.py`, `reviewer/mcp/service.py` |
| PRI-221 | measured | 0.7778 | 0.6667 | -0.1111 | `reviewer/config/fetch_errors.py`, `reviewer/config/provider_access.py` | `reviewer/graph/scip.py`, `reviewer/mcp/session_serde.py` |
| PRI-196 | measured | 0.8 | 0.7 | -0.1 | `reviewer/tasks/boards/jira.py`, `reviewer/tasks/boards/setup.py` | `reviewer/tasks/boards/trello.py`, `reviewer/tasks/service.py` |
| PRI-223 | measured | 0.4 | 0.32 | -0.08 | `reviewer/config/fetch_errors.py`, `reviewer/policy/policy.py` | `reviewer/config/onboarding.py`, `reviewer/entrypoints/mcp_server.py` |
| PRI-215 | measured | 0.3571 | 0.2857 | -0.07143 | `reviewer/tasks/boards/github.py`, `reviewer/tasks/boards/yandex_tracker.py` | `reviewer/entrypoints/cli.py`, `reviewer/tasks/boards/kaiten.py` |
| PRI-164 | empty_core_denominator | — | — | — | `reviewer/tasks/boards/__init__.py`, `reviewer/tasks/boards/adf.py` | `reviewer/services/brief_quality.py`, `reviewer/tasks/boards/asana.py` |
| PRI-176 | empty_core_denominator | — | — | — | `plugin/hooks/brief_post_write.py`, `reviewer/tasks/boards/__init__.py` | — |
| PRI-202 | measured | 0.6667 | 0.6667 | +0 | `reviewer/index/_retry.py`, `reviewer/index/chunker.py` | `reviewer/metrics/brief_quality/classify.py`, `reviewer/services/review_service.py` |
| PRI-203 | empty_core_denominator | — | — | — | `reviewer/mcp/schemas.py`, `reviewer/mcp/session_serde.py` | `reviewer/config/settings.py`, `reviewer/entrypoints/cli.py` |
| PRI-205 | measured | 1 | 1 | +0 | `reviewer/tasks/boards/__init__.py`, `reviewer/tasks/boards/adf.py` | `reviewer/policy/policy.py` |
| PRI-206 | empty_core_denominator | — | — | — | `reviewer/tasks/boards/linear.py`, `reviewer/tasks/boards/youtrack.py` | `reviewer/entrypoints/mcp_server.py`, `reviewer/vcs/github.py` |
| PRI-207 | measured | 1 | 1 | +0 | `reviewer/tasks/boards/adf.py`, `reviewer/tasks/boards/attachments.py` | `reviewer/mcp/service.py`, `reviewer/policy/policy.py` |
| PRI-208 | empty_core_denominator | — | — | — | `reviewer/mcp/schemas.py`, `reviewer/mcp/session_serde.py` | `reviewer/config/layers.py`, `reviewer/mcp/service.py` |
| PRI-210 | measured | 1 | 1 | +0 | `reviewer/app.py`, `reviewer/gitutil.py` | `scripts/verify_launcher_distribution.py` |
| PRI-179 | measured | 0.75 | 0.75 | +0 | `reviewer/graph/summaries.py`, `reviewer/tools/code_tools.py` | `reviewer/graph/family.py`, `reviewer/graph/scip.py` |
| PRI-212 | measured | 1 | 1 | +0 | `reviewer/mcp/schemas.py`, `reviewer/services/branch.py` | `plugin/hooks/reviewer_defect.py`, `reviewer/index/reranker.py` |
| PRI-172 | measured | 0 | 0 | +0 | `plugin/hooks/brief_cost.py`, `reviewer/agent/centrality.py` | — |
| PRI-173 | measured | 0.75 | 0.75 | +0 | `reviewer/tasks/boards/__init__.py`, `reviewer/tasks/boards/adf.py` | — |
| PRI-177 | measured | 0 | 0 | +0 | `reviewer/tasks/boards/__init__.py`, `reviewer/tasks/boards/adf.py` | — |
| PRI-219 | measured | 0.5 | 0.5 | +0 | `reviewer/services/branch.py`, `reviewer/services/repo_id.py` | `reviewer/retrieval/retriever.py`, `reviewer/services/gc.py` |
| PRI-134 | measured | 0.75 | 0.75 | +0 | `reviewer/config/committed.py`, `reviewer/config/settings.py` | `reviewer/index/embeddings.py`, `reviewer/tasks/boards/clickup.py` |
| PRI-178 | measured | 1 | 1 | +0 | `reviewer/index/_retry.py`, `reviewer/index/chunker.py` | `reviewer/entrypoints/mcp_server.py`, `reviewer/tasks/boards/adf.py` |
| PRI-225 | measured | 0.3889 | 0.3889 | +0 | `reviewer/config/branches.py`, `reviewer/tasks/boards/linear.py` | `reviewer/config/settings.py`, `reviewer/tasks/boards/github.py` |
| PRI-227 | measured | 0 | 0 | +0 | `reviewer/app.py`, `reviewer/gitutil.py` | `reviewer/config/layers.py`, `reviewer/tasks/boards/github.py` |
| PRI-222 | measured | 1 | 1 | +0 | `reviewer/entrypoints/launcher.py`, `reviewer/web/api.py` | `reviewer/services/repo_id.py`, `reviewer/web/history.py` |
| PRI-234 | measured | 1 | 1 | +0 | `reviewer/config/onboarding.py`, `reviewer/config/provider_access.py` | `reviewer/compose_lifecycle.py`, `reviewer/services/brief_quality.py` |
| PRI-228 | measured | 1 | 1 | +0 | `reviewer/index/_retry.py`, `reviewer/index/chunker.py` | `reviewer/entrypoints/mcp_server.py`, `reviewer/retrieval/multiquery.py` |
| PRI-235 | measured | 1 | 1 | +0 | `reviewer/config/branches.py`, `reviewer/config/fetch_errors.py` | `reviewer/mcp/task_context.py`, `reviewer/tasks/boards/weeek.py` |
| PRI-236 | measured | 1 | 1 | +0 | `reviewer/install_claude.py`, `reviewer/install_codex.py` | `reviewer/graph/family.py`, `reviewer/graph/store.py` |
| PRI-237 | measured | 0 | 0 | +0 | `reviewer/agent/assemble.py`, `reviewer/agent/centrality.py` | `reviewer/graph/summaries.py` |
| PRI-238 | measured | 0.6667 | 0.6667 | +0 | `reviewer/tasks/boards/github.py`, `reviewer/tasks/boards/weeek.py` | `reviewer/install.py`, `reviewer/tasks/pr_backlink.py` |
| PRI-239 | measured | 0.5 | 0.5 | +0 | `reviewer/tasks/boards/__init__.py`, `reviewer/tasks/boards/adf.py` | `reviewer/app.py`, `reviewer/web/history.py` |
| PRI-241 | measured | 1 | 1 | +0 | `reviewer/app.py`, `reviewer/gitutil.py` | `reviewer/metrics/brief_quality/briefs.py`, `reviewer/web/serve.py` |
| PRI-242 | measured | 0.5 | 0.5 | +0 | `reviewer/app.py`, `reviewer/update_lifecycle.py` | `reviewer/graph/store.py`, `scripts/verify_launcher_distribution.py` |
| PRI-243 | measured | 0 | 0 | +0 | `reviewer/tasks/boards/github.py`, `reviewer/tasks/boards/setup.py` | `reviewer/services/brief_quality.py`, `reviewer/services/repo_id.py` |
| PRI-246 | empty_core_denominator | — | — | — | `plugin/hooks/brief_post_write.py`, `reviewer/tasks/boards/__init__.py` | `plugin/hooks/brief_guard.py`, `reviewer/tasks/boards/youtrack.py` |
| PRI-250 | measured | 0.5 | 0.5 | +0 | `plugin/hooks/brief_guard.py`, `reviewer/tasks/boards/asana.py` | `reviewer/agent/assemble.py`, `reviewer/web/api.py` |
| PRI-251 | measured | 0.2857 | 0.2857 | +0 | `reviewer/agent/assemble.py`, `reviewer/policy/policy.py` | `reviewer/retrieval/retriever.py`, `reviewer/tasks/boards/clickup.py` |
| PRI-252 | measured | 1 | 1 | +0 | `reviewer/graph/builder.py`, `reviewer/index/refs.py` | `reviewer/graph/store.py`, `reviewer/tasks/boards/youtrack.py` |
| PRI-247 | measured | 0.6667 | 0.6667 | +0 | `reviewer/install_codex.py`, `reviewer/mcp/schemas.py` | `reviewer/tasks/boards/base.py`, `scripts/update_codex_plugin_manifest.py` |
| PRI-248 | measured | 0.3333 | 0.3333 | +0 | `reviewer/tasks/boards/__init__.py`, `reviewer/tasks/boards/adf.py` | `reviewer/retrieval/retriever.py`, `reviewer/tools/code_tools.py` |
| PRI-256 | measured | 0.8 | 0.8 | +0 | `reviewer/index/embeddings.py`, `reviewer/index/store.py` | `reviewer/entrypoints/mcp_server.py`, `reviewer/metrics/brief_quality/recall.py` |
| PRI-257 | measured | 0.3333 | 0.3333 | +0 | `reviewer/agent/assemble.py`, `reviewer/agent/centrality.py` | `reviewer/bugreport/render.py`, `reviewer/metrics/brief_quality/briefs.py` |
| _и ещё 13_ | без изменений | — | — | — | — | — |

## Оговорка

Линия `replay` и линия `snapshot` **несравнимы напрямую**: snapshot считает пути, которые отобрала LLM из выдачи ретрива, а replay — всю выдачу ретрива. Сравнивать можно только replay с replay.

## Приёмка PRI-255

Мультизапрос с RRF-слиянием: секция `code` контекста задачи ищется набором подзапросов,
извлечённых из структуры задачи, вместо одного запроса на весь её текст.

### Критерий 1 — распределение числа подзапросов производно от размера задачи

`python -m eval.solve_task_metrics subqueries --repo mimfort/rag_for_git --branch dev`,
корпус 56 задач:

| класс задачи | задач | медиана подзапросов | мин | макс |
|---|---|---|---|---|
| мелкая (≤10 строк) | 7 | 2 | 1 | 2 |
| средняя (11-30) | 19 | 11 | 2 | 18 |
| развёртка (>30) | 30 | 14 | 2 | 20 |

Не константа: 2 против 14 между крайними классами. Предохранитель `MAX_SUBQUERIES = 20`
срабатывает на самых крупных задачах (макс = 20 ровно).

### Критерий 2 — дельта bulk core-recall в replay

Обе стороны сняты в одном прогоне, на одном `indexed_sha` (`a1b28c1`) и одном коммите
(`a87b468`), корпус 56, измерено 40 задач:

| Метрика | до (`baseline`) | после (`multiquery`) | Δ |
|---|---|---|---|
| core-recall (медиана) | 0.225 | 0.3333 | **+0.1083** |
| core-recall bulk (ядро ≥ 10) | 0.1548 | 0.1825 | **+0.02778** |
| bulk N | 4 | 4 | — |
| precision (медиана) | 0.875 | 0.5 | −0.375 |
| предсказано файлов (медиана) | 2 | 4 | +2 |

Дельта положительна по обеим линиям recall. Три оговорки, без которых число читается неверно:

1. **Baseline здесь 0.1548, а не 0.127 из прошлого отчёта.** Прошлая цифра снята на другом
   корпусе (55 задач) и другом `indexed_sha`; сравнивать между отчётами нельзя, сравнивать
   можно только две стороны одного прогона — они и приведены.
2. **`bulk N = 4`.** Bulk-дельта опирается на четыре задачи, то есть её доверительный
   интервал широк. Медианная дельта (+0.1083, N=40) — куда более надёжный сигнал того, что
   рычаг работает.
3. **Precision упала с 0.875 до 0.5, число файлов выросло с 2 до 4.** Это ожидаемая цена, а
   не побочный дефект: механизм потери был именно в том, что выдача сжималась до двух файлов.
   Отбор нужного из четырёх — работа LLM-сборщика брифа, а вот отсутствующего в выдаче файла
   он не восстановит никак.

Не все задачи выиграли: `PRI-217` (11 адаптеров досок) просела на −0.25, `PRI-248`/`PRI-249`
на −0.3333. Разбор просадок в скоуп PRI-255 не входит — соседние рычаги той же программы
(ID-310 файловая диверсификация, ID-311 diff-пути похожих задач) остаются впереди.

### Критерий 3 — файлы, найденные только хвостовым подзапросом

Замер воспроизводится так (подкоманды у него нет — разовая проверка, живой Voyage, несколько
query-эмбеддингов):

```python
from eval.solve_task_metrics import live
from eval.solve_task_metrics.context_paths import extract_context_paths
from reviewer.mcp.subqueries import build_subqueries

provider, repo, branch = live.open_live("mimfort/rag_for_git", "dev")
with provider:
    for key in ("PRI-217", "PRI-222"):
        task = provider.task(key)
        queries = build_subqueries(task, provider.query(task, key))
        head = extract_context_paths(provider.code_multi(repo, branch, queries[:1], None))
        full = extract_context_paths(provider.code_multi(repo, branch, queries, None))
        print(key, len(queries), sorted(full - head))
```

Задачи-развёртки, сравнение выдачи по одному `q0` против полного набора подзапросов:

| Задача | подзапросов | только от `q0` | доехало только от хвостовых |
|---|---|---|---|
| PRI-217 | 20 | 4 файла | `reviewer/tasks/boards/clickup.py`, `reviewer/tasks/boards/kaiten.py` |
| PRI-222 | 12 | 5 файлов | `reviewer/compose_lifecycle.py`, `reviewer/entrypoints/cli.py`, `reviewer/web/app.py` |

Множество непустое на обеих задачах, и это именно те файлы, которых однозапросная выдача
не давала вовсе. У `PRI-222` два из трёх (`reviewer/entrypoints/cli.py`,
`reviewer/compose_lifecycle.py`) входят в её фактический diff — то есть хвостовой подзапрос
принёс не шум, а ядро.

### Критерий 4 — токены LLM не растут

Извлечение подзапросов детерминированное (`reviewer/mcp/subqueries.py`, только `re`), LLM в
нём не участвует. Эмбеддинги: один батч-вызов на сборку секции
(`tests/retrieval/test_multiquery.py::test_one_batched_embedding_call_per_assembly`).
Рост Voyage-эмбеддингов задачей разрешён явно, рост LLM-токенов — нет, и его нет.

## Приёмка PRI-256

Файловый бюджет секции `code`: выдача ограничивается числом РАЗЛИЧНЫХ файлов
(`context_limits.code_section`, дефолт `max_files=12`, `max_chunks_per_file=1`,
`chars_per_file=1300`), а не арифметическим следствием символьного потолка.

### Дельта в replay

Обе стороны сняты в одном прогоне, на одном `indexed_sha` (`7d66a08`), одном коммите
(`25a7aa9`) и одном корпусе (57 задач, измерено 41), 2026-08-17:

| Метрика | до (бюджет 4×1×2000) | после (12×1×1300) | Δ |
|---|---|---|---|
| различных файлов секции (медиана) | 4 | 12 | **+8** |
| объём секции, символов (медиана) | 6412 | 13660 | **+7248 (×2.13)** |
| core-recall (медиана) | 0.2857 | 0.5 | **+0.2143** |
| core-recall bulk (ядро ≥ 10) | 0.1911 | 0.3544 | **+0.1633** |
| bulk N | 4 | 4 | — |
| precision (медиана) | 0.5 | 0.25 | −0.25 |

Обе линии recall выросли: медианная (N=41) и bulk (N=4). Precision упала ровно так, как
ожидалось от расширения выдачи, — отбор нужного из двенадцати остаётся работой сборщика
брифа, а отсутствующего в выдаче файла он не восстановит.

### Критерий «рост объёма не кратен росту числа файлов»

Число файлов выросло втрое (4 → 12), объём секции — в 2.13 раза по медиане (6412 → 13660
символов; по среднему 6479 → 13408, то есть ×2.07). Кратного роста нет: доля символов на
файл сокращена с 2000 до 1300, поэтому втрое больше файлов стоят вдвое больше символов.
Замер объёма — все 57 задач корпуса, обе стороны в одном процессе; он разовый, сырой вывод
не трекается, воспроизводится сниппетом ниже.

Цифра односторонняя и это надо знать: измерена секция `code`, но те же лимиты действуют и на
секцию `test_exemplars` — обе идут через `_search_codebase_multi`. Верхняя оценка роста всего
payload `prepare_task_context` — тот же множитель по обеим секциям, то есть примерно вдвое
больше измеренного прироста в худшем случае. Отдельного замера `test_exemplars` не снималось:
он стоил бы второго живого прохода по корпусу, а решения не менял бы — лимит один на обе секции.

### Три оговорки, без которых числа читаются неверно

1. **Baseline — «4 файла» из раздела «Приёмка PRI-255», а не «5-6» из описания тикета.**
   Цифра «5-6» ни одним отчётом этого репозитория не подтверждается и, судя по всему,
   предшествует замеру PRI-255. Сверять приёмку с ней значило бы мерить две стороны
   разными линейками.
2. **Сторона «до» эмулирована оверрайдом `code_section` 4×1×2000, а не старым кодом.**
   Это воспроизводит прежнюю арифметическую стену `max_tool_result_chars 8000 ÷
   MAX_BLOCK_CHARS 2000 = 4 блока`. Эмуляция великодушна к baseline: старый путь мог
   потратить слоты на несколько чанков ОДНОГО файла и дать меньше четырёх различных
   файлов. Измеренная дельта, таким образом, — нижняя оценка.
3. **`indexed_sha` не тот, что у PRI-255.** Тот замер снят на `a1b28c1`, индекс с тех пор
   перестроен на `7d66a08`, а корпус вырос с 56 задач до 57. Поэтому числа PRI-256 не
   сравниваются с числами PRI-255 напрямую: сравниваются только две стороны ЭТОГО прогона
   (у обеих `indexed_sha`, коммит и корпус совпадают — предупреждений о несопоставимости
   в отчёте нет).

### Процедура воспроизведения

**Прежде чем запускать: `replay` перезаписывает `eval/replay_report.md` целиком** — автоген
(шапка, идентичность прогона, агрегат, дельта по задачам) затирает и разделы приёмки, включая
этот. Сохрани файл (`git show HEAD:eval/replay_report.md > /tmp/report.md`) и верни разделы
приёмки на место после прогона.

Сторона «до» и сторона «после», два прогона подряд (второй берёт первый как baseline):

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant multiquery \
  --repo mimfort/rag_for_git --branch dev --baseline multiquery \
  --set code_section.max_files=4 --set code_section.max_chunks_per_file=1 \
  --set code_section.chars_per_file=2000
.venv/bin/python -m eval.solve_task_metrics replay --variant multiquery \
  --repo mimfort/rag_for_git --branch dev --baseline last
```

Объём секции в символах в снимок replay не входит (снимок хранит пути, не тексты), поэтому
он считается отдельно — обе стороны в одном процессе, чтобы LRU эмбеддера не удваивал
расход Voyage:

```python
import pathlib
import statistics

from eval.solve_task_metrics import live, replay as replay_mod
from eval.solve_task_metrics.context_paths import extract_context_paths
from reviewer.mcp.subqueries import build_subqueries

BEFORE = {"code_section": {"max_files": 4, "max_chunks_per_file": 1,
                           "chars_per_file": 2000}}
rows = []
provider, repo, branch = live.open_live("mimfort/rag_for_git", "dev")
with provider:
    for key in replay_mod.corpus_keys(pathlib.Path("docs/superpowers/briefs")):
        task = provider.task(key)
        queries = build_subqueries(task, provider.query(task, key))
        before = provider.code_multi(repo, branch, queries, BEFORE)
        after = provider.code_multi(repo, branch, queries, None)
        rows.append((len(before), len(after),
                     len(extract_context_paths(before)),
                     len(extract_context_paths(after))))
        print(key, rows[-1], flush=True)
for i, name in enumerate(("символов до", "символов после", "файлов до", "файлов после")):
    print(name, statistics.median([r[i] for r in rows]))
```

Оверрайд `code_section` через `--set` появился вместе с этим замером (PRI-256): без него
раздел не сериализовался в `limits_to_yaml` и молча терялся в `LiveRetrieval.code_multi`,
то есть сторону «до» нечем было бы выразить.

## Приёмка PRI-257

Все прогоны — на одном `indexed_sha=951e791`, корпус 42 измеренных задачи, сторона «до» —
вариант `multiquery`.

### Критерий 1 — дельта по каждому рычагу отдельно

| Вариант | медиана core-recall | Δ | bulk | precision (медиана) |
|---|---|---|---|---|
| `multiquery` (до) | 0.5 | — | 0.3730 | 0.167 |
| `similar_paths` | **0.75** | **+0.25** | 0.3944 | 0.333 |
| `cochange` | 0.5778 | +0.078 | 0.3544 | 0.250 |
| `augmented` (оба) | 0.75 | +0.25 | 0.3944 | 0.333 |

Вклад по задачам:

| Рычаг | задач с добавлением | добавлено путей | из них в ядре | вытеснено core | recall вырос / упал |
|---|---|---|---|---|---|
| similar-diffs | 22 | 35 | 28 | 1 | 17 / 0 |
| co-change | 30 | 34 | 4 | 1 | 4 / 1 |
| оба | 23 | 39 | 29 | 1 | 18 / 0 |

**Вердикт: similar-diffs смержен, co-change снят.** Точность сигнала similar-diffs — 28 попаданий
на 35 подмешанных путей (80 %), ни одной задачи с падением recall, precision выдачи вырос вдвое:
подмешивание уплотняет выдачу, а не размывает её. co-change даёт 12 % точности, роняет bulk и
поверх similar-diffs не добавляет ничего — строки `cochange`/`augmented` в таблице сохранены как
свидетельство замера, самих вариантов в реестре больше нет.

### Три вещи, без которых числа читаются неверно

**Первые три замера дали ровно ноль, и ни один из них не был свойством сигнала.** Дельта появилась
только после снятия трёх независимых механизмов, каждый из которых обнулял рычаг в одиночку:
квота работала потолком на остаток файлового бюджета (гибрид забирал все 12 слотов, augmented
доставался 0-1); известность кандидата считалась по сырому пулу ретрива, из-за чего выбрасывался
ровно тот файл, который гибрид нашёл, но ранжировал слишком низко для выдачи, — то есть основной
случай, ради которого рычаг и делался; квота тратилась на кандидатов до проверки наличия чанков,
а списки начинаются с `docs/*`, `README`, `*.jsonl`, у которых чанков нет вовсе.

**Табличный источник в этом замере не участвовал.** `brief_quality` на момент прогона пуста
(0 строк), поэтому весь измеренный эффект даёт git-фолбэк по ключу задачи. С накоплением истории
прогонов основной источник добавится к измеренному, а не заменит его.

**Дельта измерена против `multiquery`, а не против `baseline`.** Сторона «до» — уже улучшенный
PRI-255/256 путь; выигрыш +0.25 медианы получен поверх него.

Контрольный прогон после фикса финального ревью (перенос фильтра тестовых путей внутрь квоты,
коммит `066912c`) повторил числа в точности: медиана 0.75, bulk 0.3944 на том же `indexed_sha`.
Совпадение ожидаемо — на этом корпусе квота ни разу не выгорала на тестовых путях; фикс закрывает
случай, который корпус не содержит, а не меняет измеренный эффект.

### Процедура воспроизведения

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant multiquery --repo mimfort/rag_for_git --branch dev
.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths --repo mimfort/rag_for_git --branch dev --baseline multiquery
```

`replay` перезаписывает этот файл целиком: разделы приёмки прошлых задач восстанавливаются после
прогона вручную.

## Приёмка PRI-258

Все прогоны — на одном `indexed_sha=951e791246db4577a3913d07b75001bc32ff9969` (та же ветка и тот
же индекс, что в приёмке PRI-257), корпус 43 измеренных задачи, сторона «до» — вариант
`similar_paths` (продакшн-конфигурация после мержа PRI-257).

### Критерий 1 — дельта разворота кластеров относительно `similar_paths`

| Вариант | медиана core-recall | Δ | bulk | precision (медиана) |
|---|---|---|---|---|
| `similar_paths` (до) | 0.75 | — | 0.3889 | 0.333 |
| `subsystem_paths` (изолированно, similar-diffs выключен) | 0.5 | -0.25 | 0.32 | 0.250 |
| `similar_paths+subsystem_paths` (оба) | 0.6667 | **-0.0833** | 0.3333 | 0.333 |

Обе метрики core-recall (медиана и bulk) у продакшн-конфигурации `similar_paths+subsystem_paths`
**ниже**, чем у `similar_paths` без разворота: -0.0833 медианы, -0.0556 bulk. Изолированный
`subsystem_paths` (similar-diffs выключен полностью, разворот кластеров — единственный источник
подмешивания) проседает ещё сильнее: -0.25 медианы.

Вклад по задачам (гружёный и вытесненный набор путей при включении разворота кластеров поверх
`similar_paths`):

| Сравнение | задач с добавлением | добавлено путей | из них в ядре | вытеснено путей | из них было ядро (`similar_paths` находил) | recall вырос / упал / без изменений |
|---|---|---|---|---|---|---|
| `subsystem_paths` vs `similar_paths` | 48 | 96 | 4 (4 %) | 86 | 30 | 0 / 17 / 26 |
| `similar_paths+subsystem_paths` vs `similar_paths` | 46 | 92 | 2 (2 %) | 81 | 9 | 1 / 7 / 35 |

Задачи с падением recall (`similar_paths` → `similar_paths+subsystem_paths`):

| Ключ | до | после | Δ |
|---|---|---|---|
| PRI-249 | 1.0 | 0.6667 | -0.3333 |
| PRI-255 | 1.0 | 0.6667 | -0.3333 |
| PRI-213 | 0.8571 | 0.7143 | -0.1429 |
| PRI-196 | 0.8 | 0.7 | -0.1 |
| PRI-221 | 0.7778 | 0.6667 | -0.1111 |
| PRI-223 | 0.4 | 0.32 | -0.08 |
| PRI-215 | 0.3571 | 0.2857 | -0.0714 |

Единственная задача с ростом recall прибавила +0.25 медианы (пренебрежимо на фоне семи падений).

**Вердикт: рычаг снимается** — 2 % точность подмешанных путей, вытеснение core-хитов (9 из 92)
и просадка recall у 7 задач против 1 выросшей перевешивают потенциальный выигрыш; механика
бюджета проверена и не виновата (см. ниже).

### Механика бюджета проверена — сигнал доезжает до секции

Перед интерпретацией числа проверено ровно то, о чём предупреждает PRI-257 (три ложных нуля,
вызванных механикой бюджета, а не свойством сигнала): нота видимости `— подмешано N файлов:
subsystems N (квота Q)` в сырой выдаче секции `code` подтверждает, что файлы разворота реально
доезжают до неё. Ручная проверка на первых шести задачах корпуса (вариант `subsystem_paths`,
изолированно): 5 из 6 задач показали `— подмешано 2 файлов: subsystems 2 (квота 2)` — квота
`max_subsystem_files=2` выбирается целиком; одна задача (`PRI-162`) не дала ноты вовсе (пустое
пересечение сводок с известными путями — штатный случай отсутствия сигнала, а не сбой механики).
Таким образом низкий core-recall варианта — свойство самого сигнала (файлы релевантного кластера
сводок часто не совпадают с ядром правки конкретной задачи), а не артефакт квоты/известности/
файлового бюджета, как было у трёх первых замеров co-change в PRI-257.

### Что показывают числа

**Разворот кластеров вытесняет core-файлы, которые `similar_paths` находил сам.** У совместного
варианта 9 из 81 вытесненных путей были core-хитами `similar_paths` — то есть каждый девятый файл,
занятый разворотом кластеров, отобрал слот у файла, который гибрид+similar-diffs уже держали в
ядре выдачи. У изолированного `subsystem_paths` эта доля ещё выше — 30 core-хитов из 86
вытесненных.

**Точность добавленных путей крайне низкая.** Из 92 путей, добавленных разворотом кластеров
поверх `similar_paths`, в ядро попали только 2 (2 %) — против 80 % у similar-diffs при приёмке
PRI-257. Кластер сводки подсистемы описывает состав подсистемы в целом, а не файлы, которые нужно
менять под конкретную задачу; релевантность по ANN-близости запроса к сводке не эквивалентна
релевантности конкретных файлов состава.

**Дельта отрицательна и по медиане, и по bulk одновременно** — не единичный выброс распределения:
7 задач потеряли recall против 1 выигравшей, падения сосредоточены на задачах, где similar-diffs
и без того давал полное покрытие (`PRI-249`, `PRI-255`: 1.0 → 0.6667) — разворот кластеров вытеснил
уже найденные core-файлы файлами состава подсистемы.

### Процедура воспроизведения

```bash
.venv/bin/python -m eval.solve_task_metrics replay --variant similar_paths --repo mimfort/rag_for_git --branch dev
.venv/bin/python -m eval.solve_task_metrics replay --variant subsystem_paths --repo mimfort/rag_for_git --branch dev --baseline similar_paths
.venv/bin/python -m eval.solve_task_metrics replay --variant "similar_paths+subsystem_paths" --repo mimfort/rag_for_git --branch dev --baseline similar_paths
```

`replay` перезаписывает этот файл целиком: разделы приёмки прошлых задач восстанавливаются после
прогона вручную.
