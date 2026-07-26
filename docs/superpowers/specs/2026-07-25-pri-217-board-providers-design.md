# PRI-217 — Реестр досок: 8 новых провайдеров. Дизайн

Бриф: `docs/superpowers/briefs/2026-07-25-PRI-217-board-registry-8-providers.md`
Задача: https://ru.yougile.com/team/686c049c8af8/#PRI-217
Факты по внешним API (источник истины для эндпоинтов): `board-apis-research.md` (scratchpad сессии,
копируется в ветку как `docs/board-apis-research.md` в фазе C).

Цель: `default_board_registry()` даёт 11 типов вместо 3. Добавляются: `github` (Issues), `trello`,
`linear`, `clickup`, `asana`, `yandex_tracker`, `kaiten`, `weeek`.

## Что уже готово (проверено чтением кода, не предполагается)

- `TaskBoardProvider` Protocol — 9 методов (`reviewer/tasks/boards/base.py:46-123`), `RawTask` (`:26-43`).
- Реестр: `BoardProviderSpec`/`CredentialFieldSpec`/`ProviderOptionSpec`/`ProviderSetupSpec`,
  runtime-валидация полноты по `_REQUIRED_PROVIDER_MEMBERS`, guard `_contains_secret`,
  `default_board_registry()` с тремя явными строками (`registry.py`).
- Единая точка создания — `make_board_provider`/`make_board_providers` (`boards/__init__.py`),
  единый lifecycle — `resolved_provider` (`boards/runtime.py`).
- Транспорт `BoardHttpClient` (`http.py`): retry только для read, `Retry-After` (число и HTTP-date),
  категоризация статусов в `BoardProviderError`.
- Ошибки/сан-тайз: `errors.py` (`BoardProviderError`, `sanitize_provider_text/payload`),
  `reporting.py` (`sanitize_validation_report`).
- Интерактивная настройка: `SetupIO`/`ClickSetupIO`/`configure_board_provider` (`setup.py`) — три
  режима получения кредов: кастомный `acquisition`, `help_url_builder` (двухфазный prompt), дефолтный
  `_prompt_credentials`.
- Разметка: `markup.py` (`md_to_html`/`html_to_md`), `adf.py` (Jira ADF).
- Contract-suite: `ProviderContract` — 13 тестов, фейки на `httpx.MockTransport`, `ADAPTERS`
  (`tests/tasks/boards/contract.py`).
- **Критерий 7 уже структурно выполнен**: `reviewer/config/settings.py`,
  `provider_credentials.py`, `task_board.py`, `tasks/sync.py`, `mcp/service.py`, `install.py`,
  `entrypoints/cli.py` не содержат ни одного упоминания `yougile`/`youtrack`/`jira` — ветвления по
  типу нет нигде, кроме `.env.example` (документация) и `default_board_registry()` (сам allowlist).
  Инвариант, который нельзя нарушить новыми провайдерами.

## Решения по открытым вопросам

### D1. Общий транспорт делаем ДО адаптеров, старые 3 не ретрофитим
Шаг 7 задачи — новые файлы не должны копировать httpx-обвязку `jira.py`/`yougile.py`/`youtrack.py`.
Добавляем (не ломая существующее):
- `boards/restbase.py` — `RestBoardBase`: `httpx.Client` (base_url, headers, timeout, **обязательная
  инъекция `transport=`** как у `JiraCloudBoard` — фикстуры не должны подменять `_client` постфактум),
  `_read`/`_write` через `BoardHttpClient`, `close()`, хранение `secrets`, `key_pattern`,
  `url_template`, `_task_url(code)`.
- `boards/pagination.py` — чистые генераторы под 4 модели: `paginate_offset`, `paginate_page`,
  `paginate_cursor`, `paginate_link_header` (GitHub `Link: rel="next"`). Тестируются без сети.
- `boards/graphql.py` — `GraphQLClient` поверх `BoardHttpClient`: `execute(query, variables, *,
  operation)`, категоризация `errors[].extensions.code` в `BoardProviderError`, курсорная пагинация
  по `pageInfo.hasNextPage/endCursor` (нужен для Linear).
- `BoardHttpClient` расширяется одним необязательным параметром
  `rate_limit_hint: Callable[[Mapping[str,str]], float | None]` — провайдер-специфичное чтение
  заголовков лимита (GitHub `X-RateLimit-Remaining/Reset`, Linear complexity). Дефолт — текущее
  поведение, обратная совместимость сохранена.

**Ретрофит существующих трёх адаптеров — вне скоупа PRI-217**: задача требует лишь чтобы новые файлы
не копировали логику; ретрофит переписал бы три зелёных адаптера и их 15 тестовых файлов, умножив
риск регрессии на эпике. Фиксируем как отдельный технический долг в `docs/board-providers.md`.

### D2. Канонический ключ задачи: `key_prefix` как provider option
Система скоупит выдачу по проекту (`project_prefix`, `.review.yml task_board.key_pattern`), а
внешние доски дают разные ключи. Правило:
- Нативные человекочитаемые ключи есть у `linear` (`ENG-123`) и `yandex_tracker` (`QUEUE-123`) —
  используем их как `key`.
- Для `github`, `trello`, `clickup`, `asana`, `kaiten`, `weeek` вводим
  `ProviderOptionSpec(key="key_prefix", required_for=("sync",))`: `key = f"{key_prefix}-{number}"`,
  где `number` — стабильный числовой идентификатор доски (GitHub issue number, Trello `idShort`,
  ClickUp `custom_id`/числовой id, Asana gid, Kaiten/Weeek номер карточки).
- Нативный id доски всегда попадает в `aliases` и в `RawTask.board_id`, чтобы write-операции
  (`finish`, `fetch_one`) не зависели от синтезированного ключа.
- Если у доски есть штатные custom ids (ClickUp), они имеют приоритет над синтезом.

### D3. Никакого OAuth loopback — только PAT/API-ключи
Плагин работает в разных CLI (Claude Code, Codex, Cursor, Gemini) и на разных ОС, в том числе
headless и по SSH. Локальный redirect-сервер требует свободного порта и браузера на той же машине,
поэтому исключается. Разрешены только: ввод токена через `_prompt_credentials`, подсказка страницы
через `help_url`/`help_url_builder`, `io.open_url` (`click.launch`, кроссплатформенный). Кастомный
`acquisition` — только для случаев «логин+пароль → API-ключ по REST», как `acquire_yougile_key`.

### D4. Кроссплатформенность (требование пользователя)
- Адаптеры — чистый Python + httpx: ни `subprocess`, ни shell, ни POSIX-путей, ни `os.environ`-мутаций.
- Пути (если понадобятся) — только `pathlib.Path` и существующие хелперы `install.py`
  (`default_env_path`, `_home()` с `APPDATA` для Windows).
- Явные timeout'ы у каждого клиента: медленная сеть не должна вешать CLI без предела.
- Никаких зависимостей от локали/часового пояса при парсинге дат: только UTC-aware разбор в epoch ms.
- Unit-тесты без сети (`httpx.MockTransport`), как требует политика `tests/infrastructure_policy.py`.

### D5. Contract-фикстура становится параллелизуемой
Сейчас `ProviderAdapter.provider_factory` — dict-диспетчер по `board_type` внутри `contract.py`, а
пороги теста захардкожены (`>1000 if yougile else >200`, пути страниц
`("/tasks","/issues","/search/jql")`). Восемь провайдеров в одном файле = конфликт правок и рост
файла. Рефакторинг в фазе A:
- `tests/tasks/boards/fakes/<type>.py` — по файлу на провайдера: свой `State`, handler и
  `ADAPTER: ProviderAdapter`.
- `ProviderAdapter` получает поля `factory: Callable[..., tuple[TaskBoardProvider, FakeState]]`,
  `min_rows: int`, `page_paths: tuple[str, ...]` — тест больше ничего не хардкодит.
- Общий `FakeState` (`calls`, `closed`) в `fakes/base.py`; provider-специфичное состояние — в подклассе.
- `ADAPTERS` в `contract.py` собирается из явного списка импортов — добавление провайдера = одна строка.
Существующие yougile/youtrack/jira переносятся в этот формат в фазе A; их тесты должны остаться зелёными.

### D6. Нормализация описания в markdown (критерий 4)
- `github`, `linear`, `clickup` (`markdown_description`), `trello` (`desc`) — markdown нативно, конвертация не нужна.
- `asana` — `html_notes` → `markup.html_to_md`.
- `kaiten`, `weeek` — по факту из research-файла: markdown → как есть; HTML → `html_to_md`.
- `yandex_tracker` — YFM. Добавляем узкий `boards/yfm.py::yfm_to_md`: разворачивает
  Tracker-специфичные конструкции (`%%code%%`, cut, `<{...}>`) в markdown-эквивалент; неизвестные
  конструкции оставляет как есть (fail-soft, читаемо), никогда не бросает.

### D7. GraphQL в contract-suite (Linear)
`httpx.MockTransport` подходит без изменений: handler роутит по `operationName`/подстроке `query` из
`request.content` вместо пути. Требование `page_calls >= 2` покрывается двумя POST `/graphql` с
разными курсорами; `page_paths=("/graphql",)`.

### D8. Поставка — одна ветка, один PR, коммит на провайдера
Ветка `feature/pri-217-board-providers` от `dev`. Коммит A — общий транспорт + рефакторинг фикстур,
затем по коммиту на провайдера, каждый из которых даёт **полную** регистрацию типа (adapter → spec →
строка реестра → фикстура → provider-тесты → строка матрицы) — инвариант «partial registration
запрещена» не нарушается ни на одном коммите. Финальные коммиты — документация, README ×2,
версия + манифесты плагина.

### D9. Что НЕ входит в скоуп
Ретрофит трёх существующих адаптеров на `RestBoardBase` (D1); OAuth-флоу (D3); новые ветки по типу в
`SyncService`/`Settings`/installer (критерий 7 запрещает); вложения, требующие отдельной авторизации
скачивания, если у доски нет публичного/токен-совместимого URL — тогда `attachments` отдаём с
метаданными без текста и пишем warning (как уже делает `attachments.py` при offhost-блокировке).

## Порядок реализации

- **Фаза A (последовательно, 1 агент):** `restbase.py`, `pagination.py`, `graphql.py`,
  `rate_limit_hint` в `http.py`, `yfm.py`; рефакторинг `tests/tasks/boards/fakes/*` + `contract.py`.
  Гейт: `pytest tests/tasks/boards -q` зелёный на трёх старых провайдерах, ruff чист.
- **Фаза B (параллельно, 8 агентов Opus 5):** по адаптеру на агента —
  `reviewer/tasks/boards/<type>.py`, `tests/tasks/boards/fakes/<type>.py`,
  `tests/tasks/boards/test_<type>_*.py`. Каждый возвращает: строку регистрации, строку импорта
  фикстуры, готовую секцию для `docs/board-providers.md`, строку матрицы, блок для `.env.example`.
  Общие файлы (`registry.py`, `contract.py`, docs) агенты **не правят** — интеграция в фазе C.
- **Фаза C (последовательно, оркестратор):** 8 строк в `default_board_registry()`, 8 строк в
  `ADAPTERS`, `.env.example`, `docs/board-providers.md` (матрица + секции + долг из D1),
  `README.md`/`README.ru.md`, `CLAUDE.md` (если меняются инварианты), bump версии в `pyproject.toml`,
  `python scripts/update_codex_plugin_manifest.py`.
- **Фаза D (верификация):** `pytest tests/tasks/boards -q` → `pytest -q` (полный unit-прогон) →
  `ruff check .` по новым файлам → проверка критериев 1/2/6/7 отдельными тестами.
