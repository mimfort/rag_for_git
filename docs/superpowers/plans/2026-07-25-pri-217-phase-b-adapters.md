# PRI-217 Фаза B — восемь адаптеров досок

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать восемь полных `TaskBoardProvider`-адаптеров (`github`, `trello`, `linear`,
`clickup`, `asana`, `yandex_tracker`, `kaiten`, `weeek`) поверх общего транспорта фазы A, каждый — со
своим фейком для contract-suite и provider-специфичными тестами.

**Architecture:** Каждый адаптер — один файл `reviewer/tasks/boards/<type>.py`: класс
`<Name>Board(RestBoardBase)` (для Linear — поверх `GraphQLClient`) плюс модульная функция
`provider_spec() -> BoardProviderSpec`. Ни один адаптер не заводит собственную httpx-обвязку, не
правит общие файлы (`registry.py`, `contract.py`, документацию) и не знает о других адаптерах —
подключение делает фаза C.

**Tech Stack:** Python 3.11–3.13, httpx (`MockTransport` в тестах), pytest, ruff.

## Global Constraints

- Язык проекта — русский: комментарии и докстринги по-русски, идентификаторы латиницей, line-length 100.
- Источник истины по эндпоинтам, аутентификации, пагинации, полям и лимитам —
  `docs/board-apis-research.md`. Если в нём для нужного пункта стоит «НЕ НАЙДЕНО», агент обязан сам
  найти официальную документацию (WebSearch/WebFetch) или исходники официального SDK и
  **не выдумывать** эндпоинт. Найденное идёт в отчёт агента, а **не** в общий research-файл —
  восемь агентов работают параллельно, общие файлы правит только фаза C.
- `ProviderAdapter` и `FakeState` импортируются из `tests.tasks.boards.fakes.base`
  (`contract.py` их только реэкспортирует).
- Агенты фазы B **не делают коммитов** и не запускают `pytest tests/tasks/boards` целиком (там лежат
  недописанные файлы соседних агентов) — только свои тесты. Коммитит и прогоняет всё оркестратор.
- Адаптер строится на `RestBoardBase` (`reviewer/tasks/boards/restbase.py`), генераторах
  `pagination.py` и — для GraphQL — `graphql.py`. Копировать httpx-обвязку из `jira.py`/`yougile.py`/
  `youtrack.py` запрещено.
- Транспорт инжектируется: конструктор принимает `transport: httpx.BaseTransport | None` и
  `sleeper: Callable[[float], None]`. Подмена `provider._client` постфактум запрещена.
- Unit-тесты не открывают сокетов (`tests/infrastructure_policy.py`) — только `httpx.MockTransport`.
- Секреты: каждый секретный credential объявляется `CredentialFieldSpec(secret=True)`; значения
  передаются в `RestBoardBase(secrets=...)`, чтобы `BoardProviderError` их вымарывал. Секрет не
  попадает в `validate_connection`, `list_targets`, текст ошибки и логи.
- Никакого OAuth loopback и локальных redirect-серверов: только ввод токена и `help_url` /
  `help_url_builder` (кроссплатформенно, работает в headless CLI).
- Кроссплатформенность: чистый Python + httpx; ни `subprocess`, ни shell, ни POSIX-путей, ни мутаций
  `os.environ`; парсинг времени — только UTC-aware в epoch ms.
- Файлы, которые адаптер НЕ трогает: `reviewer/tasks/boards/registry.py`,
  `tests/tasks/boards/contract.py`, `docs/board-providers.md`, `README*.md`, `.env.example`,
  `pyproject.toml`. Всё это — фаза C.
- Коммиты: Conventional Commits на русском, без self-attribution. Один коммит на адаптер (адаптер +
  фейк + тесты вместе — это неделимая полная регистрация типа).

---

## Обязательный контракт каждого адаптера

Ниже — точные требования, выведенные из `TaskBoardProvider` (`reviewer/tasks/boards/base.py`) и из 13
тестов `ProviderContract` (`tests/tasks/boards/contract.py`). Адаптер считается готовым, только когда
выполнены все пункты.

### 1. `provider_spec() -> BoardProviderSpec`

```python
def provider_spec() -> BoardProviderSpec:
    return BoardProviderSpec(
        board_type="<type>",
        factory=_build_provider,
        credential_fields=(
            CredentialFieldSpec(env="<PREFIX>_TOKEN", label="<Human> API token", secret=True),
            CredentialFieldSpec(env="<PREFIX>_API_BASE", label="<Human> API base URL",
                                secret=False, required=False, default=""),
        ),
        setup=ProviderSetupSpec(
            label="<Human name>",
            help_url="<официальная страница выпуска токена>",
            help_text="<как получить токен, по-русски, 1-2 предложения>",
        ),
        option_fields=(
            ProviderOptionSpec(key="<option>", label="<Human label>",
                               required_for=("sync", "create", "finish")),
        ),
        default_api_base="<дефолтный base URL>",
        create_target_label="<как называется цель создания у этой доски>",
        done_target_label="<как называется цель закрытия у этой доски>",
    )
```

- `_build_provider(context: ProviderBuildContext) -> <Name>Board` читает `context.credentials`,
  `context.options`, `context.key_pattern`, `context.url_template`, attachment-лимиты.
- `spec` — frozen dataclass; никаких мутаций после создания.
- Секрет никогда не объявляется как `ProviderOptionSpec` (реестр это запрещает и проверяет).

### 2. Девять методов провайдера

| Метод | Требование |
|---|---|
| `validate_connection(project)` | Возвращает ровно `{"status","identity","project","capabilities","warnings"}`; `status == "ok"` при рабочих кредах; секрет не встречается в `repr()` результата. При 403 бросает `BoardProviderError(category="permission")`, при 404 — `"not_found"`. |
| `iter_raw(board, limit)` | Ленивый обход **всех** страниц через `pagination.py`; `RawTask.timestamp` — epoch ms последнего изменения (> 0); `limit` ограничивает выдачу. |
| `normalize(raw)` | `{key, aliases, title, description, criteria, status, url, links, attachments, project}`; `description` — **строго markdown** (никаких `<p>`); `links` содержит запись `{"type": "subtask", ...}` для подзадач/чеклистов; `attachments` — метаданные вложений. |
| `normalize_meta(raw)` | **Ноль HTTP-вызовов**: только плоские поля из `RawTask`; `criteria == []`, `attachments == []`. |
| `list_targets(project)` | Возвращает ровно `{"targets","options","warnings"}`; каждый target — `{"id","label","purposes"}`; у всех targets в `purposes` есть `"create"`. |
| `create(doc_md, *, title, target, project)` | Возвращает `{key, url, board_id, target_resolved, warnings}`; при найденном target `target_resolved` равен его id или label, `warnings == []`; при ненайденном — создаёт в дефолтном месте, `target_resolved != target`, `warnings` непусто (не бросает). |
| `finish(key, pr_url, *, note, mark_done, target)` | Идемпотентно: первый вызов → `pr_link_added=True`, `done_set=True`; повторный с теми же аргументами → `pr_link_added=False`, `done_set=False`, `already_closed=True`. Любая правка двигает last-modified доски. |
| `fetch_one(key)` | `dataclasses.asdict(fetch_one(key)) == dataclasses.asdict(<та же задача из iter_raw>)` — один и тот же маппер; сетевой сбой/404 → `None`. |
| `close()` | Закрывает транспорт и после успеха, и после ошибки. |

### 3. Канонический ключ задачи

- Нативные ключи (`linear`: `ENG-123`, `yandex_tracker`: `QUEUE-123`) используются как `RawTask.key`.
- Остальные доски синтезируют ключ: `key = f"{key_prefix}-{number}"`, где `key_prefix` —
  `ProviderOptionSpec(key="key_prefix", required_for=("sync",))`, а `number` — стабильный числовой
  идентификатор доски. Нативный id всегда попадает в `RawTask.board_id` и в `aliases` нормализованной
  задачи, чтобы write-операции не зависели от синтезированного ключа.
- `RawTask.project_code` заполняется тем же ключом (или нативным project-кодом, если он есть), чтобы
  `project_prefix()` находил проектный префикс.

### 4. Фейк для contract-suite: `tests/tasks/boards/fakes/<type>.py`

Модуль экспортирует `SECRET`, `State(FakeState)`, `build(...)` и `ADAPTER: ProviderAdapter`
(структура — см. `tests/tasks/boards/fakes/jira.py` после фазы A). Требования к фейку:

- Отдаёт **больше одной страницы** задач: `min_rows` в `ADAPTER` ставится так, чтобы contract-тест
  `len(rows) > adapter.min_rows` требовал минимум двух страничных запросов
  (`page_paths` — пути/эндпоинты страничных вызовов, для GraphQL — `("/graphql",)`).
- Первая задача имеет подзадачу/чеклист и вложение с именем **ровно** `spec.txt` — этого требует
  contract-тест нормализации.
- Поддерживает `error_status`/`forbidden`: при заданном статусе любой запрос отвечает этим кодом и
  телом, содержащим значение секрета (`{"token": SECRET}`) — тест проверяет, что секрет не утекает.
- Хранит состояние в памяти (`State`), чтобы `finish` был по-настоящему идемпотентным: второй вызов
  видит уже дописанную PR-ссылку и уже закрытый статус.
- `RecordingTransport` из `tests/tasks/boards/fakes/base.py` — обязателен (через него проверяется `close`).

### 5. Provider-специфичные тесты

По образцу `test_jira_*.py` — минимум пять файлов на адаптер:

- `tests/tasks/boards/test_<type>_read.py` — пагинация (точные параметры запросов), маппинг
  `RawTask`, `limit`, парсинг времени в epoch ms.
- `tests/tasks/boards/test_<type>_normalize.py` — markdown-инвариант (для не-markdown транспорта —
  фактическая конвертация), подзадачи → `criteria`/`links`, вложения, `normalize_meta` без I/O.
- `tests/tasks/boards/test_<type>_targets.py` — `list_targets` (нормализованная форма, `purposes`),
  резолв target по id и по label, отсутствующий target.
- `tests/tasks/boards/test_<type>_create.py` — создание с target, fallback с warning, обязательные поля.
- `tests/tasks/boards/test_<type>_finish.py` — идемпотентность, дописывание PR-ссылки, установка
  done-цели, `already_closed`.
- `tests/tasks/boards/test_<type>_errors.py` (если у доски есть особая семантика ошибок: GraphQL-ошибки
  Linear, 403 rate-limit GitHub, `X-Org-ID` Yandex Tracker) — категоризация в `BoardProviderError`.

---

## Шаблон задачи (применяется к каждому из восьми адаптеров)

### Task <N>: адаптер `<type>`

**Files:**
- Create: `reviewer/tasks/boards/<type>.py`
- Create: `tests/tasks/boards/fakes/<type>.py`
- Create: `tests/tasks/boards/test_<type>_read.py`, `test_<type>_normalize.py`,
  `test_<type>_targets.py`, `test_<type>_create.py`, `test_<type>_finish.py`
- Не трогать: `registry.py`, `contract.py`, `docs/board-providers.md`, `README*.md`, `.env.example`

**Interfaces:**
- Consumes: `RestBoardBase` / `GraphQLClient`, `pagination.paginate_*`, `RawTask`,
  `BoardProviderError`, `CredentialFieldSpec`/`ProviderOptionSpec`/`ProviderSetupSpec`/
  `BoardProviderSpec`/`ProviderBuildContext`, `FakeState`/`RecordingTransport`/`record`/`request_json`,
  `ProviderAdapter`.
- Produces: `provider_spec()`, класс `<Name>Board` с `board_type = "<type>"`, `ADAPTER` в фейке.

- [ ] **Step 1: Свериться с фактами API**

Прочитать раздел своей доски в `docs/board-apis-research.md`: base URL, аутентификация, эндпоинт
листинга с фильтром по времени изменения, модель пагинации, поле описания и его формат, подзадачи/
чеклисты, вложения, единичное чтение, discovery статусов, создание, лимиты. Пробел («НЕ НАЙДЕНО») —
найти официальную доку самому и дописать в файл.

- [ ] **Step 2: Написать падающий тест чтения (`test_<type>_read.py`)**

Тест поднимает адаптер на `httpx.MockTransport`, отдаёт две страницы задач и проверяет: число строк,
точные параметры страничных запросов (по факту из Step 1), `RawTask.key`/`project_code`/`board_id`/
`timestamp`, работу `limit`.

- [ ] **Step 3: Прогнать — убедиться, что падает по причине «нет модуля/класса»**

Run: `.venv/bin/pytest tests/tasks/boards/test_<type>_read.py -q`

- [ ] **Step 4: Реализовать чтение (`iter_raw`, `fetch_one`, `_raw_from_*`) минимально**

- [ ] **Step 5: Прогнать — зелено**

- [ ] **Step 6: Повторить цикл тест→реализация для нормализации, targets, create, finish**

Каждый цикл: сначала тест, прогон с падением, минимальная реализация, зелёный прогон. Порядок:
`normalize`/`normalize_meta` → `list_targets` → `create` → `finish` → `validate_connection`.

- [ ] **Step 7: Написать фейк и подключить его к contract-suite локально**

Создать `tests/tasks/boards/fakes/<type>.py` (требования — раздел 4 выше). Проверить свой адаптер
общим набором, **не меняя** `contract.py`, — через локальный тест-файл:

```python
# tests/tasks/boards/test_<type>_contract.py
"""Общий contract-набор на фейке <type> (постоянная параметризация — в фазе C)."""
import pytest

from tests.tasks.boards.contract import ProviderContract
from tests.tasks.boards.fakes import <type> as fake


@pytest.mark.parametrize("adapter", [fake.ADAPTER], indirect=True)
class Test<Name>Contract(ProviderContract):
    pass
```

- [ ] **Step 8: Прогнать полный contract-набор на своём адаптере**

Run: `.venv/bin/pytest tests/tasks/boards/test_<type>_contract.py -q`
Expected: все 13 тестов `ProviderContract` — PASS.

- [ ] **Step 9: Линт и полный прогон своих тестов**

Run: `.venv/bin/ruff check reviewer/tasks/boards/<type>.py tests/tasks/boards/fakes/<type>.py tests/tasks/boards/test_<type>_*.py && .venv/bin/pytest tests/tasks/boards -q`
Expected: ruff чист по своим файлам; `tests/tasks/boards` зелёный (свои тесты добавились, чужие не сломались).

- [ ] **Step 10: Коммит**

```bash
git add reviewer/tasks/boards/<type>.py tests/tasks/boards/fakes/<type>.py tests/tasks/boards/test_<type>_*.py
git commit -m "feat(boards): адаптер <Human name>"
```

- [ ] **Step 11: Сдать отчёт для фазы C**

В отчёте обязательно: точные `credential_fields` (env, secret, required, default), `option_fields`
(key, required_for), `default_api_base`, `create_target_label`/`done_target_label`, фактический формат
описания для колонки матрицы (`Native MD` / `HTML↔MD` / `YFM→MD`), ссылка на официальную страницу
токена для секции документации, что играет роль done-цели, и любые ограничения (например: вложения
требуют отдельной авторизации → отдаются метаданными без текста).

---

## Спецификации по доскам

Заполняются из `docs/board-apis-research.md` (см. Global Constraints). Порядок реализации —
по приоритету задачи: `github` (эталон) → `trello` → `linear` → `clickup` → `asana` →
`yandex_tracker` → `kaiten` → `weeek`.

Ключевые заранее принятые решения по доскам (дизайн, `docs/superpowers/specs/2026-07-25-pri-217-board-providers-design.md`):

| Тип | Транспорт | Ключ задачи | Формат описания | Роль done-цели |
|---|---|---|---|---|
| `github` | REST, `paginate_link_header` | `key_prefix` + issue number | Native MD | `state=closed`; labels/milestone как targets |
| `trello` | REST v1, key+token в query (`RestBoardBase(params=...)`) | `key_prefix` + `idShort` | Native MD (`desc`) | список (list) доски |
| `linear` | GraphQL (`GraphQLClient.paginate`) | нативный (`ENG-123`) | Native MD | workflow state |
| `clickup` | REST v2, `paginate_page` | `custom_id`, иначе `key_prefix` + id | Native MD | status списка |
| `asana` | REST, `paginate_cursor` | `key_prefix` + gid | HTML↔MD (`html_notes`, `markup.py`) | `completed=true` / секция |
| `yandex_tracker` | REST, `X-Org-ID`/`X-Cloud-Org-ID` | нативный (`QUEUE-123`) | YFM→MD (`yfm.py`) | переход/статус очереди |
| `kaiten` | REST, self-hosted base URL как option | `key_prefix` + номер карточки | по факту research | колонка доски |
| `weeek` | REST, base URL как option | `key_prefix` + номер задачи | по факту research | колонка/статус |

Каждый агент фазы B обязан сверить свою строку с research-файлом и, если факт расходится, следовать
research (официальной доке), а расхождение указать в отчёте.
