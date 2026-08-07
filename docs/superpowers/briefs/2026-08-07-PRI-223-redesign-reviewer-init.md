# Brief — PRI-223 Перепроектировать reviewer init: конфигурация, repo-onboarding и токены
https://ru.yougile.com/team/686c049c8af8/#PRI-223

## Task
`reviewer init` сейчас выводит единый плоский каталог глобальных, репозиторных и опциональных
полей (`reviewer/install.py:182-275`), включая глобальные `DEFAULT_REPO`/`REVIEW_BRANCHES`
(группа «Мульти-репо / ветки», `install.py:219-233`), хотя repo обычно берётся из git remote
(`cli.py:48-60`), а `REVIEW_BRANCHES` резолвится через единый Settings для всех репозиториев
(`settings.py:97,134-138`; `services/branch.py:15-25`). PRI-221 уже дала home-слои конфига
(`~/.config/rag-reviewer/review.yml` + `repos/<owner>/<name>.yml`), но только для policy —
ветки и мастер их не используют. Нужно: разделить onboarding на глобальный (runtime/credentials)
и per-repo (remote/ветки) шаги; завести в home per-repo YAML блок
`repository.primary_branch`/`repository.index_branches`; перевести index/status/prepare_review/
sync-codebase/session-less MCP-тулы и branch validation на per-repo branch config; убрать
`DEFAULT_REPO`, `REVIEW_BRANCHES`, `WEB_ADMIN_*`, legacy `TASK_BOARD_MCP` из мастера (runtime-
фолбэк сохранить); спрашивать credentials только выбранного provider'а с минимальными правами;
добавить `reviewer config show` для эффективных веток; научить `configure-review` писать ветки
в home per-repo YAML; неразрушающая миграция `DEFAULT_REPO`/`REVIEW_BRANCHES` из env. 15
критериев приёмки покрывают: отсутствие лишних вопросов при fresh init, авто-detect
owner/name+primary branch, изоляцию второго репо, независимые `index_branches`, запись
configure-review в правильный файл с сохранением ключей/комментариев, отсутствие циклической
зависимости bootstrap↔`.review.yml`, запрет секретов в YAML, provider-scoped credentials,
YouGile без admin-прав, `--yes`/`--dry-run` безопасность, идемпотентный повторный init, обратную
совместимость старых env, `config show` с source веток, обновление README (RU+EN), тесты.

## Related work
- **PRI-221 (ID-274, done, PR #150)** — прямой предшественник: ввела `reviewer/config/layers.py`
  (`resolve_policy_data`, `home_repo_path`, `build_config_report`, `migrate_repo_config`,
  `HomeConfigError`), `home_repo_path(repo)` → `~/.config/rag-reviewer/repos/<owner>/<name>.yml`,
  `reviewer config show`/`migrate` CLI-команды и secret-quarantine (`_credential_path`,
  `_secret_names`). PRI-223 расширяет ЭТУ ЖЕ инфраструктуру блоком `repository.*`, а не строит
  новую — модель мержа (верхний ключ целиком, home-repo > `.review.yml` > home-global > env) и
  паттерн provenance/`ResolutionMeta` нужно скопировать 1:1 для веток.
- **PRI-169 (ID-169, done)** — предыдущая ревизия того же `WIZARD_GROUPS`/`ENV_TEMPLATE`/
  `configure-review` SKILL.md: добавляла GitLab VCS, веб-админку, `YOUGILE_API_BASE` в мастер.
  Показывает точный шаблон правки install.py (`_GROUP_HEADERS`, `ENV_TEMPLATE`) и
  `tests/test_install_wizard.py`/`tests/install/test_install_wizard.py` — та же матрица тестов,
  которую PRI-223 теперь ЧАСТИЧНО ОТКАТЫВАЕТ (убирает группу «Мульти-репо / ветки», WEB_ADMIN_*
  из стандартного мастера).
- **PRI-168 (ID-168, done)** — исходная спека скилла `configure-review`; текущий SKILL.md
  (`plugin/skills/configure-review/SKILL.md`) уже реализует выбор target
  (home per-repo vs `.review.yml`) для policy-ключей — тот же UX-паттерн (шаг 1 pipeline) нужно
  расширить вопросом про primary/index branches.
- **PRI-225 (ID-279, done, PR #153)** — добела свежий пример добавления generic-блока в per-repo
  YAML (`task_board.sync_filter`): показывает, как валидировать новый блок в `layers.py`, как
  писать «работает и в home per-repo, и в committed `.review.yml`», и как обновлять
  `configure-review` и тесты для нового ключа — прямой шаблон для `repository.primary_branch`/
  `index_branches`.
- (dropped 3: PRI-133 GitLab-провайдер/креды VCS в .review.yml — про VCS-платформу, не про
  onboarding/branch-конфиг, credentials-фокус уже покрыт setup.py/registry.py, читанными напрямую;
  PRI-122 исходный мастер онбординга — полностью замещён/расширен PRI-169, читать устаревший
  снимок избыточно; PRI-210 Codex автоустановка плагина — про payload-digest установки плагина в
  IDE, не про содержимое `reviewer init`)

## Subsystems
- `reviewer/config` — Settings (70+ env-параметров, включая `default_repo`/`review_branches`),
  `layers.py` (home/committed резолв policy, provenance), `TaskBoardConfig`,
  `ProviderCredentialSource` — ядро, которое расширяется блоком `repository.*`.
- `reviewer/entrypoints` — CLI (`init`, `config show/migrate`, `index`, `status`) и MCP-сервер;
  все команды, которые сейчас читают `settings.review_branches_list()`/`primary_branch()`
  напрямую, должны перейти на per-repo резолв.
- `reviewer` (корневой кластер) — `install.py` (WIZARD_GROUPS, prompt_groups, render_env) —
  главный файл переделки мастера.
- `tests/config` — 110 unit-тестов слоя конфига (layers, settings, review_branches,
  provider_credentials, task_board) — шаблон для новых тестов per-repo branch config.
- `reviewer/tasks` — 11 board-адаптеров + `registry.py`/`setup.py` — источник
  `CredentialFieldSpec`/`ProviderSetupSpec` для provider-scoped credential UX (п.9-10 задачи).
- `reviewer/services` — `branch.py` (`resolve_branch`, ветка-агностичный резолв) и
  `review_service.py` (`review_branches_list()` гейт для PR) — оба должны стать per-repo-aware.
- (dropped 2: `reviewer/launcher` — TUI поверх Click-команд, не меняет семантику onboarding, для
  PRI-223 нерелевантен; `reviewer/graph`/`reviewer/retrieval` — не касаются конфигурации/веток)

## Relevant code
- `reviewer/install.py:182-275` — `WIZARD_GROUPS`: группа «Мульти-репо / ветки»
  (`DEFAULT_REPO`, `REVIEW_BRANCHES`, строки 219-233) и «Веб-админка» (258-275) — убрать из
  стандартного мастера; `board_env_group(_default_board_registry())` (257) — существующий паттерн
  provider-driven группы для credentials, переиспользовать для «спрашивать только выбранного
  provider'а».
- `reviewer/install.py:283-368` — `read_env`, `_GROUP_HEADERS`, `render_env`, `prompt_groups` —
  функции, которые правит мастер; `prompt_groups` (319) — точка, где нужно добавить repo-шаг
  (git remote autodetect + branch prompt), не смешивая его с EnvGroup/`.env`-моделью.
- `reviewer/entrypoints/cli.py:828-940` — команда `init`: интерактивный поток (board provider
  choice → `configure_board_provider`), место для нового отдельного repo-onboarding шага и вызова
  `reviewer config show` в конце.
- `reviewer/entrypoints/cli.py:48-60` (`_resolve_repo`) — текущий фолбэк-каскад
  `--repo → git remote → DEFAULT_REPO → ошибка`; после задачи `DEFAULT_REPO` остаётся
  compat-фолбэком, но home per-repo YAML должен резолвиться раньше.
- `reviewer/entrypoints/cli.py:113-179` (`_config_context`, `config_show`, `config_migrate`) —
  существующий `reviewer config` group; `primary_branch()` вызывается на строке 120 из глобальных
  Settings — сюда встраивается per-repo branch resolution и новая ветка вывода в `config show`.
- `reviewer/entrypoints/cli.py:630,684,701-714,735,743` — все текущие места, которые берут ветку
  из `s.primary_branch()`/`s.review_branches_list()` (index/status/search команды) — blast radius
  перевода на per-repo резолв (grep подтвердил ровно эти строки + `mcp/service.py:1336-1349,2227`
  и `services/review_service.py:35-38,202`).
- `reviewer/config/settings.py:91-97,134-138` — `default_repo`, `review_branches` env-поля и
  `review_branches_list()`/`primary_branch()` — методы остаются как runtime-фолбэк (п.8 задачи),
  но перестают быть единственным источником.
- `reviewer/services/branch.py:15-38` — `resolve_branch(requested, current, settings)` — сейчас
  берёт allowlist только из `settings.review_branches_list()`; нужно параметризовать по
  per-repo `index_branches`, сохранив сигнатуру для вызывающих (branch-agnostic паттерн для CLI
  search/solve-task).
- `reviewer/config/layers.py:60-72` (`reviewer_config_root`, `home_repo_path`) — уже даёт путь
  `~/.config/rag-reviewer/repos/<owner>/<name>.yml`; новый блок `repository.primary_branch`/
  `index_branches` пишется туда же через тот же `_read_mapping`/секрет-quarantine контракт
  (`_secret_names`, `_credential_path`, строки 87-151).
- `reviewer/config/layers.py:1-58` — `HomeConfigError`/`HomeCredentialError`/`HomePolicyError`,
  `ResolutionMeta`, `MigrationResult` — типы ошибок и provenance-модель, которую блок веток должен
  использовать (не изобретать новую).
- `reviewer/policy/policy.py:86-132` (`ReviewPolicy.load_data`/`load`) — образец «явные ключи
  data перекрывают Settings-дефолты только если заданы»; `repository.*` — параллельная, но
  отдельная от `ReviewPolicy` структура (ветки не являются policy).
- `reviewer/tasks/boards/registry.py:19-27,36-66` — `CredentialFieldSpec` (env/label/secret/
  required/default/aliases) и `BoardProviderSpec`/`ProviderSetupSpec` (help_url, help_text,
  acquisition) — контракт, который уже несёт «label» для credential-полей; задача просит
  дополнить его текстом про минимальные права/read-write-операции — либо расширить
  `ProviderSetupSpec.help_text`, либо `CredentialFieldSpec`.
- `reviewer/tasks/boards/setup.py:154-243` (`acquire_yougile_key`) — рабочий companies→key
  auto-flow + manual fallback, упомянутый в задаче как образец «без обязательных admin-прав»;
  `_manual_yougile_key` (вызывается на 168,238) и `_setup_url`/`spec.setup.help_url_builder`
  (246-248) — где добавляется описание allowOnlyOpenId/минимальных прав.
- `reviewer/tasks/boards/yougile.py:90-` (`provider_spec`) — `CredentialFieldSpec(env="YOUGILE_API_KEY", secret=True, ...)`; задача явно просит описать минимальные права здесь (п.10).
- `plugin/skills/configure-review/SKILL.md:22-34` — pipeline шаг 1: уже спрашивает
  home-per-repo-vs-`.review.yml` target для policy; естественное место добавить вопрос про
  primary/index branches и запись в `home_repo_path` (п.5 задачи).

(dropped 1: `reviewer/mcp/service.py` полные 1300+ строк — грепом подтверждены только точные
номера строк 235,1336-1349,2227 как места чтения `review_branches_list`/`primary_branch`; читать
файл целиком не потребовалось, использованы grep-подтверждённые строки)

## Test exemplars
- `tests/config/test_layers.py:27-52` (`test_layers_replace_top_level_values_and_report_sources`) —
  канонический паттерн проверки резолва слоёв через `resolve_policy_data(repo, branch, fetch_fn,
  config_root=tmp_path)`: пишет `tmp_path/review.yml` и `tmp_path/repos/o/r.yml`, мокает
  committed-fetch лямбдой, проверяет `data[...]`, `meta.sources[...]`, `meta.shadowed[...]` —
  прямой шаблон для тестов резолва `repository.primary_branch`/`index_branches`.
- `tests/config/test_layers.py:110-136` (`test_subgroup_repo_uses_nested_home_path`,
  `test_dotted_repo_name_appends_yaml_suffix_without_colliding`) — паттерны для owner-подгрупп
  (GitLab) и repo-имён с точками — нужно повторить для branch-конфига, т.к. путь тот же
  `home_repo_path`.
- `tests/config/test_layers.py:210-236` (`test_credential_file_is_skipped_without_echoing_value`,
  `test_max_tokens_is_not_misclassified_as_secret`) — паттерн проверки «секрет в YAML отклоняется
  без эха значения» — прямой тест для критерия 7 задачи применительно к новому блоку.
- `tests/config/test_layers.py:365-380` (`test_migrate_creates_file_and_second_call_is_noop`,
  `test_migrate_refuses_different_destination`) — идемпотентность миграции; шаблон для новой
  миграции `DEFAULT_REPO`/`REVIEW_BRANCHES` → home per-repo YAML.
- `tests/config/test_review_branches.py:4-25` (`test_review_branches_default_is_main`,
  `test_review_branches_parsed_from_csv`, `test_review_branches_empty_falls_back_to_main`) —
  текущие тесты чисто env-branches; после задачи потребуют дополнения per-repo-override
  вариантами (mock `monkeypatch.setenv`, без БД/сети — соответствует unit-контракту репозитория).
- `tests/install/test_install_wizard.py:99-136`
  (`test_prompt_groups_yes_uses_current_values`, `..._yes_skips_optional_groups`,
  `test_render_env_includes_board_api_key_and_hint`) — паттерн мокинга `prompt_groups`/
  `render_env` без реального click-ввода; тот же подход нужен для тестов нового repo-onboarding
  шага.
- `tests/install/test_install_wizard.py:276-314`
  (`test_init_interactive_configures_selected_registry_provider`) — полный пример: мокает
  `reviewer.install.prompt_groups`, `ClickSetupIO.choose`, `configure_board_provider`,
  `click.confirm` (через `iter([...])`), запускает `CliRunner().invoke(cli, ["init"])` и проверяет
  записанный `.env` — прямой шаблон для теста «после выбора provider запрашиваются только его
  credentials» (критерий 8).
- `tests/install/test_board_setup.py:171-` (`test_jira_setup_opens_official_page_only_after_...`) —
  найден по релевантности (cliff обрезал контекст) для setup-flow конкретного провайдера;
  подтверждает, что паттерн «open help page → confirm → validate» уже тестируется по каждому
  provider — можно скопировать структуру теста для доработки YouGile-хинтов о минимальных правах.
- (dropped 0 — все найденные тестовые примеры прямо релевантны выбранным граням задачи)

## Constraints / open questions
- `get_task_context` не дал связанных задач/PR — граф задач по PRI-223 пуст; весь related work
  собран через `search_tasks` + прямое чтение кода, без готового списка «linked issues».
- Voyage-лимиты (3 RPM/10K TPM) намеренно экономились: сделано 2 `search_codebase`-запроса (один
  обрезан по cliff-cutoff — «15 из 111», «10 из 23» — есть ещё релевантные совпадения за
  обрезом, не дотянутые повторным вызовом с большим ceiling); один `get_subsystem_summaries` и
  один `search_tasks` (тоже обрезан — «8 из 30»; за обрезом могут быть ещё релевантные задачи,
  не проверено). Остальная кодовая грунтовка — прямые `Read`/`grep`, т.к. задача сама называла
  конкретные файлы.
- Граф кода (`related_symbols`/`callers`/`implementations`/`definition`) не вызывался: узловых
  node_id вида `path#fqn` для функций-целей (`prompt_groups`, `resolve_branch`,
  `resolve_policy_data`) в сниппетах не потребовалось — blast radius на чтение веток закрыт
  прямым `grep` по `review_branches_list|primary_branch` (дал точные номера строк во всех
  вызывающих модулях). Если реализация захочет точный caller-граф `resolve_branch`, это отдельный
  дешёвый `callers` вызов на этапе кода.
- В `reviewer/config/layers.py` (950 строк) прочитаны только первые ~160 строк (типы ошибок,
  `home_repo_path`, secret-quarantine) — сама функция `resolve_policy_data`/`build_config_report`/
  `migrate_repo_config` (тела) не вычитаны построчно; при реализации блока `repository.*`
  потребуется полностью прочитать эти функции, а не полагаться на сигнатуры из тестов.
- Не найден (и не искался целенаправленно) код, описывающий, как MCP session-less тулы
  (`search_codebase`, `related_symbols` и т.п. в `mcp/service.py`) резолвят branch без явного
  `--branch` — есть только grep-совпадения строк 1336-1349/2227; полный путь резолва в MCP-слое
  не прочитан целиком.
- Задача явно выносит вне скоупа PRI-221 «читать `.review.yml` из рабочего дерева» — PRI-223
  наследует это ограничение: bootstrap веток тоже не может зависеть от committed `.review.yml`
  (критерий 6), только от home-слоя/git remote/CLI.
- ProviderSetupSpec/CredentialFieldSpec (`registry.py:20-66`) сейчас не имеют выделенного поля под
  «минимальные права»/«read vs write операции» — придётся решить, расширять ли dataclass или
  переиспользовать `help_text`; в брифе зафиксирован только факт отсутствия поля, решение не
  принято.

Собран на: mid tier (Sonnet-class), режим: subagent

## Токены (этап solve-task)
Модель: claude-opus-5
fresh-in 220 · out 306.8K · cache-write 1.9M · cache-read 11.5M
Всего: 13.7M токенов
