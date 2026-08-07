# Brief — PRI-225 Добавить retention-фильтр задач доски в per-repo YAML
https://ru.yougile.com/team/686c049c8af8/#PRI-225

## Task
- Источник: reviewer store после scoped `sync_board` проекта PRI; ключ стора `ID-279`, alias `PRI-225`, статус «Бэклог».
- Добавить generic `task_board.sync_filter` (`max_age_days`, `include_archived`) в effective home per-repo/committed YAML отдельно от provider `options`, без credentials, с каноническим archive/terminal metadata.
- Пробросить фильтр через sync-tasks и MCP `sync_board` в listing/`SyncService`, применяя его до `normalize`/`normalize_meta` и по возможности pushdown-ить ограничения в API провайдера.
- Вернуть `eligible`, `filtered_by_age`, `filtered_archived` и warnings; включить fingerprint фильтра в cursor, чтобы смена/ослабление backfill-ила задачи ниже watermark.
- Без фильтра сохранить текущее поведение; `limit` не двигает cursor и не purge-ит; полный scoped purge удаляет только исключённые задачи выбранного project.
- Приёмка требует независимых retention-политик двух repo, референсного YouGile и generic contract, тестов возраста/archive/cursor/purge/partial sync и обновления configure-review/docs.

## Related work
- PRI-170 — сохранить существующий project scope для enumeration, cursor и purge; retention не должен затрагивать задачи других проектов.
- PRI-207 / PR #94 — переиспользовать разделение changed-normalize и дешёвого meta-refresh ниже watermark; fingerprint должен намеренно обходить старый cursor при backfill.
- PRI-140 / PR #27 — сохранить server-side ETL и компактный summary `sync_board`, не возвращая тексты задач через LLM.
- PRI-95 — развить opt-in orphan purge так, чтобы eligible keys задавали scoped множество сохранения при полном обходе.
- PRI-221 / PR #150 — `task_board` уже проходит через committed и home per-repo policy layers; новый вложенный блок должен уважать их top-level replacement/provenance.
- PRI-215 / PR #127 — расширять общий provider contract/contract suite без ветвления generic MCP и `SyncService` по типу доски.
- (dropped 9: текущая задача, discovery/create/attachments/VCS/freshness задачи и добавление отдельных провайдеров не задают retention-механику.)

## Subsystems
- `reviewer/tasks` — provider contract, RawTask, server-side ETL, watermark, counters and project-scoped purge.
- `tests/tasks` — fake-based SyncService tests, provider contract suite and YouGile reference coverage.
- `reviewer/config` — credential-free TaskBoardConfig plus effective committed/global/per-repo home policy layers.
- `tests/config` — normalization, secret rejection, source precedence and top-level replacement contracts.
- `reviewer/policy` — converts effective YAML data into the runtime task-board configuration consumed by MCP flows.
- `tests/docs` — guards board-provider configuration and capability documentation against registry drift.

## Relevant code
- `reviewer/config/task_board.py:11` — extend immutable `TaskBoardConfig`/`as_dict` with a validated generic sync filter, not `options`.
- `reviewer/config/task_board.py:194` — normalize the nested block and retain recursive credential rejection/backward-compatible absence semantics.
- `reviewer/config/layers.py:257` — effective config merges home global, committed and home per-repo layers by top-level key; two repos already resolve independently.
- `reviewer/tasks/boards/base.py:26` — `RawTask` currently has `timestamp`, `completed` and neutral `provider_data`, but no canonical archived field.
- `reviewer/tasks/boards/base.py:46` — evolve the board-agnostic provider listing contract so pushdown capability remains optional and generic.
- `reviewer/tasks/boards/yougile.py:155` — reference listing currently walks every project/board/column/task and exposes timestamp/completed; age/archive filtering must happen before normalization and project scope must remain first-class.
- `reviewer/tasks/boards/clickup.py:423` — existing `include_closed` and `date_updated_gt` request parameters are a concrete pushdown precedent.
- `reviewer/tasks/sync.py:36` — core insertion point: read cursor, enumerate, classify eligibility before `normalize_meta`/`normalize`, aggregate reason counters and persist cursor plus filter fingerprint.
- `reviewer/tasks/sync.py:131` — preserve `limit` safeguards and derive full scoped purge from eligible active keys only for unlimited runs.
- `reviewer/mcp/service.py:469` — thread the generic filter beside `provider_options` through both deploy-wide and scoped provider paths without mixing them.
- `reviewer/entrypoints/mcp_server.py:104` — public MCP schema/call site must expose the new filter to sync-tasks and generated clients.
- Blast radius: callers of `SyncService.run` include MCP and `tests/tasks/test_sync.py`; callers of config normalization include policy, home-layer validation, docs and config tests.
- (dropped 7: secondary provider implementations and unrelated policy/task-service chunks do not change the minimum config→listing→sync→MCP path.)

## Test exemplars
- `tests/tasks/test_sync.py:114` — preserve the exact partial-sync invariant: `limit` disables purge and cursor advancement.
- `tests/tasks/test_sync.py:239` — model filter-fingerprint backfill on the existing below-watermark force-renormalize fixture while asserting ordinary runs remain unchanged.
- `tests/tasks/test_sync.py:190` — assert retention purge continues passing project scope and only eligible active keys.
- `tests/config/test_task_board_config.py:6` — extend immutable generic config normalization assertions with `sync_filter`, keeping `options` unchanged.
- `tests/config/test_task_board_config.py:86` — retain recursive cross-provider credential rejection without echoing secret values.
- `tests/config/test_layers.py:27` — test per-repo home precedence/top-level replacement with distinct `task_board.sync_filter` values for two repos.
- `tests/tasks/boards/contract.py:72` — extend the shared provider contract around stable timestamps/archive certainty and optional listing pushdown.
- `tests/tasks/boards/test_yougile_normalize.py:248` — keep the zero-I/O `normalize_meta` budget; excluded rows must never reach it.
- `tests/mcp/test_sync_board.py:55` — mirror current immutable provider-options threading with a separate generic filter and compact counters.
- (dropped 14: duplicate SyncService coverage and unrelated create/finish/attachment provider lifecycle tests.)

## Constraints / open questions
- Define the age boundary explicitly: which provider update timestamp is authoritative, UTC clock injection for tests, and whether exactly `max_age_days` is eligible.
- Define canonical archive certainty separately from terminal/completed status; unknown providers must warn rather than silently classify.
- Decide cursor-state representation/migration from the current integer value and which filter changes require backfill without re-embedding unchanged content.
- Clarify whether retention purge may delete tasks protected by `keep_with_prs`; current orphan purge can retain PR-linked tasks, while acceptance asks to remove previously indexed excluded tasks.
- `task_board` is replaced as one top-level value across policy layers, so configure-review must preserve sibling board fields when editing only `sync_filter`.
- Store returned `criteria=[]`, but the full acceptance criteria are present under the description's `## Критерии приёмки` heading and were used above.
- Index `mimfort/rag_for_git@dev` was refreshed in this run (`drift=0`, SCIP); subsystem summaries and scoped PRI task corpus were warm.

Собран на: openai/gpt-5.6-sol, режим: inline
