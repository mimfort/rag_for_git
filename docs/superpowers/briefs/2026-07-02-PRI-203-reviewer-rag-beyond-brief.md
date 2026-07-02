# Brief — PRI-203 Reviewer RAG за пределами брифа: грунтовка в фазах план/ревью (plugin-level)
url: https://ru.yougile.com/team/686c049c8af8/#PRI-203

## Task
- **Цель:** дать пользоваться reviewer-инструментами (session-less RAG + граф) **за пределами фазы брифа**, на уровне плагина — точечно, fail-open, без форсирования во всех фазах.
- **ROI по убыванию:** (1) `writing-plans` — грунтовать «Files/Interfaces» через `search_codebase`+граф (точные сигнатуры + полнее call-sites); (2) ревью — blast-radius смены сигнатур (закрывает класс пропущенных call-sites, высший ROI); (3) `brainstorming` — слабее, `get_subsystem_summaries`+точечный `search_codebase` (опц.); (4) реализация субагентами — почти нет.
- **Капабилити-гэп движка:** session-less impact/blast-radius (сейчас `get_impact` только внутри PR-сессии).
- **Критерии (мягкие, inline в description):** точки вставки в скилах с fail-open деградацией в grep/Read; решить вопрос session-less impact-тула (нужен ли, форма); зафиксировать «точечно, не везде» + ограничение свежести (WIP vs base); standalone-baseline и solve-task не ломаются.
- **НЕ входит:** working-tree overlay ретрива; форсирование reviewer везде; движок retrieval/cliff/ANN (сделано в PRI-202).

## Related work
- Корпус задач (`search_tasks`/`get_task_context`) вернул **только саму PRI-203** — связанных/похожих задач с PR нет. (dropped 0)
- Контекст из текста задачи (не из поиска): **PRI-202** — движок retrieval/cliff (готово, граница «не трогаем»); **PRI-170** — скоуп задач по project; **PRI-167** — top-k сводок. Только как ориентиры, не источники правок.

## Subsystems
- `reviewer/tools` — `make_tools` (7 PR-session тулов), `compute_impact`/`get_impact`, `ToolContext`; здесь живёт impact.
- `reviewer/mcp` — session-less тулы (`search_codebase`/`related_symbols`/`callers`/`definition`), `_resolve_repo_branch`; сюда лёг бы session-less impact.
- `reviewer/entrypoints` — MCP-сервер (32 тула, FastMCP) — точка регистрации нового тула.
- `reviewer/retrieval` — session-less ретрив уже адаптивный (cliff), трогать не надо.

## Relevant code
- `reviewer/tools/impact.py:30` — `compute_impact`: гейт «сигнатура head≠base», нужен `overlay_ref` → **сессионный по конструкции**. Зовётся ТОЛЬКО из `get_impact` (+ 4 теста) → подтверждает гэп. (blast-radius логика для мимикрии)
- `reviewer/tools/code_tools.py:149` — `get_impact()` PR-session тул; `:60` `make_tools` строит тулы из `ToolContext` (overlay_ref/changed_*).
- `reviewer/mcp/service.py:314` — `_resolve_repo_branch`: паттерн session-less тула (repo+ветка из REVIEW_BRANCHES); session-less impact мимикрирует `callers`/`related_symbols` здесь.
- `reviewer/entrypoints/mcp_server.py` — регистрация MCP-тула (32→33, если новый тул).
- `plugin/skills/_common/tool-usage.md:24-29` — общий reference-блок, уже перечисляет session-less тулы; естественный дом для конвенции «грунтовка»; включён в ask/solve-task/maintainability/performance/pr-walkthrough/review-pr.
- `plugin/skills/maintainability-review/SKILL.md`, `plugin/skills/performance-review/SKILL.md:34` — **in-repo ревью-скилы = реальная «code-review» точка вставки репо** (performance-review уже называет `search_code`/`find_callers` — PR-session имена).
- `plugin/skills/review-pr/references/blast-radius-prompt.md` — уже существующий PR-session blast-radius (эталон грунтовки).

## Constraints / open questions
- **[КЛЮЧЕВОЕ] `writing-plans`/`brainstorming`/`subagent-driven` (superpowers) и официальный `/code-review` — ВНЕ репозитория** (`~/.claude/plugins/cache/.../superpowers/6.1.0/`, вендоренные) → **править их в этом репо нельзя**. Открытый вопрос для brainstorming: куда реально ложатся точки вставки? Вероятно — собственные скилы репо (`maintainability-review`/`performance-review` = «ревью»; новый plan-grounding reference/скил или дополнение solve-task-хэндоффа), а не вендоренные. Форк superpowers — недопустимо по сопровождаемости.
- **Session-less impact:** `compute_impact` требует head-vs-base overlay → для локального WIP невозможен session-less; `callers`/`related_symbols` уже дают session-less blast-radius (без гейта по сигнатуре). Открытый вопрос: нужен ли новый тул движка или `callers` достаточно? Working-tree overlay — явно ВНЕ скоупа.
- **Свежесть индекса:** база = `base:<branch>`, не рабочее дерево; overlay только для GitHub-PR. Грунтовка честна для планирования (до правок), слепа на своём WIP-диффе.
- **Стоимость Voyage** (free 3 RPM / 10K TPM) → политика «точечно, не звать на мелких/знакомых правках».
- Индекс `dev` на старте отставал на 10 коммитов — **переиндексирован до `52d3957`** в преполёте (drift 0).
- **Директива пользователя:** использовать **Opus и для код-субагентов** (переопределяет дефолт «код → Sonnet») — применить на этапе subagent-driven-development.
- Существующих артефактов PRI-203 (briefs/specs/plans) нет.

## Токены (этап solve-task)
Модель: claude-opus-4-8
fresh-in 52.9K · out 59.7K · cache-write 417.3K · cache-read 3M
Всего: 3.5M токенов
