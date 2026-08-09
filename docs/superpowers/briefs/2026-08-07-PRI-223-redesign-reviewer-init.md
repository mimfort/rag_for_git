# Brief — PRI-223 Части C и D: credentials и configure-review
https://ru.yougile.com/team/686c049c8af8/#PRI-223

## Task
- Данные получены из reviewer store после sync; нормализованный ключ `ID-277`, алиас `PRI-223`.
- Реализовать только C: после выбора VCS/board provider спрашивать только его credentials и показывать получение токена, минимальные права, read/write-операции и проверку.
- Не дублировать готовый YouGile `companies → API key` flow; довести его UX про manual fallback, `allowOnlyOpenId` и отсутствие обязательных admin-прав.
- Реализовать D: `configure-review` определяет repo, спрашивает primary/index branches и безопасно обновляет home per-repo YAML, сохраняя остальные ключи и комментарии.
- Зафиксировать модель владения настройками и сценарии single-repo/multi-repo/CI в синхронных README; добавить focused tests C/D.

## Related work
- PRI-215 / PR #127 — переиспользовать generic registry, credential schema, setup metadata и существующий YouGile acquisition flow; не добавлять provider-specific ветвления в CLI.
- PRI-221 / PR #150 — переиспользовать home per-repo path, credential rejection, provenance и двуязычный шаблон документации layered config.
- PRI-205 / PR #92 — следовать server-side discovery/pick-list паттерну `configure-review`, сохраняя fail-open fallback и отсутствие credentials в YAML.
- PRI-133 — сохранить VCS-контракт: GitHub/GitLab credentials принадлежат env, а provider определяется repo/remote или явным выбором.
- PRI-169 — текущие init/provider tests являются регрессионным baseline при сужении VCS credential prompts.
- (dropped 3: task sync scoping, retention filter и сама PRI-223 не добавляют отдельного механизма для C/D.)

## Subsystems
- `reviewer` — `install.py` задаёт стандартные env-группы, credential prompts и redacted preview.
- `reviewer/entrypoints` — `reviewer init` выбирает provider, собирает global/repo plans и оркестрирует проверку.
- `reviewer/tasks` — registry-driven board credential/setup metadata и YouGile acquisition/validation.
- `reviewer/config` — effective branches, provenance и безопасная публикация home per-repo YAML.
- `reviewer/vcs` — GitHub/GitLab операции, по которым формулируются минимальные scopes.
- `tests/config` — изоляция repo, validation и сохранение home YAML.
- `tests/entrypoints` — интерактивный/noninteractive контракт init и redaction.
- `tests/docs` — паритет README и целостность документированных команд/skills.

## Relevant code
- `reviewer/entrypoints/cli.py:1126` — расширить текущий `init`, сохранив scope/preview/no-network контракты части B.
- `reviewer/entrypoints/cli.py:1173` — board credentials уже prompt-ятся только после выбора provider; использовать этот registry-driven паттерн для VCS, не регрессить board flow.
- `reviewer/tasks/boards/registry.py:104` — `BoardProviderRegistry` централизует credential fields и проверяет полноту setup metadata; это эталон provider-scoped модели.
- `reviewer/tasks/boards/yougile.py:90` — YouGile spec уже связывает credential fields, help text и acquisition hook; изменения должны быть уточнением метаданных/UX.
- `reviewer/tasks/boards/setup.py:154` — готовый `companies → /auth/keys` flow с manual fallback и очисткой password; не переписывать.
- `reviewer/config/settings.py:82` — GitHub/GitLab credentials и runtime fallbacks остаются env-only и обратно совместимыми.
- `reviewer/config/onboarding.py:101` — текущий repo plan намеренно делает noop для существующего `repository`; `configure-review` нужен отдельный безопасный update существующего блока.
- `reviewer/config/branches.py:159` — `render_repository_block` задаёт canonical YAML представление веток; переиспользовать validation/format без полной перезаписи файла.
- `reviewer/config/branches.py:664` — `resolve_repo_branches` возвращает effective primary/index/source для показа выбранного слоя до записи.
- (dropped 20: runtime board consumers, task ETL, MCP review path и общие Settings-поля не меняют C/D.)

## Test exemplars
- `tests/install/test_install_wizard.py:303` — существующий тест доказывает выбор одного board provider и запись только его credentials; расширить аналогичным VCS-контрактом.
- (dropped 14: broad test retrieval вернул production/нерелевантные chunks; два focused graph/test запроса завершились timeout.)

## Constraints / open questions
- Жёсткий scope: только C и D; A уже смержена PR #159, B уже присутствует на текущем `dev` и имеет отдельные spec/plan.
- C частично существует: board provider credentials и YouGile acquisition уже scoped; реализация должна закрыть остаток, а не создавать второй setup framework.
- `criteria=[]` в store, но описание содержит критерии; для C/D применяются 5, 8, 9, 14, 15 и общие security/noninteractive ограничения 7/10.
- `[existing_artifacts]` brief C/D перезаписывает прежний brief B; specs/plans частей A и B остаются отдельными историческими артефактами.
- `[existing_artifacts]` `docs/superpowers/specs/2026-08-07-PRI-223-per-repo-branch-config-design.md`, `docs/superpowers/specs/2026-08-08-PRI-223-reviewer-init-onboarding-design.md`.
- `[existing_artifacts]` `docs/superpowers/plans/2026-08-07-PRI-223-per-repo-branch-config.md`, `docs/superpowers/plans/2026-08-08-PRI-223-reviewer-init-onboarding.md`.
- Markdown skill/README не индексируются как Python snippets; перед дизайном их нужно прочитать локально, сохранив паритет plugin source и установленных projections.
- Graph/test expansion reviewer завершился timeout; это не блокирует, но blast radius уточнить локальным search перед реализацией.
- Сводки подсистем уже были построены; в этом запуске они не обновлялись по прямому указанию пользователя.

Собран на: mid tier (session model gpt-5.6-sol), режим: inline
