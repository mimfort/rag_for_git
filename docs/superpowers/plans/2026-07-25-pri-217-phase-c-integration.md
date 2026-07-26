# PRI-217 Фаза C — интеграция, документация, релиз

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Замкнуть эпик: зарегистрировать восемь готовых адаптеров, подключить их фейки к общей
contract-suite, обновить `.env.example`, документацию и оба README, бампнуть версию и пересобрать
манифесты плагина.

**Architecture:** Все восемь адаптеров и их фейки уже написаны в фазе B и лежат в
`reviewer/tasks/boards/<type>.py` и `tests/tasks/boards/fakes/<type>.py`, но **ещё не подключены**:
`default_board_registry()` их не регистрирует, `ADAPTERS` их не перечисляет. Эта фаза добавляет
по одной явной строке на провайдера в каждой точке подключения, делает guard-тесты документации
generic (от реестра, а не от захардкоженных трёх колонок) и транспонирует матрицу возможностей
так, чтобы строка соответствовала доске.

**Tech Stack:** Python 3.11–3.13, pytest, ruff, `scripts/update_codex_plugin_manifest.py`.

## Global Constraints

- Восемь типов ровно в этом порядке и написании: `github`, `trello`, `linear`, `clickup`, `asana`,
  `yandex_tracker`, `kaiten`, `weeek`.
- `reviewer/config/settings.py`, `provider_credentials.py`, `task_board.py`, `tasks/sync.py`,
  `mcp/service.py`, `install.py`, `entrypoints/cli.py` не получают ни одного упоминания конкретного
  типа доски — расширение только через реестр (критерий 7). Проверено: сейчас там ноль упоминаний.
- Wizard (`install.WIZARD_GROUPS` / `board_env_group`) и его тест
  `tests/test_install_wizard.py::test_wizard_field_count_tracks_registry_credentials` уже generic —
  правки не требуют, но должны остаться зелёными.
- `docs/board-providers.md`, `README.md` — по-английски; `README.ru.md`, `CLAUDE.md` — по-русски.
  README-файлы обязаны перечислять доски одинаково (критерий 8).
- Коммиты: Conventional Commits на русском, без self-attribution.
- Любая правка контента под `plugin/` **или** бамп `version` в `pyproject.toml` требует
  `python scripts/update_codex_plugin_manifest.py`, иначе install-тесты падают.

---

### Task 1: Регистрация восьми типов + guard-тесты реестра

**Files:**
- Modify: `reviewer/tasks/boards/registry.py:220-234` (`default_board_registry`)
- Test: `tests/tasks/boards/test_registry.py` (дописать)

**Interfaces:**
- Consumes: `provider_spec()` из восьми модулей фазы B —
  `reviewer.tasks.boards.{github,trello,linear,clickup,asana,yandex_tracker,kaiten,weeek}`.
- Produces: `default_board_registry().registered_types() == ("yougile", "youtrack", "jira", "github",
  "trello", "linear", "clickup", "asana", "yandex_tracker", "kaiten", "weeek")` — 11 типов.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/tasks/boards/test_registry.py — дописать в конец
EXPECTED_TYPES = (
    "yougile",
    "youtrack",
    "jira",
    "github",
    "trello",
    "linear",
    "clickup",
    "asana",
    "yandex_tracker",
    "kaiten",
    "weeek",
)


def test_registry_exposes_all_eleven_provider_types_in_order():
    assert default_board_registry().registered_types() == EXPECTED_TYPES


@pytest.mark.parametrize("board_type", EXPECTED_TYPES)
def test_every_registered_spec_is_complete_and_immutable(board_type: str):
    spec = default_board_registry().get(board_type)

    assert spec.board_type == board_type
    assert callable(spec.factory)
    assert spec.credential_fields
    assert spec.setup.label and spec.setup.help_url and spec.setup.help_text
    assert dataclasses.is_dataclass(spec) and spec.__dataclass_params__.frozen
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.board_type = "mutated"  # type: ignore[misc]


@pytest.mark.parametrize("board_type", EXPECTED_TYPES)
def test_registered_provider_passes_runtime_member_validation(board_type: str):
    registry = default_board_registry()
    spec = registry.get(board_type)
    provider = spec.factory(
        ProviderBuildContext(
            credentials={field.env: f"value-{field.env}" for field in spec.credential_fields},
            options={},
            key_pattern=r"PRI-\d+",
            url_template="https://board.test/{code}",
            attachment_max_bytes=1000,
            attachment_timeout=1.0,
            attachment_store_chars=1000,
        )
    )
    try:
        BoardProviderRegistry._validate_runtime_provider(provider, board_type)
    finally:
        provider.close()


@pytest.mark.parametrize("board_type", EXPECTED_TYPES)
def test_secret_credential_value_is_rejected_as_provider_option(board_type: str):
    registry = default_board_registry()
    spec = registry.get(board_type)
    secret_fields = [field for field in spec.credential_fields if field.secret]
    assert secret_fields, f"{board_type}: у провайдера должен быть хотя бы один секретный field"
    secret_value = "s3cret-value-long-enough"
    option_key = spec.option_fields[0].key if spec.option_fields else "unknown_option"

    with pytest.raises(ValueError):
        registry.create(
            board_type,
            credentials={
                field.env: (secret_value if field.secret else f"value-{field.env}")
                for field in spec.credential_fields
            },
            options={option_key: f"prefix-{secret_value}-suffix"},
            build_defaults={
                "key_pattern": r"PRI-\d+",
                "url_template": "https://board.test/{code}",
                "attachment_max_bytes": 1000,
                "attachment_timeout": 1.0,
                "attachment_store_chars": 1000,
            },
        )
```

Добавить в шапку файла недостающие импорты: `import dataclasses`, `import pytest`,
`from reviewer.tasks.boards.registry import BoardProviderRegistry, ProviderBuildContext,
default_board_registry`.

- [ ] **Step 2: Прогнать — падают**

Run: `.venv/bin/pytest tests/tasks/boards/test_registry.py -q`
Expected: FAIL — `registered_types()` возвращает три типа; `registry.get("github")` бросает `KeyError`.

- [ ] **Step 3: Добавить восемь строк регистрации**

```python
@lru_cache(maxsize=1)
def default_board_registry() -> BoardProviderRegistry:
    """Production registry только полностью реализованных адаптеров.

    Локальные явные импорты не создают цикл при инициализации modules.
    """
    from reviewer.tasks.boards.asana import provider_spec as asana_provider_spec
    from reviewer.tasks.boards.clickup import provider_spec as clickup_provider_spec
    from reviewer.tasks.boards.github import provider_spec as github_provider_spec
    from reviewer.tasks.boards.jira import provider_spec as jira_provider_spec
    from reviewer.tasks.boards.kaiten import provider_spec as kaiten_provider_spec
    from reviewer.tasks.boards.linear import provider_spec as linear_provider_spec
    from reviewer.tasks.boards.trello import provider_spec as trello_provider_spec
    from reviewer.tasks.boards.weeek import provider_spec as weeek_provider_spec
    from reviewer.tasks.boards.yandex_tracker import provider_spec as yandex_tracker_provider_spec
    from reviewer.tasks.boards.yougile import provider_spec as yougile_provider_spec
    from reviewer.tasks.boards.youtrack import provider_spec as youtrack_provider_spec

    return BoardProviderRegistry([
        yougile_provider_spec(),
        youtrack_provider_spec(),
        jira_provider_spec(),
        github_provider_spec(),
        trello_provider_spec(),
        linear_provider_spec(),
        clickup_provider_spec(),
        asana_provider_spec(),
        yandex_tracker_provider_spec(),
        kaiten_provider_spec(),
        weeek_provider_spec(),
    ])
```

- [ ] **Step 4: Прогнать — зелено**

Run: `.venv/bin/pytest tests/tasks/boards/test_registry.py -q && .venv/bin/pytest tests/config tests/test_install_wizard.py -q`
Expected: PASS. Если `test_wizard_field_count_tracks_registry_credentials` падает — значит
`board_env_group` где-то не generic; исправлять его, а не тест.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/tasks/boards/registry.py tests/tasks/boards/test_registry.py
git commit -m "feat(boards): зарегистрировать восемь новых типов досок"
```

---

### Task 2: Подключить восемь фейков к общей contract-suite

**Files:**
- Modify: `tests/tasks/boards/contract.py` (функция `_adapters()`)
- Test: `tests/tasks/boards/test_provider_contract.py` (проверка полноты параметризации)

**Interfaces:**
- Consumes: `ADAPTER` из `tests/tasks/boards/fakes/{github,trello,linear,clickup,asana,yandex_tracker,kaiten,weeek}.py`.
- Produces: `ADAPTERS` длиной 11; каждый тест `ProviderContract` исполняется для всех 11 типов.

- [ ] **Step 1: Написать падающий тест полноты**

```python
# tests/tasks/boards/test_provider_contract.py — дописать
def test_contract_suite_covers_every_registered_provider():
    from reviewer.tasks.boards.registry import default_board_registry
    from tests.tasks.boards.contract import ADAPTERS

    covered = {adapter.board_type for adapter in ADAPTERS}
    assert covered == set(default_board_registry().registered_types())
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_provider_contract.py::test_contract_suite_covers_every_registered_provider -q`
Expected: FAIL — в `covered` нет восьми новых типов.

- [ ] **Step 3: Расширить сборку адаптеров**

```python
def _adapters() -> tuple[ProviderAdapter, ...]:
    """Явный список зарегистрированных фейков: одна строка на провайдера."""
    from tests.tasks.boards.fakes import (
        asana,
        clickup,
        github,
        jira,
        kaiten,
        linear,
        trello,
        weeek,
        yandex_tracker,
        yougile,
        youtrack,
    )

    return (
        yougile.ADAPTER,
        youtrack.ADAPTER,
        jira.ADAPTER,
        github.ADAPTER,
        trello.ADAPTER,
        linear.ADAPTER,
        clickup.ADAPTER,
        asana.ADAPTER,
        yandex_tracker.ADAPTER,
        kaiten.ADAPTER,
        weeek.ADAPTER,
    )
```

- [ ] **Step 4: Прогнать всю contract-suite на 11 провайдерах**

Run: `.venv/bin/pytest tests/tasks/boards -q`
Expected: PASS — 13 contract-тестов × 11 адаптеров + provider-специфичные тесты.

- [ ] **Step 5: Коммит**

```bash
git add tests/tasks/boards/contract.py tests/tasks/boards/test_provider_contract.py
git commit -m "test(boards): параметризовать contract-suite всеми 11 адаптерами"
```

---

### Task 3: `.env.example` — блоки кредов восьми досок

**Files:**
- Modify: `.env.example` (после блока Jira, до legacy-секции)
- Test: `tests/docs/test_board_provider_docs.py::test_env_template_leaves_all_registry_credentials_unconfigured` (существующий, должен пройти)

**Interfaces:**
- Consumes: `credential_fields` каждого нового spec.
- Produces: в `.env.example` присутствует пустая запись `<ENV>=` для каждого env любого
  зарегистрированного провайдера, включая алиасы.

- [ ] **Step 1: Прогнать существующий guard — убедиться, что падает**

Run: `.venv/bin/pytest tests/docs/test_board_provider_docs.py::test_env_template_leaves_all_registry_credentials_unconfigured -q`
Expected: FAIL — новых env нет в шаблоне (`values.get(env, "") == ""` не выполняется, т.к. ключа нет).

- [ ] **Step 2: Получить точный список env из реестра**

Run:
```bash
.venv/bin/python -c "
from reviewer.tasks.boards.registry import default_board_registry
r = default_board_registry()
for t in r.registered_types():
    spec = r.get(t)
    print(t, [(f.env, f.secret, f.default) for f in spec.credential_fields])
"
```
Использовать вывод как источник истины для следующего шага (не выдумывать имена).

- [ ] **Step 3: Дописать блоки в `.env.example`**

Формат — как у существующих блоков: комментарий со ссылкой на официальную страницу токена, затем
`ENV=` пустым значением. Пример для GitHub Issues (остальные — по тому же образцу, с их env из Step 2):

```
# GitHub Issues: https://github.com/settings/tokens (fine-grained PAT: Issues read/write,
# Metadata read; для приватных репозиториев ещё Contents read).
# Токен ревью (GITHUB_TOKEN) переиспользуется, если отдельный board-токен не задан.
GITHUB_ISSUES_TOKEN=
# Base URL API; пусто → https://api.github.com. Для GitHub Enterprise — https://<host>/api/v3.
GITHUB_ISSUES_API_BASE=
```

Ограничения формата, которые проверяет guard: значение всегда пустое (`ENV=`), после `=` не должно
идти пробелов с комментарием (регекс `^[A-Z][A-Z0-9_]*=[ \t]+#` запрещён).

- [ ] **Step 4: Прогнать guard-тесты документации**

Run: `.venv/bin/pytest tests/docs -q`
Expected: `test_env_template_leaves_all_registry_credentials_unconfigured` — PASS.
Тест матрицы (`test_authoritative_provider_reference_has_full_capability_matrix`) на этом шаге
ещё может быть зелёным — он переписывается в Task 4.

- [ ] **Step 5: Коммит**

```bash
git add .env.example
git commit -m "docs(env): шаблоны кредов восьми новых досок"
```

---

### Task 4: Матрица возможностей — транспонировать и сделать guard generic

Матрица «возможность в строке, доска в колонке» на 11 досках нечитаема, а критерий приёмки 8
требует **строку матрицы на каждую доску**. Транспонируем и переписываем guard так, чтобы он
охранял полноту от реестра, а не от трёх захардкоженных колонок.

**Files:**
- Modify: `docs/board-providers.md:7-19` (секция `## Capability matrix`)
- Modify: `tests/docs/test_board_provider_docs.py:21-39` (`test_authoritative_provider_reference_has_full_capability_matrix`)

**Interfaces:**
- Consumes: `default_board_registry().registered_types()`.
- Produces: в `docs/board-providers.md` есть заголовок
  `| Provider | Sync/pagination | Markdown normalization | Links/subtasks | Attachments | Single read | Discovery | Create/target | Finish/PR link | Write-through |`
  и по одной строке на каждый зарегистрированный тип, начинающейся с `| <human name> (<board_type>) |`.

- [ ] **Step 1: Переписать guard-тест на generic-проверку**

```python
PROVIDER_MATRIX_CAPABILITIES = (
    "Sync/pagination",
    "Markdown normalization",
    "Links/subtasks",
    "Attachments",
    "Single read",
    "Discovery",
    "Create/target",
    "Finish/PR link",
    "Write-through",
)


def test_authoritative_provider_reference_has_full_capability_matrix():
    text = _read("docs/board-providers.md")
    header = "| Provider | " + " | ".join(PROVIDER_MATRIX_CAPABILITIES) + " |"

    assert header in text
    matrix = text.split("## Capability matrix", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    rows = [line for line in matrix.splitlines() if line.startswith("| ") and "---" not in line]
    documented = {
        cell.strip()
        for line in rows[1:]
        for cell in [line.split("|")[1]]
    }

    for board_type in default_board_registry().registered_types():
        assert any(f"({board_type})" in name for name in documented), board_type
    # у каждой строки доски заполнены все 9 колонок возможностей
    for line in rows[1:]:
        assert len(line.split("|")) == len(PROVIDER_MATRIX_CAPABILITIES) + 3, line
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/pytest tests/docs/test_board_provider_docs.py::test_authoritative_provider_reference_has_full_capability_matrix -q`
Expected: FAIL — в документе старая (транспонированная иначе) матрица, нового заголовка нет.

- [ ] **Step 3: Переписать секцию матрицы в `docs/board-providers.md`**

```markdown
## Capability matrix

One row per provider; every registered board type must appear here (a missing row means the
provider is not supported — see "Adding a provider").

| Provider | Sync/pagination | Markdown normalization | Links/subtasks | Attachments | Single read | Discovery | Create/target | Finish/PR link | Write-through |
|---|---|---|---|---|---|---|---|---|---|
| YouGile (yougile) | ✓ | HTML↔MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| YouTrack (youtrack) | ✓ | Native MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Jira Cloud (jira) | ✓ | ADF↔MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| GitHub Issues (github) | ✓ | Native MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Trello (trello) | ✓ | Native MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Linear (linear) | ✓ | Native MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| ClickUp (clickup) | ✓ | Native MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Asana (asana) | ✓ | HTML↔MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Yandex Tracker (yandex_tracker) | ✓ | YFM→MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Kaiten (kaiten) | ✓ | Native MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Weeek (weeek) | ✓ | HTML↔MD | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
```

Колонку `Markdown normalization` заполнять по факту реализации адаптера (значение из отчёта
соответствующего агента фазы B), а не по этому образцу: если Kaiten или Weeek оказались с другим
форматом описания, поставить фактическое (`HTML↔MD` / `Native MD`).

- [ ] **Step 4: Прогнать guard-тесты документации целиком**

Run: `.venv/bin/pytest tests/docs -q`
Expected: PASS. Тест
`test_provider_reference_documents_safe_credentials_and_jira_cloud_boundary` требует, чтобы имена
провайдеров и их env упоминались в документе — их секции добавляются в Task 5.

- [ ] **Step 5: Коммит**

```bash
git add docs/board-providers.md tests/docs/test_board_provider_docs.py
git commit -m "docs(boards): матрица возможностей строкой на доску"
```

---

### Task 5: Секции setup/rotation на каждую новую доску + технический долг D1

**Files:**
- Modify: `docs/board-providers.md` (новые секции `## GitHub Issues` … `## Weeek`, перед `## Legacy migration`)
- Test: `tests/docs/test_board_provider_docs.py` (дописать generic-проверку секций)

**Interfaces:**
- Consumes: отчёты агентов фазы B (env-поля, options, целевые состояния, ссылки на официальную доку).
- Produces: на каждый тип — секция с заголовком `## <Human name>`, перечислением его env,
  словами `hidden input`, `reviewer check`, `rotation`, и списком provider options.

- [ ] **Step 1: Написать падающий generic-тест секций**

```python
def test_every_registered_provider_has_setup_and_rotation_section():
    text = _read("docs/board-providers.md")
    registry = default_board_registry()

    for board_type in registry.registered_types():
        spec = registry.get(board_type)
        section = text.split(f"({board_type})", maxsplit=1)
        assert len(section) == 2, board_type  # присутствует в матрице
        for field in spec.credential_fields:
            assert field.env in text, (board_type, field.env)
        for option in spec.option_fields:
            assert option.key in text, (board_type, option.key)
    assert text.lower().count("rotation") >= len(registry.registered_types())


def test_provider_reference_records_shared_transport_debt():
    text = _read("docs/board-providers.md")
    assert "RestBoardBase" in text
    assert "yougile.py" in text and "youtrack.py" in text and "jira.py" in text
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/pytest tests/docs/test_board_provider_docs.py -q -k "setup_and_rotation or transport_debt"`
Expected: FAIL — секций новых досок и записи о долге нет.

- [ ] **Step 3: Дописать секции по образцу существующих**

Образец структуры (заполнять фактами из отчёта агента фазы B и `docs/board-apis-research.md`):

```markdown
## GitHub Issues

Board type: `github`. REST API v3 (`https://api.github.com`, GitHub Enterprise — `https://<host>/api/v3`).

Credentials: `GITHUB_ISSUES_TOKEN` (secret, hidden input), `GITHUB_ISSUES_API_BASE` (optional).
Create a fine-grained PAT at https://github.com/settings/tokens with Issues read/write and Metadata
read; add Contents read for private repositories. The token is never returned by
`get_board_config`, `get_board_targets` or MCP errors.

Provider options: `repo` (`owner/name`, required for sync/create/finish), `key_prefix` (required for
sync — canonical task keys are `<key_prefix>-<issue number>`).

Targets: labels and milestones are discovery targets for `create_target`/`done_target`; closing an
issue sets `state=closed`.

Rotation: issue a new PAT, update `GITHUB_ISSUES_TOKEN`, run `reviewer check --board-project
github=<PROJECT>`, then revoke the previous token.
```

В конец секции `## Adding a provider` добавить запись о долге:

```markdown
### Shared transport debt

`RestBoardBase` (`reviewer/tasks/boards/restbase.py`) is the shared REST skeleton for every provider
added in PRI-217. The three original adapters — `yougile.py`, `youtrack.py`, `jira.py` — still carry
their own httpx wrappers; retrofitting them onto `RestBoardBase` is deliberately out of scope for
PRI-217 and tracked as follow-up work. New adapters must not copy those wrappers.
```

- [ ] **Step 4: Прогнать — зелено**

Run: `.venv/bin/pytest tests/docs -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add docs/board-providers.md tests/docs/test_board_provider_docs.py
git commit -m "docs(boards): setup и rotation восьми новых досок"
```

---

### Task 6: README.md и README.ru.md — синхронно

**Files:**
- Modify: `README.md:566-574` (таблица env) + место, где перечисляются поддерживаемые доски
- Modify: `README.ru.md:507-515` (та же таблица) + аналогичное место
- Test: `tests/docs/test_board_provider_docs.py` (дописать проверку симметрии)

**Interfaces:**
- Consumes: env-поля из реестра.
- Produces: оба README содержат одинаковый набор `board_type` и одинаковый набор env-строк.

- [ ] **Step 1: Написать падающий тест симметрии**

```python
def test_readmes_list_the_same_registered_boards():
    registry = default_board_registry()
    english = _read("README.md")
    russian = _read("README.ru.md")

    for board_type in registry.registered_types():
        assert board_type in english, board_type
        assert board_type in russian, board_type
        for field in registry.get(board_type).credential_fields:
            assert field.env in english, (board_type, field.env)
            assert field.env in russian, (board_type, field.env)
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/pytest tests/docs/test_board_provider_docs.py::test_readmes_list_the_same_registered_boards -q`
Expected: FAIL — новых типов и env в README нет.

- [ ] **Step 3: Дописать строки в обе таблицы**

В `README.md` в таблицу env (рядом с существующими строками YouGile/YouTrack/Jira) — по строке на
доску, английским:

```markdown
| `GITHUB_ISSUES_TOKEN` / `GITHUB_ISSUES_API_BASE` | `""` | GitHub Issues credentials; the base defaults to `https://api.github.com`. |
```

В `README.ru.md` — та же строка по-русски:

```markdown
| `GITHUB_ISSUES_TOKEN` / `GITHUB_ISSUES_API_BASE` | `""` | Креды GitHub Issues; base по умолчанию `https://api.github.com`. |
```

Плюс в обоих README в месте, где перечисляются поддерживаемые доски, привести полный список из 11
типов и сослаться на `docs/board-providers.md` как на источник истины.

- [ ] **Step 4: Прогнать guard-тесты документации**

Run: `.venv/bin/pytest tests/docs -q`
Expected: PASS (в том числе существующие
`test_readmes_document_store_first_server_side_board_workflow_symmetrically` и
`test_public_docs_use_registered_provider_terminology_not_a_closed_choice`).

- [ ] **Step 5: Коммит**

```bash
git add README.md README.ru.md tests/docs/test_board_provider_docs.py
git commit -m "docs(readme): перечислить 11 поддерживаемых досок в обоих README"
```

---

### Task 7: `CLAUDE.md` — обновить инвариант реестра

**Files:**
- Modify: `CLAUDE.md` (пункт «Реестр досок и `task_board`» в разделе «Неочевидные факты»)

**Interfaces:**
- Consumes: итоговый состав реестра.
- Produces: в `CLAUDE.md` указано, что зарегистрировано 11 типов, и упомянуты общие модули
  (`restbase.py`, `pagination.py`, `graphql.py`, `yfm.py`) как обязательная основа новых адаптеров.

- [ ] **Step 1: Дописать факты в существующий пункт**

Добавить в пункт про реестр досок (не переписывая остальное):

```markdown
  Зарегистрировано 11 типов: yougile, youtrack, jira, github, trello, linear, clickup, asana,
  yandex_tracker, kaiten, weeek. Новый адаптер строится на общих модулях
  `boards/restbase.py` (REST-скелет), `boards/pagination.py` (4 модели пагинации),
  `boards/graphql.py` (GraphQL) и не заводит собственную httpx-обвязку; три исторических адаптера
  (yougile/youtrack/jira) ещё живут на своих обёртках — это зафиксированный долг, а не образец.
```

- [ ] **Step 2: Проверить guard-тесты, читающие CLAUDE.md**

Run: `.venv/bin/pytest tests/docs -q && .venv/bin/pytest tests/skills -q`
Expected: PASS.

- [ ] **Step 3: Коммит**

```bash
git add CLAUDE.md
git commit -m "docs(claude): 11 типов досок и общие модули транспорта"
```

---

### Task 8: Бамп версии и пересборка манифестов плагина

**Files:**
- Modify: `pyproject.toml:3` (`version`)
- Modify: манифесты, которые перепишет `scripts/update_codex_plugin_manifest.py`

**Interfaces:**
- Consumes: финальное состояние `plugin/` и версию пакета.
- Produces: `version = "0.4.0"` (минорный бамп — расширение возможностей без ломающих изменений) и
  синхронные payload-digest'ы в манифестах.

- [ ] **Step 1: Поднять версию**

```bash
.venv/bin/python - <<'PY'
import pathlib, re
p = pathlib.Path("pyproject.toml")
text = p.read_text(encoding="utf-8")
p.write_text(re.sub(r'^version = "0\.3\.7"', 'version = "0.4.0"', text, count=1, flags=re.M), encoding="utf-8")
PY
grep -n '^version' pyproject.toml
```
Expected: `version = "0.4.0"`.

- [ ] **Step 2: Пересобрать манифесты**

Run: `.venv/bin/python scripts/update_codex_plugin_manifest.py`
Expected: скрипт завершается без ошибок; `git status` показывает изменённые манифесты.

- [ ] **Step 3: Прогнать install-тесты (они гейтят digest)**

Run: `.venv/bin/pytest tests/install -q && .venv/bin/pytest tests/test_ci_gates.py -q`
Expected: PASS.

- [ ] **Step 4: Коммит**

```bash
git add pyproject.toml plugin
git commit -m "chore(release): 0.4.0 — восемь новых провайдеров досок"
```

---

### Task 9: Финальная верификация эпика по критериям приёмки

**Files:** только чтение и прогоны.

- [ ] **Step 1: Критерии 1 и 2 — реестр и contract-suite**

Run: `.venv/bin/pytest tests/tasks/boards -q`
Expected: PASS; в выводе видно 11 параметризаций contract-тестов.

Run:
```bash
.venv/bin/python -c "
from reviewer.tasks.boards.registry import default_board_registry
print(len(default_board_registry().registered_types()), default_board_registry().registered_types())
"
```
Expected: `11 (...)` — все 11 типов.

- [ ] **Step 2: Критерий 5 — project-scoped валидация каждого типа**

Run: `.venv/bin/reviewer check --help | head -20`
Expected: флаг `--board-project` присутствует и повторяем. Полная проверка с реальными кредами
недоступна (их нет в окружении) — зафиксировать это ограничение в отчёте, а покрытие обеспечить
тестом `tests/entrypoints/test_check_boards.py` на новых типах:

Run: `.venv/bin/pytest tests/entrypoints/test_check_boards.py -q`
Expected: PASS.

- [ ] **Step 3: Критерии 6 и 7 — отсутствие утечек и веток по типу**

Run:
```bash
grep -rn "github\|trello\|linear\|clickup\|asana\|yandex_tracker\|kaiten\|weeek" \
  reviewer/config/settings.py reviewer/config/provider_credentials.py reviewer/config/task_board.py \
  reviewer/tasks/sync.py reviewer/mcp/service.py reviewer/install.py reviewer/entrypoints/cli.py
```
Expected: пустой вывод (ни одной ветки по типу доски вне реестра).

Run: `.venv/bin/pytest tests/tasks/boards/test_registry.py -q -k secret`
Expected: PASS — guard `_contains_secret` покрыт на каждом новом spec.

- [ ] **Step 4: Критерий 9 — линт и полный прогон**

Run: `.venv/bin/ruff check reviewer/tasks/boards tests/tasks/boards && .venv/bin/pytest -q`
Expected: ruff без замечаний по новым файлам; полный unit-прогон зелёный.

- [ ] **Step 5: Критерий 3/4 — по строке матрицы и markdown-инварианту**

Run: `.venv/bin/pytest tests/docs -q && .venv/bin/pytest tests/tasks/boards -q -k "normalize"`
Expected: PASS — каждая доска документирована, `normalize` отдаёт markdown у всех адаптеров
(contract-тест `test_normalize_preserves_markdown_links_subtasks_and_attachments`).

- [ ] **Step 6: Итоговый коммит статуса (если остались правки) и отчёт**

```bash
git status --short
git log --oneline dev..HEAD
```
Собрать отчёт: какие критерии приёмки закрыты автоматически (тестами), какие требуют живой
приёмки с реальными кредами (5 — project-scoped валидация каждой доски, 3 — фактические
sync/create/finish на живых API).
