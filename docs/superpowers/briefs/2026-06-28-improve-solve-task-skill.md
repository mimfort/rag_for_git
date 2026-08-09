# Brief — Улучшения solve-task skill

## Task — 10 идей улучшения solve-task skill
Пользователь просит придумать 10 улучшений текущего solve-task skill. Критерии: не оверинженерить, не ухудшать, либо не сильно повышать стоимость, либо давать значительный прирост качества. Каждую идею нужно проверить на то, что она уже не реализована. После выдачи идей пользователь выберет, какие оставить, и запишет их на доску.

## Related work — ≤3 задачи
- ID-163 / PRI-163 — solve-task: персистить solution brief в артефакт (уже реализовано в `docs/superpowers/briefs/`).
- ID-146 / PRI-146 — solve-task: спека brief + ранговый relevance-фильтр (уже реализовано в `plugin/skills/solve-task/SKILL.md`).
- ID-161 / PRI-161 — solve-task: приор подсистемных сводок перед сбором кода (уже реализовано).
(dropped 2: ID-162, ID-164 — уже реализованы в текущем SKILL.md, поэтому не несут новых идей.)

## Subsystems — ≤8 relevant
- reviewer/tasks — индексация, поиск и граф задач из досок.
- tests/skills — guardrail-тесты определений скиллов.
- reviewer/mcp — сервисный слой MCP-сервера; tools search_codebase/search_tasks/get_task_context.
- reviewer/retrieval — hybrid search + graph expansion.
- reviewer/services — ReviewService, статус, ветки, граф.
- tests/tasks — тесты пайплайна задач.
- tests/hooks — тесты PostToolUse-хуков (brief_cost).
- tests/install — тесты установки скиллов.

## Relevant code — ≤5 files/symbols
- `plugin/skills/solve-task/SKILL.md:6-233` — сам скилл, pipeline 0-5.
- `plugin/hooks/brief_cost.py:29-44` — хук расчёта токенов брифа.
- `tests/skills/test_solve_task_brief.py:13-58` — guardrail-тесты формата брифа.
- `reviewer/mcp/service.py:396-417` — `search_codebase`, базовый retrieval для solve-task.
- `reviewer/tasks/service.py:211-243` — `search_tasks` / `get_task_context`.

## Constraints / open questions
- Индекс `main` переиндексирован прямо сейчас (`drift=0`, 1691 chunks, 1735 graph nodes, SCIP). Ветка `dev` при старте отставала на 18 коммитов, но solve-task использует первичную ветку, поэтому gap закрыт.
- `sync_board(PRI)` — 62 задачи, 0 изменений; корпус задач тёплый.
- auto permission mode активен; подтверждение на reindex не запрашивалось.
- Board-less brief: нет конкретного task key, slug из формулировки пользователя.
