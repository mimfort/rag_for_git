# Replay-метрики ретрива solve-task

Прогон от 2026-08-16T22:44:40.883952+00:00, репозиторий `mimfort/rag_for_git`, ветка `dev`.

## Идентичность прогона

- **до**: вариант `baseline`, коммит `9ed98f550903c9427215be5a85429dbb7de1a904`, indexed_sha `37518042b5c7666bebf7d1969c2f4cfd82829ac0`, корпус 55
- **после**: вариант `limits`, параметры `{'search_codebase': {'ceiling': 25, 'candidate_pool': 50}}`, коммит `9ed98f550903c9427215be5a85429dbb7de1a904`, indexed_sha `37518042b5c7666bebf7d1969c2f4cfd82829ac0`, корпус 55

## Агрегат

| Метрика | до | после | Δ |
|---|---|---|---|
| core-recall (медиана) | 0.225 | 0.225 | +0 |
| core-recall (среднее) | 0.2773 | 0.2484 | -0.02887 |
| core-recall bulk (ядро ≥ 10) | 0.127 | 0.09127 | -0.03571 |
| bulk N | 4 | 4 | +0 |
| precision (медиана) | 0.875 | 1 | +0.125 |
| предсказано файлов (медиана) | 2 | 2 | +0 |
| задач измерено | 40 | 40 | +0 |
| без точки измерения | 0 | 0 | +0 |

## Статусы задач

| Статус | Задач |
|---|---|
| measured | 40 |
| empty_core_denominator | 9 |
| no_ground_truth | 6 |
| task_not_in_store | 0 |
| retrieval_failed | 0 |

## Дельта по задачам

| Ключ | Статус | до | после | Δ | приобретено | потеряно |
|---|---|---|---|---|---|---|
| PRI-241 | measured | 1 | 0.5 | -0.5 | `reviewer/config/settings.py` | `reviewer/entrypoints/cli.py` |
| PRI-252 | measured | 0.6667 | 0.3333 | -0.3333 | `reviewer/graph/inherit.py` | `reviewer/entrypoints/cli.py` |
| PRI-173 | measured | 0.5 | 0.25 | -0.25 | — | `reviewer/mcp/service.py` |
| PRI-215 | measured | 0.1429 | 0.07143 | -0.07143 | — | `reviewer/tasks/boards/__init__.py` |
| PRI-162 | empty_core_denominator | — | — | — | `reviewer/mcp/task_context.py` | — |
| PRI-164 | empty_core_denominator | — | — | — | `reviewer/tasks/boards/asana.py`, `reviewer/tasks/boards/yougile.py` | — |
| PRI-176 | empty_core_denominator | — | — | — | `reviewer/metrics/brief_quality/classify.py` | — |
| PRI-208 | empty_core_denominator | — | — | — | `reviewer/entrypoints/mcp_server.py` | `reviewer/mcp/service.py` |
| PRI-212 | measured | 0.5 | 0.5 | +0 | `reviewer/mcp/session_store.py` | `reviewer/mcp/service.py` |
| PRI-217 | measured | 0.25 | 0.25 | +0 | — | `reviewer/tasks/boards/__init__.py`, `reviewer/tasks/boards/base.py`, `reviewer/tasks/boards/github.py` |
| PRI-177 | measured | 0 | 0 | +0 | `reviewer/web/history.py` | `reviewer/mcp/service.py` |
| PRI-220 | empty_core_denominator | — | — | — | `plugin/hooks/reviewer_defect.py`, `reviewer/install.py` | — |
| PRI-178 | measured | 1 | 1 | +0 | `reviewer/retrieval/cliff.py` | `reviewer/mcp/service.py` |
| PRI-227 | measured | 0 | 0 | +0 | — | `reviewer/config/settings.py`, `scripts/update_codex_plugin_manifest.py` |
| PRI-222 | measured | 0 | 0 | +0 | — | `reviewer/entrypoints/mcp_server.py` |
| PRI-243 | measured | 0 | 0 | +0 | `reviewer/tasks/taskdoc.py` | — |
| PRI-245 | measured | 0.3333 | 0.3333 | +0 | — | `reviewer/tools/code_tools.py` |
| PRI-249 | measured | 0.3333 | 0.3333 | +0 | `reviewer/web/api.py` | `reviewer/web/history.py` |
| _и ещё 37_ | без изменений | — | — | — | — | — |

## Оговорка

Линия `replay` и линия `snapshot` **несравнимы напрямую**: snapshot считает пути, которые отобрала LLM из выдачи ретрива, а replay — всю выдачу ретрива. Сравнивать можно только replay с replay.
