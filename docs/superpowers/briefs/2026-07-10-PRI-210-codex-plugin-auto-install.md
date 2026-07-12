# Brief — PRI-210 Codex: глобальная автоустановка и обновление rag-reviewer plugin через reviewer install
https://ru.yougile.com/team/686c049c8af8/#PRI-210

## Task

- Источник: reviewer store после инкрементального sync; канонический ключ `ID-210`, alias `PRI-210`, статус «Движок (reviewer CLI/MCP)».
- Цель: `uvx --from rag-reviewer@latest reviewer install codex` на чистой Windows/macOS/Linux ставит один глобальный reviewer MCP и namespaced Codex plugin без клона, shell, symlink и путей машины разработчика.
- Lifecycle: fresh install делает marketplace add + plugin add, повторный запуск — upgrade + идемпотентный add; `install-skills codex` обновляет тот же plugin, `install codex --no-skills` меняет только MCP, `--dry-run` не пишет и не ходит в сеть.
- Packaging: переносимый Git marketplace и компактный `plugin/`, единая release identity/cachebuster, полный динамический inventory `plugin/skills/*/SKILL.md`, `_common` и references доставляются, но не регистрируются как skill; plugin не владеет вторым MCP.
- Safety: точечно обновлять существующий TOML с backup, проверять plugin/manifest/skills/references/MCP, мигрировать только reviewer-owned legacy skills после успешной проверки, а при conflict/offline/verification failure возвращать non-zero и откатывать частичные изменения.
- Tests/UX: временные `HOME`/`CODEX_HOME`, mocked Codex, fresh/repeat/update/Windows/rollback/dry-run; обновить README EN/RU, AGENTS/plugin docs и завершать инструкцией New Chat/new session (+ Reload Window для IDE).

## Related work

- PRI-98 — сохранить существующую модель глобальной reviewer-конфигурации/allowlist и не смешивать distribution lifecycle с уже решёнными permission-настройками.
- PRI-122 / PR mimfort/rag_for_git#21 — повторить архитектурный паттерн: orchestration в CLI остаётся тонкой, состояние и data-driven lifecycle живут в `reviewer/install.py`, тесты — в `tests/install/`.
- PRI-169 — синхронизировать новый Codex flow с onboarding-документацией и `reviewer init`, не регрессируя новые поля/связку configure-review.

(dropped 5: PRI-203/166/115/141/140 семантически близки по plugin/RAG/install-контексту, но используют другие механизмы и не задают реализацию PRI-210.)

## Subsystems

- reviewer — composition/installation layer; `install.py` уже владеет client registry, MCP plans, skill download/stamps и wizard primitives.
- reviewer/entrypoints — Click-команды `install`, `install-skills`, `init` и общий UX/error propagation.
- tests/install — текущие unit/CLI-паттерны для config preservation, backup, idempotency, temporary home и skill installation.
- tests/skills — guard-тесты структуры skill prompts/inventory; подходящее место для manifest/inventory cachebuster guards.

## Relevant code

- reviewer/entrypoints/cli.py:284 — основной `install`: разветвить Codex plugin lifecycle, сохранить `--no-skills`/`--dry-run`/`--all`, не маскировать ошибку Codex и заменить финальное «перезапустите» на New Chat/new session.
- reviewer/entrypoints/cli.py:393 — `init` сейчас только пишет `.env` и запускает check; интеграцию с каноническим Codex flow держать тонкой и явной.
- reviewer/entrypoints/cli.py:513 — `install-skills` фильтрует клиентов по `skills_fn` и отвергает Codex; нужен отдельный dispatch в тот же plugin installer без изменения MCP.
- reviewer/install.py:305 — `launch_command` уже предпочитает абсолютный `uvx`/`uv`; переиспользовать как единственного владельца глобального MCP и сделать отсутствие launcher actionable.
- reviewer/install.py:362 — `Client.skills_fn=None` кодирует отсутствие standalone file skills; Codex plugin lifecycle не следует втискивать в файловую распаковку.
- reviewer/install.py:423 — `_render_codex` безопасно TOML-экранирует command/args; сохранить это при точечном обновлении reviewer-секции.
- reviewer/install.py:451 — `build_plan` сейчас оставляет существующий Codex `[mcp_servers.reviewer]` неизменным; заменить на точечное обновление с сохранением чужого TOML.
- reviewer/install.py:533 — `apply_plan` уже централизует запись с backup; расширить/переиспользовать транзакционную границу для MCP, marketplace/plugin и rollback.
- reviewer/install.py:563 — `fetch_skills_archive` — старый GitHub-tarball путь standalone skills; Codex должен идти через публичный CLI и переносимый marketplace source.
- reviewer/install.py:622 — `install_skills` и stamp остаются для файловых клиентов; рядом нужен отдельный `install_codex_plugin()` с verify/migrate/rollback результатом.
- reviewer/install.py:451 — граф blast radius: прямые callers — CLI `install` и Codex/config/backup-тесты `tests/install/test_install.py:79`, `:89`, `:119`, поэтому изменение контракта плана требует согласованного обновления всех этих точек.

(dropped 7: повторные retrieval-хиты, PyPI `update` и reviewer runtime/MCP symbols не участвуют в plugin distribution lifecycle.)

## Test exemplars

- tests/install/test_install.py:79 — сохраняет чужой TOML и проверяет абсолютный `uvx`; расширить на paths with spaces и точное обновление существующей reviewer-секции.
- tests/install/test_install.py:89 — текущая Codex-idempotency проверяет только отсутствие дубля; превратить в repeat/update assertions для актуальных command/args.
- tests/install/test_install.py:119 — готовый temp-path паттерн проверки backup и сохранения чужой конфигурации.
- tests/install/test_install.py:186 — temp-home + синтетический tarball для файловых skills; использовать как контраст и fixture-основу для безопасной legacy migration.
- tests/install/test_install_skills_cli.py:19 — CliRunner + mocked download/stamp; аналогично мокать argv-вызовы Codex CLI и JSON status.
- tests/install/test_install.py:267 — CLI-интеграция с подменённым HOME и повторным запуском; повторить для `CODEX_HOME`, fresh/repeat/rollback и финального UX.

(dropped 7: allowlist, другие client dialects и общие wizard/stamp кейсы не задают Codex plugin lifecycle напрямую.)

## Constraints / open questions

- Base-индекс `mimfort/rag_for_git` на `main` свеж (`drift=0`); рабочее дерево находится на `dev`, поэтому brief описывает существующий код primary index, а локальные незакоммиченные изменения индекс не видит.
- `get_task_context(PRI-210)` не дал связанных PR/затронутого кода; PRI-87 отсутствует в task store, поэтому исторический intent берётся только из описания PRI-210.
- Non-Python assets из задачи (`.agents/plugins/marketplace.json`, manifests, `.mcp.json`, README/AGENTS) не покрыты Python RAG snippets; их фактическое состояние и единый source of truth проверить напрямую на brainstorming/design этапе.
- До фиксации дизайна feature-detect нужно проверить реальный контракт установленного `codex plugin ... --json`, поведение conflict/upgrade/add и доступные rollback primitives; brief не предполагает внутренний cache API.
- Похожих артефактов по `PRI-210` в `briefs/`, `specs/`, `plans/` не найдено; посторонние untracked-файлы рабочего дерева сохранены без изменений.

Собран на: GPT-5 (текущая модель), режим: inline
