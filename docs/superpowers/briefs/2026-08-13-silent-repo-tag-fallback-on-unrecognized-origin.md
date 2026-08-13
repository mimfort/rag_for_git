# Brief — Тихая подстановка чужого repo-тега при нераспознанном URL в git remote origin
Источник: https://github.com/mimfort/rag_for_git/issues/190

## Task
`_resolve_repo` (`reviewer/entrypoints/cli.py:76-88`) при нераспознанном origin молча падает на `DEFAULT_REPO` без предупреждения; `_REMOTE_RE` (`reviewer/services/repo_id.py:6`) распознаёт только `github.com`. Три вызова (`index`, `migrate-branches`, `status`) получают чужое имя одинаково тихо. Нужно: (1) вернуть источник резолва (`explicit`/`remote`/`default`); (2) `reviewer index` с источником `default` в валидном git-клоне с нераспознанным origin — явная ошибка (`--repo owner/name`); (3) `status`/`migrate-branches` остаются fail-open, но предупреждают и `status` (текст + JSON) получает `repo_source`; (4) unit-тесты на все три источника, `--repo`/распознаваемый origin — без изменений поведения. `.venv/bin/pytest -q` должен быть зелёным.

## Related work
- ID-277 — «Перепроектировать reviewer init: конфигурация, repo-onboarding и токены» — источник паттерна `repo_source` (`"cli"`/`"git:origin"`), уже используемого в `RepositoryDetection` (`reviewer/config/onboarding.py:22-30`); новую функцию резолва стоит выровнять по тем же именам источников, а не изобретать новый enum.
- ID-274 — «Домашний слой конфига репозиториев в каталоге reviewer» — соседний потребитель `derive_repo_from_remote`/`normalize_repo`, показывает, что repo-резолв уже расщеплён на переиспользуемые примитивы в `repo_id.py`, куда и стоит добавить функцию с источником.
(dropped 6: ID-288/289/293/133/292/211 — про VCS-креды, GitLab, report_bug, finish_task, unit-изоляцию инфраструктуры; не про резолв repo-тега)

## Subsystems
- reviewer/entrypoints — три точки входа (`cli.py::_resolve_repo`, `index`, `status`, `migrate-branches`); здесь живёт вся тройная точка тихого фолбэка.
- reviewer/services — `repo_id.py` (канонизация/вывод repo), `status.py` (сборка `RepoStatus`/JSON-рендер) — оба потребуют нового поля источника.
- reviewer/config — `onboarding.py::detect_repository` уже возвращает `repo_source` для `init`/`config`-путей; жизнеспособный прецедент для имён источников и формы возврата.

## Relevant code
- `reviewer/entrypoints/cli.py:76-88` (`_resolve_repo`) — точка тихого фолбэка на `DEFAULT_REPO`; переписать на возврат `(repo, source)`. Blast radius (callers из графа) — ровно три: `index` (вызов на cli.py:939), `migrate-branches` (cli.py:1021, `_resolve_repo(repo_tag, ".", s)` — путь всегда `"."`, т.е. распознаваемость проверяется от текущего рабочего каталога, не от произвольного клона), `status` (cli.py:1087).
- `reviewer/services/repo_id.py:24-33` (`derive_repo_from_remote`) — узкий `_REMOTE_RE` (только `github.com`), но задача НЕ просит расширять regex — источник задачи фокусируется на explicit/remote/default различении, а не на GitLab-распознавании (для GitLab уже есть отдельный `derive_vcs_from_remote`, к --repo id-резолву не относится).
- `reviewer/entrypoints/cli.py:924-1013` (`index`, декораторы с 924, `def index` на 936) — команда, которую нужно сделать fail-closed при источнике `default`: сейчас никакой проверки источника нет, `repo_id = _resolve_repo(...)` используется напрямую в `update_base`/`c.store.set_index_meta` без ветвления по источнику.
- `reviewer/entrypoints/cli.py:1083-1099` (`status`) — вызывает `_resolve_repo`, затем `build_status_report`; нужно прокинуть источник в `render_status`/`render_status_json` и напечатать предупреждение при `default`.
- `reviewer/services/status.py:16-38` (`RepoStatus`/`BranchStatus`/`OverlayStatus` dataclasses) — `RepoStatus.repo: str` без источника; естественное место для нового поля `repo_source: str | None` (репо-уровневое, не branch-уровневое — источник резолва один на весь report).
- `reviewer/services/status.py:80-104` (`render_status_json`) — payload не содержит `repo_source`; добавить ключ верхнего уровня рядом с `"repo"`.
- `reviewer/services/status.py:111-140` (`render_status`) — текстовый рендер; добавить строку-предупреждение, когда источник `default`.
- `reviewer/entrypoints/cli.py:1015-1040` (`migrate_branches`, `def` на 1018) — вызывает `_resolve_repo(repo_tag, ".", s)`; нужно fail-open предупреждение аналогично `status`, без явного отказа.
- `reviewer/config/onboarding.py:70-98` (`detect_repository`) — существующий прецедент `repo_source` (`"cli"` / `"git:origin"`); НЕ вызывает `_resolve_repo` напрямую (независимый локальный путь без DEFAULT_REPO-фолбэка), но задаёт словарь имён источников для согласованности.
(dropped 3: `reviewer/config/committed.py::validate_clone` — тоже вызывает `derive_repo_from_remote` косвенно через `repo_id`, но по другой оси (сверка remote с целевым repo для fetch коммиченного `.review.yml`), не по резолву CLI-репо; `reviewer/gitutil.py#remote_url` — infra-примитив, менять не планируется; полная сводка кластера `reviewer` — фоновая, не про этот путь)

## Test exemplars
- `tests/services/test_repo_id.py:20-32` — параметризованные `test_derive_from_remote`/`test_derive_from_remote_none`: чистые unit-тесты без моков на regex-функцию; такому же стилю подражать для новой функции с источником (чистая функция, параметризация по URL → ожидаемый результат).
- `tests/entrypoints/test_cli.py:223-260` (`test_index_derives_repo_from_git_remote`) — паттерн мокирования CLI-команды `index`: `@patch` на `reviewer.entrypoints.cli.remote_url`, `Settings`, `build_components` и т.д., `CliRunner.invoke(cli, [...])`, проверка `result.exit_code`/`result.output`. Новый тест «нераспознанный origin + DEFAULT_REPO → index падает» и «status предупреждает + `repo_source: default`» должны следовать этому же fixture-набору (`runner`, `fake_components`, `fake_settings`, `monkeypatch.setenv("XDG_CONFIG_HOME", ...)`).
- `tests/skills/test_configure_review_skill.py:352-359` — косвенный ориентир: проверяет, что офлайн-путь резолва repo в скилле не делает сетевых вызовов (`git fetch`/`ls-remote`/`remote show` запрещены) и печатает `source`; полезно как sanity-check формата слова "source" в выводе.

## Constraints / open questions
- Доска YouGile недоступна (HTTP 502); задача НЕ заведена на доске (нет ключа); корпус задач в reviewer-сторе не обновлялся в этом прогоне (последний живой синк — раньше).
- Черновик задачи лежит в `/private/tmp/claude-501/-Users-aleksejzadoroznyj-PycharmProjects-rag-for-git/72b5c1cc-0c96-4115-947c-38d5940bdcf5/scratchpad/task-draft-repo-id-silent-fallback.md` и ждёт создания через `create_task`, когда доска поднимется.
- `migrate_branches` всегда резолвит от `"."` (текущий рабочий каталог), а не от произвольного `path`-аргумента — при добавлении предупреждения источник читается от текущего клона CLI-процесса, это не баг, но стоит сохранить это поведение явно.
- `_REMOTE_RE` расширять на GitLab/self-hosted вне скоупа этой задачи (нет явного критерия в issue); `derive_vcs_from_remote` уже отдельно поддерживает GitLab для `repo_vcs`, но `--repo`-id резолв — другая ось.
- Открытый вопрос дизайна: `_resolve_repo` возвращает один `str`; переход на `(repo, source)`/dataclass — breaking change сигнатуры для всех трёх вызывающих сайтов (`index`/`status`/`migrate-branches`) — синхронизировать сигнатуру и call-sites в одном PR.
- Открытый вопрос: `RepoStatus.repo_source` — на уровне repo (весь report) или нужно ли per-branch — по формулировке задачи это repo-уровневый факт (один резолв на вызов команды), per-branch не требуется.

Собран на: mid (sonnet), сборка: subagent. Номера строк по cli.py сверены и поправлены оркестратором.
