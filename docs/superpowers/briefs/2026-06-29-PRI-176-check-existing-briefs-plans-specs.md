# Brief — PRI-176 solve-task: проверка существующих briefs/plans/specs по ключу
url: https://ru.yougile.com/team/686c049c8af8/#PRI-176

## Task
- **Ключ:** PRI-176. **Слой:** плагин/скил `solve-task` (`plugin/skills/solve-task/SKILL.md`) + guard-тест `tests/skills/test_solve_task_brief.py`.
- **Проблема 1:** glob-паттерн идемпотентности `docs/superpowers/briefs/<date>-<KEY>-*.md` привязан к сегодняшней дате → повторный прогон на другой день создаёт дубликат брифа.
- **Проблема 2:** solve-task не проверяет downstream-артефакты (spec/plan) перед перезаписью брифа.
- **Решение:** (1) заменить glob на `*-<KEY>-*.md` (без даты); (2) перед дистилляцией брифа искать `*<KEY>*` в `briefs/`, `*<key>*-design.md` в `specs/`, `*<key>*.md` в `plans/` (case-insensitive); (3) предупреждать пользователя, но не блокировать; (4) найденные артефакты писать в `## Constraints` с тегом `[existing_artifacts]`.
- **Критерии:** правильный glob, case-insensitive поиск по трём директориям, предупреждение (не блокировка), тег в Constraints, guard-тесты в `test_solve_task_brief.py`.

## Related work
- **PRI-163 (ID-163)** — персист брифа и idempotency-логика, которую чиним (`plugin/skills/solve-task/SKILL.md` шаг 4, PR #69). ✓ reuse
- **PRI-164 (ID-164)** — brief hygiene, guard-тесты `test_solve_task_brief.py` (PR #72). ✓ reuse стиль guard-тестов.
- **PRI-146 (ID-146)** — спека brief + relevance-фильтр, задаёт скелет брифа и секцию Constraints.
- (dropped 2: PRI-177 — трассируемость brief→spec, смежная но отдельная; PRI-187 — верификация перед hand-off, задача явно исключает.)

## Subsystems
- **tests/skills** — guardrail-тесты скиллов; здесь живут маркер-проверки `SKILL.md`.
- **plugin/hooks** — хук `brief_cost` следит за папкой `docs/superpowers/briefs/`, но не в скоупе правки.
- (dropped 1: reviewer/tasks — store-first/get_task не трогаем.)

## Relevant code
- `plugin/skills/solve-task/SKILL.md:207` — **цель правки**: `glob \`docs/superpowers/briefs/<date>-<KEY>-*.md\`` → заменить на паттерн без даты; добавить pre-brief check и warning.
- `tests/skills/test_solve_task_brief.py:13-54` — **расширить guard-тесты**: маркеры glob-шаблона, упоминание `docs/superpowers/specs/` и `docs/superpowers/plans/`, case-insensitive search, предупреждение-не-блокировка.
- `docs/superpowers/briefs/2026-06-26-PRI-164-solve-task-brief-hygiene.md` — пример имени брифа (дата-KEY-slug).
- `docs/superpowers/plans/2026-06-26-pri-164-solve-task-brief-hygiene.md` — пример плана (дата-pri-N-slug, lowercase).
- `docs/superpowers/specs/2026-06-26-pri-164-solve-task-brief-hygiene-design.md` — пример спеки (суффикс `-design.md`, lowercase).
- (dropped 1: `plugin/hooks/brief_cost.py:156-158` — использует `/docs/superpowers/briefs/`, но не правим.)

## Test exemplars
- `tests/skills/test_solve_task_brief.py:28-34` — `test_solve_task_persists_brief()`: стиль guard-теста `SKILL.md` (PRI-163).
- `tests/skills/test_solve_task_brief.py:37-43` — `test_solve_task_dedupes_related_sources()`: пример guard'а для новой логики solve-task.
- (dropped 1: `tests/skills/test_solve_task_brief.py:46-51` — criteria enrichment, не относится к этой задаче.)

## Constraints / open questions
- **Case-insensitive glob:** на Linux/macOS glob чувствителен к регистру; инструкция должна требовать case-insensitive матчинг (например, два globs `*PRI-176*` + `*pri-176*` или fnmatch с lowercased именами).
- **Не блокировать:** warning → `[Y/n]`; при продолжении — тег `[existing_artifacts]` в Constraints.
- **Spec-суффикс:** спеки ищем по `*--*-design.md`? Нет, задача говорит `*--*design.md` (суффикс `-design.md`).
- **Board-less:** для задачи без ключа искать по slug (out of scope этой задачи? Текущий скоуп — по ключу).
- **Индекс свежий:** drift=0, summaries warm, corpus warm (PRI).
