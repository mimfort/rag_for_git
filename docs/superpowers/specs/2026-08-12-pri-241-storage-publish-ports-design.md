# PRI-241 — Параметризация хостовых портов Postgres/Neo4j в compose и wizard

Бриф: `docs/superpowers/briefs/2026-08-12-PRI-241-parametrize-storage-publish-ports.md`

## Проблема

Хостовые порты storage-сервисов зашиты в `docker-compose.yml` литералами
(`docker-compose.yml:9`, `docker-compose.yml:15`), а install-wizard их не знает: он собирает
только клиентские строки подключения `PG_DSN`/`NEO4J_URI` (`reviewer/install.py:252-270`) и пишет
их в `.env`. Смена порта в мастере тихо ломает подключение — клиент идёт на новый порт, а
контейнер продолжает публиковать старый.

Сервис `web` уже параметризован по нужному образцу (`docker-compose.yml:26-30`,
`"127.0.0.1:${REVIEWER_WEB_PUBLISH_PORT:-8000}:${REVIEWER_WEB_PORT:-8000}"`, прецедент PRI-222 /
PR #162), но его переменные задаются вручную в shell — wizard их не пишет. PRI-241 впервые
заводит publish-порт под управление мастера.

Дополнительной проводки переменных не требуется: `.env` (`reviewer/install.py:437-440`) и
`docker-compose.yml` (`reviewer/update_lifecycle.py:96-99,190`) лежат в одном каталоге
`$XDG_CONFIG_HOME/rag-reviewer`, поэтому compose подхватывает то, что записал wizard.

## Архитектура

Три новые переменные окружения владеют хостовой стороной port mapping; контейнерные порты
(5432 / 7687 / 7474) и loopback-биндинг остаются фиксированными — они не предмет настройки.

| Переменная | Дефолт | Источник дефолта в wizard |
|---|---|---|
| `PARADEDB_PUBLISH_PORT` | `5433` | порт из `PG_DSN` |
| `NEO4J_BOLT_PUBLISH_PORT` | `7687` | порт из `NEO4J_URI` |
| `NEO4J_HTTP_PUBLISH_PORT` | `7474` | статичный (в `NEO4J_URI` только bolt-порт) |

### 1. `docker-compose.yml`

```yaml
paradedb:
  ports: ["127.0.0.1:${PARADEDB_PUBLISH_PORT:-5433}:5432"]
neo4j:
  ports: ["127.0.0.1:${NEO4J_HTTP_PUBLISH_PORT:-7474}:7474",
          "127.0.0.1:${NEO4J_BOLT_PUBLISH_PORT:-7687}:7687"]
```

`${VAR:-default}` даёт обратную совместимость: без переменных `docker compose config`
разворачивается в прежние `127.0.0.1:5433:5432`, `127.0.0.1:7474:7474`, `127.0.0.1:7687:7687`.
Сервисы `paradedb-test` / `neo4j-test` (`docker-compose.yml:49,63`) не изменяются: их порты
изолированы намеренно и не должны совпадать с dev.

### 2. `reviewer/install.py`

**`EnvField.derive_default`** — новое опциональное поле dataclass
(`reviewer/install.py:105-124`):

```python
derive_default: Callable[[dict[str, str]], str] | None = None
```

`prompt_groups` (`reviewer/install.py:385-434`) получает общий хелпер

```python
def _effective_default(field, values, current) -> str:
    cur = current.get(field.key, "")
    if cur:
        return cur
    if field.derive_default is not None:
        return field.derive_default(values)
    return field.default
```

Хелпер применяется во **всех трёх** местах, где сейчас вычисляется `cur or field.default`:
в основном цикле опроса и в двух ранних ветках опциональной группы (`yes=True` и отказ от
`confirm`). Группа «Хранилища» именно опциональная, поэтому без правки коротких веток
`reviewer init --yes` молча игнорировал бы производный дефолт.

Приоритет существующего значения из `.env` (`cur`) сохраняется без изменений. Порядок полей
внутри группы гарантирует, что `PG_DSN`/`NEO4J_URI` уже лежат в `values`, когда очередь доходит
до производного поля. Один и тот же путь работает в интерактивном режиме и в `--yes`/CI —
отдельной ветки не появляется.

**`_port_from_url(value: str, fallback: str) -> str`** — чистая функция на уже импортированном
`urlsplit` (`reviewer/install.py:23`). Возвращает `fallback`, если URL не разбирается
(`ValueError`), порт отсутствует или значение пустое. Fail-soft: мастер не должен падать на
кривом DSN, который пользователь как раз пришёл исправлять.

**`publish_port_warnings(values: dict[str, str]) -> list[str]`** — чистая сверка, возвращающая
готовые тексты предупреждений (без I/O и без `click`). Правила:

- Сверяются только две пары: `PG_DSN` ↔ `PARADEDB_PUBLISH_PORT` и
  `NEO4J_URI` ↔ `NEO4J_BOLT_PUBLISH_PORT`.
- `NEO4J_HTTP_PUBLISH_PORT` в сверку не входит — у него нет источника вывода.
- **Сверка выполняется только когда хост в DSN/URI локальный** (`localhost`, `127.0.0.1`, `::1`).
  Внешний Postgres/Neo4j означает, что publish-порт compose-контейнера к этому подключению
  отношения не имеет, и предупреждение было бы ложным шумом.
- Неразбираемый URL → предупреждения нет (сверять нечего).

### 3. Группа wizard «Хранилища (Postgres / Neo4j)»

Три новых `EnvField` добавляются в группу (`reviewer/install.py:252-270`) после `PG_DSN` и
`NEO4J_URI`. Группа остаётся `optional=True`, то есть в интерактивном мастере предваряется
вопросом «Настроить Хранилища (Postgres / Neo4j)?» с дефолтом «нет» — прежний путь онбординга
не удлиняется для тех, кому дефолты подходят.

`render_env` (`reviewer/install.py:313-334`) печатает новые ключи автоматически, поскольку они
поля группы; заголовок секции (`_GROUP_HEADERS`, `reviewer/install.py:296-298`) не меняется.

`ENV_TEMPLATE` (`_ENV_TEMPLATE_BASE`, секция
`# --- Postgres (ParadeDB :5433) / Neo4j (:7687) — дефолты docker-compose ---`) и `.env.example`
(строки 52-60) получают те же три ключа с теми же дефолтами. Существующий тест
`test_env_template_contains_all_wizard_keys` (`tests/install/test_install_wizard.py:187-191`)
удерживает этот паритет.

### 4. `reviewer/entrypoints/cli.py::init`

После сбора `values` (обе ветки — интерактивная `prompt_groups(...)` и `--yes`, см.
`reviewer/entrypoints/cli.py:1356` и `:1419`) и перед формированием `_GlobalInitPlan` команда
печатает каждое предупреждение из `inst.publish_port_warnings(values)` строкой вида
`⚠ <текст>`. Поведение не блокирующее: код возврата и содержимое `.env` не меняются.

Расположение сверки в конце покрывает три случая одной проверкой: интерактивный ввод, `--yes`/CI
и унаследованный `.env`, в котором значения разъехались до этой задачи.

## Поток данных

```
reviewer init → .env ($XDG_CONFIG_HOME/rag-reviewer/.env)
                  ↓ (общий каталог, тот же compose project)
        docker-compose.yml ${PARADEDB_PUBLISH_PORT:-5433} → публикуемый порт
                  ↓
        PG_DSN / NEO4J_URI из того же .env → reviewer check / index / MCP
```

Новой проводки не появляется: единственная связь — общий каталог `.env` и `docker-compose.yml`,
который compose читает сам.

## Обработка ошибок

| Ситуация | Поведение |
|---|---|
| `PG_DSN`/`NEO4J_URI` не разбирается или без порта | `_port_from_url` возвращает статичный fallback; мастер продолжает |
| Publish-порт разошёлся с портом локального DSN | предупреждение в `init`, запись `.env` выполняется |
| DSN указывает на внешний хост | предупреждения нет |
| Пользователь пропустил группу «Хранилища» | значения = существующие из `.env` или дефолты (прежнее поведение `prompt_groups`) |

## Тестирование

**`tests/test_infrastructure_policy.py`** (паттерн
`test_compose_web_service_is_opt_in_with_separate_runtime_ports`, `:350`): `yaml.safe_load`
`docker-compose.yml`, сверка литералов `ports` у `paradedb` и `neo4j` с
`${VAR:-default}`-шаблонами. Отдельным ассертом фиксируется, что порты `paradedb-test` /
`neo4j-test` остались прежними литералами (`55433`, `17474`/`17687`) — критерий 5. Без Docker и
сети, идёт в обычном `pytest`.

Живой `docker compose config` (буквальная формулировка критериев 1 и 2) остаётся ручной
приёмкой: юнит-проверка шаблона вместе с `:-default` подстановкой доказывает обратную
совместимость, а поднимать Docker в unit-прогоне запрещено конвенцией репозитория.

**`tests/install/test_install_wizard.py`** (паттерны `:62-75`, `:346-401`):

1. `render_env` со значениями по умолчанию содержит все три новых ключа.
2. `derive_default` выводит порт из нестандартного `PG_DSN` (`...@localhost:6543/reviewer` → `6543`)
   и из нестандартного `NEO4J_URI` (`neo4j://localhost:7999` → `7999`).
3. Невалидный/беспортовый DSN → статичный fallback (`5433` / `7687`).
4. Существующее значение в `.env` перебивает производный дефолт.
5. `publish_port_warnings` возвращает предупреждение при расхождении на локальном хосте, пустой
   список — при совпадении, при внешнем хосте и при неразбираемом URL.

## Документация

README.md и README.ru.md обновляются синхронно, абзацем рядом с существующим примером
`REVIEWER_WEB_PORT` / `REVIEWER_WEB_PUBLISH_PORT` (`README.md:797`, `README.ru.md:786`):

- публикуемые порты storage-сервисов настраиваются переменными `PARADEDB_PUBLISH_PORT`,
  `NEO4J_BOLT_PUBLISH_PORT`, `NEO4J_HTTP_PUBLISH_PORT`, и `reviewer init` записывает их в `.env`
  согласованно с `PG_DSN`/`NEO4J_URI`;
- вручную отредактированный `docker-compose.yml` апдейтер не перезаписывает: сравнение SHA256
  всего файла переводит его в статус `preserved` (`reviewer/update_lifecycle.py:189-220`), и
  обновления шаблона из репозитория перестают применяться. Это аргумент за настройку
  переменными вместо правки файла руками.

## Вне скоупа

- Новые переменные не добавляются в `Settings` (`reviewer/config/settings.py`): их читает только
  compose, не Python-код.
- `reviewer check` не расширяется сверкой портов — диагностика живёт в `init`.
- Поведение `preserved` у апдейтера не меняется, только документируется.
- Тестовый профиль compose не трогается.

## Критерии приёмки

1. `docker compose config` без переменных даёт прежние `127.0.0.1:5433:5432`,
   `127.0.0.1:7687:7687`, `127.0.0.1:7474:7474`.
2. С заданными `PARADEDB_PUBLISH_PORT` / `NEO4J_BOLT_PUBLISH_PORT` compose публикует указанные
   порты, и `reviewer check` проходит против них.
3. После прогона wizard с нестандартным портом `.env` содержит согласованные publish-порт и
   DSN/URI, стек поднимается без ручной правки compose.
4. Unit-тесты в `tests/install/test_install_wizard.py` фиксируют новые поля, вывод дефолта из
   DSN/URI и поведение сверки.
5. Тестовый профиль в compose не изменён; порты test-сервисов по-прежнему отличаются от dev.
6. README.md и README.ru.md обновлены синхронно.
