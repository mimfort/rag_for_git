# Brief — PRI-223 Часть B: передел мастера reviewer init
https://ru.yougile.com/team/686c049c8af8/#PRI-223

## Task
- Данные получены из reviewer store после sync; нормализованный ключ `ID-277`, алиас `PRI-223`.
- Реализовать только часть B: разделить `reviewer init` на глобальный runtime/credentials шаг и repo-onboarding с autodetect remote/primary branch.
- Убрать из стандартного мастера вопросы multirepo, `DEFAULT_REPO`, `REVIEW_BRANCHES`, `WEB_ADMIN_*` и legacy `TASK_BOARD_MCP`, сохранив runtime-поддержку старых env.
- Перед записью показать файлы, несекретные значения и provenance; после записи показать effective config.
- Критерии: fresh init без лишних вопросов; owner/name и primary branch видны до записи; второй repo изолирован; `--yes`/`--dry-run` без prompt/network; повторный init сохраняет advanced-настройки.

## Related work
- PRI-223 / PR #159 — часть A уже ввела effective per-repo branch config; часть B должна потреблять её публичные API, не реализовывать ветки заново.
- PRI-221 / PR #150 — переиспользовать `home_repo_path`, строгую проверку home YAML и provenance вместо отдельного формата repo-конфига.
- PRI-122 / PR #21 — исходный мастер onboarding задаёт baseline поведения, которое теперь разделяется на global и repo шаги.
- PRI-169 — предыдущая правка полей `reviewer init`; её тестовые паттерны подходят для удаления полей без потери существующих advanced env.
- (dropped 4: launcher, GitLab provider, Codex install и status используют иные механизмы и не задают реализацию части B.)

## Subsystems
- `reviewer/entrypoints` — команда `init`, orchestration prompt/preview/write/check и финальный effective-config output.
- `reviewer` — `install.py` описывает env-поля, группы, prompting и безопасный render.
- `reviewer/config` — home per-repo path, branch resolution и provenance, уже созданные частями PRI-221/A.
- `tests/install` — контракт интерактивного, `--yes` и `--dry-run` режимов.
- `tests/config` — изоляция per-repo YAML и проверка источников effective config.

## Relevant code
- `reviewer/entrypoints/cli.py:828` — `init` сейчас смешивает env prompt, board setup, preview, запись и post-check; здесь разделить global и repo stages.
- `reviewer/entrypoints/cli.py:844` — текущий поток читает один `.env`, а `--dry-run` рендерит только его; preview должен охватить оба назначения без записи и сети.
- `reviewer/entrypoints/cli.py:924` — unknown/advanced env сохраняются через `extra`; этот инвариант нужен повторному init.
- `reviewer/install.py:319` — `prompt_groups(..., yes)` уже гарантирует current-or-default без prompt в CI; repo stage должен иметь такой же явный noninteractive контракт.
- `reviewer/install.py:105` — `EnvField`/`EnvGroup` моделируют только `.env`; repo YAML не следует встраивать в эти типы как псевдо-env.
- `reviewer/gitutil.py:42` — `remote_url` уже fail-soft читает `origin`; использовать его для autodetect, не дублировать subprocess parsing.
- `reviewer/services/repo_id.py:24` — `derive_repo_from_remote` уже нормализует remote в repo id; отсутствие/неподдерживаемый remote должно давать управляемый fallback.
- `reviewer/services/branch.py:28` — `current_git_branch` даёт локальную ветку fail-soft; primary branch выбирать через effective API части A с autodetect fallback, не через committed `.review.yml` bootstrap.
- `reviewer/config/layers.py:69` — `home_repo_path(repo)` задаёт канонический per-repo destination.
- `reviewer/config/layers.py:257` — `resolve_policy_data` и `ResolutionMeta` задают существующий паттерн sources/shadowing для effective preview.
- `reviewer/entrypoints/cli.py:136` — `config show` уже выводит effective config/provenance; переиспользовать после onboarding вместо второго отчётного формата.
- (dropped 10: runtime consumers веток, MCP, board provider internals и launcher не меняются в части B.)

## Test exemplars
- `tests/install/test_install_wizard.py:152` — `--dry-run` не создаёт каталог/файл, не prompt-ит и скрывает секреты.
- `tests/install/test_install_wizard.py:173` — preview редактирует legacy и неизвестные secret-like extras без утечки значений.
- `tests/install/test_install_wizard.py:200` — `--dry-run` и `--yes` не запускают provider acquisition, validation, browser или network setup.
- `tests/install/test_install_wizard.py:164` — `--yes` сохраняет unknown advanced env keys.
- `tests/config/test_layers.py:135` — существующий home-файл остаётся неизменным при конфликте; repo-onboarding нужен неразрушающий update с отдельными тестами двух repo.
- (dropped 5: provider-specific setup и integration migration tests относятся к частям C/A.)

## Constraints / open questions
- [existing_artifacts] `docs/superpowers/specs/2026-08-07-PRI-223-per-repo-branch-config-design.md` и `docs/superpowers/plans/2026-08-07-PRI-223-per-repo-branch-config.md` описывают часть A, не дизайн части B.
- Граница части B жёсткая: provider-scoped credentials/minimal permissions остаются частью C; configure-review и двуязычная документация остаются частью D.
- `criteria=[]` в store, но описание содержит полный раздел «Критерии приёмки»; для B применяются критерии 1, 2, 3, 10 и 11.
- Branch bootstrap не должен читать committed `.review.yml` до выбора branch; источник только git/CLI/home per-repo/global/env fallback части A.
- `--yes` и `--dry-run` не должны выполнять интерактивные или сетевые операции; preview никогда не показывает секреты.
- PR #159 проверен через GitHub и присутствует в обновлённом `dev` (`4978334`); часть B создаётся поверх него.
- Сводки подсистем уже были прогреты; в этом запуске они не обновлялись по прямому указанию пользователя.

Собран на: mid tier (session model gpt-5.6-sol), режим: inline
