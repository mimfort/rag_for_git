# PRI-241 — Параметризация publish-портов Postgres/Neo4j: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Публикуемые хостовые порты `paradedb` и `neo4j` задаются переменными окружения, которые
записывает `reviewer init`, а не литералами в `docker-compose.yml`.

**Architecture:** Три новые переменные (`PARADEDB_PUBLISH_PORT`, `NEO4J_BOLT_PUBLISH_PORT`,
`NEO4J_HTTP_PUBLISH_PORT`) владеют хостовой стороной port mapping по образцу уже
параметризованного сервиса `web`. `EnvField` получает опциональный callable `derive_default`,
чтобы дефолт publish-порта выводился из уже введённого `PG_DSN`/`NEO4J_URI`; чистая функция
`publish_port_warnings` в конце `reviewer init` предупреждает о расхождении, не блокируя запись.

**Tech Stack:** Python 3, Click, dataclasses, `urllib.parse.urlsplit`, PyYAML (в тестах), pytest,
Docker Compose.

Спека: `docs/superpowers/specs/2026-08-12-pri-241-storage-publish-ports-design.md`
Бриф: `docs/superpowers/briefs/2026-08-12-PRI-241-parametrize-storage-publish-ports.md`

## Global Constraints

- Ветка работы — `feat/pri-241-storage-publish-ports` (спека и бриф уже в ней, коммит `483fa76`).
- Язык проекта русский: комментарии, докстринги, сообщения CLI — на русском.
- Коммиты — Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`,
  упоминаний Claude/AI).
- Дефолты неизменны и обязаны совпадать везде (compose, `ENV_TEMPLATE`, `.env.example`, поля
  wizard): `PARADEDB_PUBLISH_PORT=5433`, `NEO4J_BOLT_PUBLISH_PORT=7687`,
  `NEO4J_HTTP_PUBLISH_PORT=7474`.
- Контейнерные порты (`5432`, `7687`, `7474`) и loopback-биндинг `127.0.0.1` не параметризуются.
- Сервисы `paradedb-test` / `neo4j-test` не изменяются ни одной задачей.
- Новые переменные **не** добавляются в `reviewer/config/settings.py` — их читает только compose.
- `reviewer check` не расширяется; поведение `preserved` у апдейтера не меняется.
- Каталог `plugin/` не трогается, версия в `pyproject.toml` не бампается → пересборка манифестов
  (`update_codex_plugin_manifest.py`) не требуется.
- Тесты запускаются интерпретатором venv: `.venv/bin/pytest`. Все тесты плана — unit (без
  Postgres, Neo4j, Docker и сети), маркер `integration` не ставится.
- Перед коммитом прогонять `.venv/bin/ruff check <изменённые .py>` (pre-commit hook делает то же
  по staged-файлам). Repo-wide чистоты ruff не добиваться — на `dev` он не чист.

---

### Task 1: Параметризовать публикуемые порты storage-сервисов в compose

**Files:**
- Modify: `docker-compose.yml:9`, `docker-compose.yml:15`
- Test: `tests/test_infrastructure_policy.py` (новый тест рядом с
  `test_compose_web_service_is_opt_in_with_separate_runtime_ports`, строка 350)

**Interfaces:**
- Consumes: ничего.
- Produces: переменные окружения `PARADEDB_PUBLISH_PORT`, `NEO4J_BOLT_PUBLISH_PORT`,
  `NEO4J_HTTP_PUBLISH_PORT` с дефолтами `5433`/`7687`/`7474` — их именами пользуются задачи 2-5.

- [ ] **Step 1: Написать падающий тест**

Добавить в `tests/test_infrastructure_policy.py` сразу после
`test_compose_web_service_is_opt_in_with_separate_runtime_ports` (файл уже импортирует `yaml` и
`Path`, новых импортов не нужно):

```python
def test_compose_publishes_storage_ports_through_overridable_variables() -> None:
    """Хостовые порты dev-хранилищ параметризованы, контейнерные — фиксированы."""
    root = Path(__file__).parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))

    assert compose["services"]["paradedb"]["ports"] == [
        "127.0.0.1:${PARADEDB_PUBLISH_PORT:-5433}:5432"
    ]
    assert compose["services"]["neo4j"]["ports"] == [
        "127.0.0.1:${NEO4J_HTTP_PUBLISH_PORT:-7474}:7474",
        "127.0.0.1:${NEO4J_BOLT_PUBLISH_PORT:-7687}:7687",
    ]
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/test_infrastructure_policy.py::test_compose_publishes_storage_ports_through_overridable_variables -q`
Expected: FAIL — фактические значения `['127.0.0.1:5433:5432']` и
`['127.0.0.1:7474:7474', '127.0.0.1:7687:7687']` не равны ожидаемым шаблонам.

- [ ] **Step 3: Внести правку в compose**

В `docker-compose.yml` заменить строку 9:

```yaml
    ports: ["127.0.0.1:${PARADEDB_PUBLISH_PORT:-5433}:5432"]   # 5433 на хосте: 5432 занят локальным Postgres разработчика
```

и строку 15:

```yaml
    ports: ["127.0.0.1:${NEO4J_HTTP_PUBLISH_PORT:-7474}:7474", "127.0.0.1:${NEO4J_BOLT_PUBLISH_PORT:-7687}:7687"]
```

Комментарий над `paradedb.ports` (loopback-only, строки 7-8) сохранить как есть. Блок
`paradedb-test`/`neo4j-test` не трогать.

- [ ] **Step 4: Запустить тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest tests/test_infrastructure_policy.py -q -k "compose"`
Expected: PASS, включая существующий `test_compose_defines_isolated_test_profile_services` —
он фиксирует неизменность портов test-профиля (критерий приёмки 5).

- [ ] **Step 5: Проверить обратную совместимость вручную**

Run: `docker compose config | grep -A2 "published"`
Expected: без заданных переменных публикуются `5433`, `7474`, `7687` — прежние значения.
Если Docker недоступен, шаг пропустить и отметить это в отчёте по задаче.

- [ ] **Step 6: Коммит**

```bash
git add docker-compose.yml tests/test_infrastructure_policy.py
git commit -m "feat(compose): публикуемые порты paradedb/neo4j через переменные окружения"
```

---

### Task 2: Производный дефолт поля wizard (`derive_default` + `_port_from_url`)

**Files:**
- Modify: `reviewer/install.py:119-124` (dataclass `EnvField`), `reviewer/install.py:385-434`
  (`prompt_groups`), импорты в шапке файла
- Test: `tests/install/test_install_wizard.py`

**Interfaces:**
- Consumes: имена переменных из задачи 1.
- Produces:
  - `EnvField.derive_default: Callable[[dict[str, str]], str] | None = None` — новое опциональное
    поле dataclass;
  - `install._port_from_url(value: str, fallback: str) -> str` — порт из URL строкой;
  - `install._effective_default(field: EnvField, values: dict[str, str], current: dict[str, str]) -> str`
    — единая точка вычисления дефолта.
  Задача 3 использует `_port_from_url` и `derive_default`.

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/install/test_install_wizard.py` (файл уже импортирует
`from reviewer import install as inst`):

```python
def test_port_from_url_reads_explicit_port():
    dsn = "postgresql://reviewer:reviewer@localhost:6543/reviewer"
    assert inst._port_from_url(dsn, "5433") == "6543"
    assert inst._port_from_url("neo4j://localhost:7999", "7687") == "7999"


def test_port_from_url_falls_back_when_port_missing_or_broken():
    assert inst._port_from_url("postgresql://reviewer@localhost/reviewer", "5433") == "5433"
    assert inst._port_from_url("neo4j://localhost:not-a-port", "7687") == "7687"
    assert inst._port_from_url("", "5433") == "5433"
    assert inst._port_from_url("   ", "5433") == "5433"


def _derived_probe_group(optional: bool) -> "inst.EnvGroup":
    """Группа из источника и производного от него поля — для проверки derive_default."""
    return inst.EnvGroup(
        title="Проба",
        optional=optional,
        fields=[
            inst.EnvField(
                key="PROBE_URI",
                prompt_text="PROBE_URI",
                default="neo4j://localhost:7687",
            ),
            inst.EnvField(
                key="PROBE_PORT",
                prompt_text="PROBE_PORT",
                default="7687",
                derive_default=lambda values: inst._port_from_url(
                    values.get("PROBE_URI", ""), "7687"
                ),
            ),
        ],
    )


def test_prompt_groups_derives_default_from_earlier_field():
    result = inst.prompt_groups(
        [_derived_probe_group(optional=False)],
        current={"PROBE_URI": "neo4j://localhost:7999"},
        yes=True,
    )
    assert result["PROBE_PORT"] == "7999"


def test_prompt_groups_derives_default_in_optional_group():
    # Опциональная группа при yes=True идёт коротким путём — derive_default обязан работать и там
    result = inst.prompt_groups(
        [_derived_probe_group(optional=True)],
        current={"PROBE_URI": "neo4j://localhost:7999"},
        yes=True,
    )
    assert result["PROBE_PORT"] == "7999"


def test_prompt_groups_existing_value_beats_derived_default():
    result = inst.prompt_groups(
        [_derived_probe_group(optional=False)],
        current={"PROBE_URI": "neo4j://localhost:7999", "PROBE_PORT": "7000"},
        yes=True,
    )
    assert result["PROBE_PORT"] == "7000"


def test_prompt_groups_derive_default_falls_back_to_static_default():
    result = inst.prompt_groups(
        [_derived_probe_group(optional=False)],
        current={"PROBE_URI": "neo4j://localhost"},
        yes=True,
    )
    assert result["PROBE_PORT"] == "7687"
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `.venv/bin/pytest tests/install/test_install_wizard.py -q -k "port_from_url or derive"`
Expected: FAIL — `AttributeError: module 'reviewer.install' has no attribute '_port_from_url'` и
`TypeError: EnvField.__init__() got an unexpected keyword argument 'derive_default'`.

- [ ] **Step 3: Реализовать минимально**

В `reviewer/install.py` в шапке добавить импорт (рядом с существующими `from ... import`):

```python
from collections.abc import Callable
```

Расширить dataclass `EnvField` (строки 118-124):

```python
@dataclass
class EnvField:
    key: str
    prompt_text: str
    default: str = ""
    secret: bool = False
    required: bool = False
    # Дефолт, выводимый из уже собранных значений (напр. порт из PG_DSN). None — статичный default.
    derive_default: Callable[[dict[str, str]], str] | None = None
```

Добавить перед `prompt_groups` две функции:

```python
def _port_from_url(value: str, fallback: str) -> str:
    """Порт из URL (DSN/URI) строкой; fallback при пустом/кривом URL или URL без порта."""
    try:
        port = urlsplit((value or "").strip()).port
    except ValueError:
        return fallback
    return str(port) if port else fallback


def _effective_default(
    field: EnvField,
    values: dict[str, str],
    current: dict[str, str],
) -> str:
    """Дефолт поля: значение из .env → производный (derive_default) → статичный."""
    cur = current.get(field.key, "")
    if cur:
        return cur
    if field.derive_default is not None:
        return field.derive_default(values)
    return field.default
```

В `prompt_groups` заменить **все три** места, где сейчас вычисляется `current.get(...) or f.default`
/ `cur or field.default`:

```python
    for group in groups:
        # Опциональную группу предваряем вопросом (в интерактивном режиме)
        if group.optional:
            if yes:
                # CI: сохраняем текущее или дефолт, не спрашиваем
                for f in group.fields:
                    values[f.key] = _effective_default(f, values, current)
                continue
            if not click.confirm(f"\nНастроить {group.title}?", default=False):
                for f in group.fields:
                    values[f.key] = _effective_default(f, values, current)
                continue
        elif not yes:
            click.echo(f"\n[{group.title}]")

        for field in group.fields:
            cur = current.get(field.key, "")
            effective_default = _effective_default(field, values, current)
```

Остальное тело цикла (ветки `yes`, `field.secret`, финальный `click.prompt`) не меняется — оно уже
использует `cur` и `effective_default`.

- [ ] **Step 4: Запустить тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest tests/install/test_install_wizard.py -q`
Expected: PASS — новые тесты зелёные, существующие (`test_prompt_groups_yes_uses_current_values`,
`test_prompt_groups_yes_uses_field_default_when_no_current`,
`test_prompt_groups_yes_skips_optional_groups`) не сломались.

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/install.py tests/install/test_install_wizard.py
git add reviewer/install.py tests/install/test_install_wizard.py
git commit -m "feat(install): производный дефолт поля wizard (derive_default) и разбор порта из URL"
```

---

### Task 3: Три поля publish-портов в группе «Хранилища», `ENV_TEMPLATE` и `.env.example`

**Files:**
- Modify: `reviewer/install.py:252-270` (группа «Хранилища (Postgres / Neo4j)»),
  `reviewer/install.py:71-77` (секция Postgres/Neo4j в `_ENV_TEMPLATE_BASE`),
  `.env.example:51-60`
- Test: `tests/install/test_install_wizard.py`

**Interfaces:**
- Consumes: `install._port_from_url`, `EnvField.derive_default` из задачи 2; имена переменных и
  дефолты из задачи 1.
- Produces: ключи `PARADEDB_PUBLISH_PORT`, `NEO4J_BOLT_PUBLISH_PORT`, `NEO4J_HTTP_PUBLISH_PORT` в
  `WIZARD_GROUPS`, `ENV_TEMPLATE` и `.env.example` — на них опирается задача 4.

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/install/test_install_wizard.py`:

```python
PUBLISH_PORT_DEFAULTS = {
    "PARADEDB_PUBLISH_PORT": "5433",
    "NEO4J_BOLT_PUBLISH_PORT": "7687",
    "NEO4J_HTTP_PUBLISH_PORT": "7474",
}


def test_storage_group_declares_publish_ports_with_compose_defaults():
    group = next(
        g for g in inst.WIZARD_GROUPS if g.title == "Хранилища (Postgres / Neo4j)"
    )
    declared = {f.key: f.default for f in group.fields}

    for key, default in PUBLISH_PORT_DEFAULTS.items():
        assert declared[key] == default


def test_wizard_yes_derives_publish_ports_from_connection_strings():
    current = {
        "PG_DSN": "postgresql://reviewer:reviewer@localhost:6543/reviewer",
        "NEO4J_URI": "neo4j://localhost:7999",
    }
    result = inst.prompt_groups(inst.WIZARD_GROUPS, current=current, yes=True)

    assert result["PARADEDB_PUBLISH_PORT"] == "6543"
    assert result["NEO4J_BOLT_PUBLISH_PORT"] == "7999"
    # HTTP-порт неоткуда выводить: в NEO4J_URI только bolt
    assert result["NEO4J_HTTP_PUBLISH_PORT"] == "7474"


def test_wizard_yes_keeps_compose_defaults_without_current_values():
    result = inst.prompt_groups(inst.WIZARD_GROUPS, current={}, yes=True)

    for key, default in PUBLISH_PORT_DEFAULTS.items():
        assert result[key] == default


def test_render_env_writes_publish_ports():
    values = {f.key: f.default for g in inst.WIZARD_GROUPS for f in g.fields}
    rendered = inst.render_env(values, extra={})

    for key, default in PUBLISH_PORT_DEFAULTS.items():
        assert f"{key}={default}" in rendered
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `.venv/bin/pytest tests/install/test_install_wizard.py -q -k "publish_port or storage_group"`
Expected: FAIL — `KeyError: 'PARADEDB_PUBLISH_PORT'`.

- [ ] **Step 3: Добавить поля в группу wizard**

В `reviewer/install.py` в группе `EnvGroup(title="Хранилища (Postgres / Neo4j)", ...)` дописать
три поля после `NEO4J_PASSWORD`:

```python
            EnvField(
                key="PARADEDB_PUBLISH_PORT",
                prompt_text="PARADEDB_PUBLISH_PORT (публикуемый порт ParadeDB в docker compose)",
                default="5433",
                derive_default=lambda values: _port_from_url(values.get("PG_DSN", ""), "5433"),
            ),
            EnvField(
                key="NEO4J_BOLT_PUBLISH_PORT",
                prompt_text="NEO4J_BOLT_PUBLISH_PORT (публикуемый bolt-порт Neo4j)",
                default="7687",
                derive_default=lambda values: _port_from_url(values.get("NEO4J_URI", ""), "7687"),
            ),
            EnvField(
                key="NEO4J_HTTP_PUBLISH_PORT",
                prompt_text="NEO4J_HTTP_PUBLISH_PORT (публикуемый порт браузера Neo4j)",
                default="7474",
            ),
```

- [ ] **Step 4: Добавить ключи в `ENV_TEMPLATE`**

В `_ENV_TEMPLATE_BASE` секцию
`# --- Postgres (ParadeDB :5433) / Neo4j (:7687) — дефолты docker-compose ---` дополнить после
`NEO4J_PASSWORD=reviewerpass`:

```
# Публикуемые (хостовые) порты docker compose; контейнерные порты фиксированы.
PARADEDB_PUBLISH_PORT=5433
NEO4J_BOLT_PUBLISH_PORT=7687
NEO4J_HTTP_PUBLISH_PORT=7474
```

- [ ] **Step 5: Синхронизировать `.env.example`**

В `.env.example` в секции `Postgres (ParadeDB: pgvector + pg_search) / Neo4j` после
`NEO4J_PASSWORD=reviewerpass` (строка 60) дописать:

```
# Публикуемые (хостовые) порты docker compose; контейнерные порты (5432/7687/7474) фиксированы.
# reviewer init выводит первые два из PG_DSN/NEO4J_URI.
PARADEDB_PUBLISH_PORT=5433
NEO4J_BOLT_PUBLISH_PORT=7687
NEO4J_HTTP_PUBLISH_PORT=7474
```

Блок `TEST_*` (строки 62-71) не трогать.

- [ ] **Step 6: Запустить тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest tests/install/test_install_wizard.py -q`
Expected: PASS. Особое внимание — два существующих guard-теста паритета: `ENV_TEMPLATE` ↔
`.env.example` (около строки 521) и «в `ENV_TEMPLATE` есть все ключи wizard» (около строки 535).
Оба зелёные только если шаги 3-5 внесены согласованно.

- [ ] **Step 7: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/install.py tests/install/test_install_wizard.py
git add reviewer/install.py .env.example tests/install/test_install_wizard.py
git commit -m "feat(install): publish-порты хранилищ в wizard, ENV_TEMPLATE и .env.example"
```

---

### Task 4: Предупреждение о расхождении publish-порта с DSN/URI

**Files:**
- Modify: `reviewer/install.py` (новая публичная функция рядом с `_effective_default`),
  `reviewer/entrypoints/cli.py:1419-1422` (после сбора `values`, до `render_env`)
- Test: `tests/install/test_install_wizard.py`

**Interfaces:**
- Consumes: `install._port_from_url` (задача 2), ключи publish-портов (задача 3).
- Produces: `install.publish_port_warnings(values: Mapping[str, str]) -> list[str]` — список
  готовых текстов предупреждений (пустой, когда всё согласовано).

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/install/test_install_wizard.py`:

```python
def test_publish_port_warnings_reports_local_mismatch():
    warnings = inst.publish_port_warnings(
        {
            "PG_DSN": "postgresql://reviewer:reviewer@localhost:6543/reviewer",
            "PARADEDB_PUBLISH_PORT": "5433",
            "NEO4J_URI": "neo4j://localhost:7687",
            "NEO4J_BOLT_PUBLISH_PORT": "7687",
        }
    )

    assert len(warnings) == 1
    assert "PARADEDB_PUBLISH_PORT" in warnings[0]
    assert "6543" in warnings[0]
    assert "5433" in warnings[0]


def test_publish_port_warnings_silent_when_ports_agree():
    assert inst.publish_port_warnings(
        {
            "PG_DSN": "postgresql://reviewer:reviewer@localhost:5433/reviewer",
            "PARADEDB_PUBLISH_PORT": "5433",
            "NEO4J_URI": "neo4j://localhost:7687",
            "NEO4J_BOLT_PUBLISH_PORT": "7687",
        }
    ) == []


def test_publish_port_warnings_ignore_remote_hosts():
    # Внешнее хранилище: publish-порт compose к этому подключению отношения не имеет
    assert inst.publish_port_warnings(
        {
            "PG_DSN": "postgresql://reviewer:reviewer@db.internal:5432/reviewer",
            "PARADEDB_PUBLISH_PORT": "5433",
            "NEO4J_URI": "neo4j://graph.internal:7687",
            "NEO4J_BOLT_PUBLISH_PORT": "7687",
        }
    ) == []


def test_publish_port_warnings_ignore_unparsable_or_missing_values():
    assert inst.publish_port_warnings({}) == []
    assert inst.publish_port_warnings(
        {"PG_DSN": "not a url", "PARADEDB_PUBLISH_PORT": "5433"}
    ) == []
    assert inst.publish_port_warnings(
        {"PG_DSN": "postgresql://reviewer@localhost/reviewer", "PARADEDB_PUBLISH_PORT": "5433"}
    ) == []


def test_publish_port_warnings_reports_both_pairs():
    warnings = inst.publish_port_warnings(
        {
            "PG_DSN": "postgresql://reviewer:reviewer@127.0.0.1:6543/reviewer",
            "PARADEDB_PUBLISH_PORT": "5433",
            "NEO4J_URI": "neo4j://localhost:7999",
            "NEO4J_BOLT_PUBLISH_PORT": "7687",
        }
    )

    assert len(warnings) == 2


def test_init_yes_warns_about_port_mismatch(tmp_path, monkeypatch):
    dest = tmp_path / ".env"
    dest.write_text(
        "VOYAGE_API_KEY=sk-test\n"
        "PG_DSN=postgresql://reviewer:reviewer@localhost:6543/reviewer\n"
        "PARADEDB_PUBLISH_PORT=5433\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)

    result = CliRunner().invoke(cli, ["init", "--scope", "global", "--yes"])

    assert result.exit_code == 0, result.output
    assert "PARADEDB_PUBLISH_PORT" in result.output
    assert "6543" in result.output
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `.venv/bin/pytest tests/install/test_install_wizard.py -q -k "publish_port_warnings or port_mismatch"`
Expected: FAIL — `AttributeError: module 'reviewer.install' has no attribute
'publish_port_warnings'`.

- [ ] **Step 3: Реализовать сверку в `install.py`**

Добавить рядом с `_effective_default`:

```python
# Хосты, для которых publish-порт docker compose и порт клиентской строки — одно и то же.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Пары «клиентская строка → переменная публикуемого порта». HTTP-порт Neo4j не сверяется:
# в NEO4J_URI его нет, выводить не из чего.
_PUBLISH_PORT_PAIRS: tuple[tuple[str, str], ...] = (
    ("PG_DSN", "PARADEDB_PUBLISH_PORT"),
    ("NEO4J_URI", "NEO4J_BOLT_PUBLISH_PORT"),
)


def publish_port_warnings(values: Mapping[str, str]) -> list[str]:
    """Расхождения publish-порта compose с портом локальной строки подключения.

    Сверяем только локальный хост: при внешнем Postgres/Neo4j публикуемый порт
    контейнера к подключению отношения не имеет, и предупреждение было бы ложным.
    """
    warnings: list[str] = []
    for url_key, port_key in _PUBLISH_PORT_PAIRS:
        url = (values.get(url_key) or "").strip()
        published = (values.get(port_key) or "").strip()
        if not url or not published:
            continue
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower()
            port = parsed.port
        except ValueError:
            continue
        if host not in _LOCAL_HOSTS or port is None:
            continue
        if str(port) != published:
            warnings.append(
                f"{url_key} указывает порт {port}, а {port_key}={published}: "
                f"контейнер опубликует {published}, и подключение не сойдётся."
            )
    return warnings
```

Если `Mapping` ещё не импортирован в `reviewer/install.py`, добавить в шапку
`from collections.abc import Callable, Mapping` (объединив с импортом из задачи 2).

- [ ] **Step 4: Печатать предупреждения в `reviewer init`**

В `reviewer/entrypoints/cli.py` в команде `init`, сразу после строки
`extra = {key: value for key, value in current.items() if key not in wizard_keys}` (строка 1421) и
**до** `content = inst.render_env(values, extra)`:

```python
            for warning in inst.publish_port_warnings(values):
                click.echo(f"⚠ {warning}")
```

Расположение покрывает обе ветки сбора `values` — интерактивную и `--yes`. Код возврата и
содержимое `.env` не меняются.

- [ ] **Step 5: Запустить тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest tests/install/test_install_wizard.py -q`
Expected: PASS.

- [ ] **Step 6: Прогнать весь unit-набор**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключены настройкой `addopts` в `pyproject.toml`).

- [ ] **Step 7: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/install.py reviewer/entrypoints/cli.py tests/install/test_install_wizard.py
git add reviewer/install.py reviewer/entrypoints/cli.py tests/install/test_install_wizard.py
git commit -m "feat(install): предупреждение о расхождении publish-порта с DSN/URI"
```

---

### Task 5: Документация в README.md и README.ru.md

**Files:**
- Modify: `README.md` (раздел `### Required services and credentials`, строки 311-326),
  `README.ru.md` (раздел `### Сервисы и credentials`, строки 316-331)
- Test: `tests/docs/test_readme_onboarding.py` (новый тест рядом с
  `test_readmes_document_web_container_runtime_ports`, строка 249)

**Interfaces:**
- Consumes: имена переменных и дефолты из задач 1 и 3; статус `preserved` апдейтера
  (`reviewer/update_lifecycle.py:189-220`).
- Produces: ничего (терминальная задача).

- [ ] **Step 1: Написать падающий guard-тест**

Добавить в `tests/docs/test_readme_onboarding.py` после
`test_readmes_document_web_container_runtime_ports`:

```python
def test_readmes_document_storage_publish_ports() -> None:
    for filename, heading in (
        ("README.md", "### Required services and credentials"),
        ("README.ru.md", "### Сервисы и credentials"),
    ):
        section = _section(_read(filename), heading)
        for marker in (
            "PARADEDB_PUBLISH_PORT",
            "NEO4J_BOLT_PUBLISH_PORT",
            "NEO4J_HTTP_PUBLISH_PORT",
            "preserved",
        ):
            assert marker in section, (filename, marker)
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/docs/test_readme_onboarding.py::test_readmes_document_storage_publish_ports -q`
Expected: FAIL — маркера `PARADEDB_PUBLISH_PORT` в разделе нет.

- [ ] **Step 3: Дописать абзац в `README.md`**

В `README.md` в разделе `### Required services and credentials` после строки
`- board credentials: provider-specific env declared in the registry.` и пустой строки вставить:

```markdown
Published host ports of the Compose storage services are variables, not literals:
`PARADEDB_PUBLISH_PORT` (default `5433`), `NEO4J_BOLT_PUBLISH_PORT` (default `7687`) and
`NEO4J_HTTP_PUBLISH_PORT` (default `7474`). Container ports stay fixed. `reviewer init` asks for
them in the storage group and derives the first two from `PG_DSN` and `NEO4J_URI`, so the client
string and the published port cannot drift apart silently; a mismatch on a local host prints a
warning without blocking.

```bash
PARADEDB_PUBLISH_PORT=6543 NEO4J_BOLT_PUBLISH_PORT=7999 \
  docker compose -f ~/.config/rag-reviewer/docker-compose.yml up -d
```

Prefer variables over editing the Compose file: a hand-edited
`~/.config/rag-reviewer/docker-compose.yml` no longer matches its recorded hash, so `reviewer
update` treats it as user-modified (status `preserved`) and stops delivering new Compose
definitions to it.
```

- [ ] **Step 4: Дописать зеркальный абзац в `README.ru.md`**

В `README.ru.md` в разделе `### Сервисы и credentials` после строки
`- board credentials: provider-specific env из registry.` и пустой строки вставить:

```markdown
Публикуемые хостовые порты storage-сервисов Compose заданы переменными, а не литералами:
`PARADEDB_PUBLISH_PORT` (дефолт `5433`), `NEO4J_BOLT_PUBLISH_PORT` (дефолт `7687`) и
`NEO4J_HTTP_PUBLISH_PORT` (дефолт `7474`). Контейнерные порты фиксированы. `reviewer init`
спрашивает их в группе хранилищ и выводит первые два из `PG_DSN` и `NEO4J_URI`, поэтому строка
подключения и публикуемый порт не разъезжаются молча; расхождение на локальном хосте печатает
предупреждение, но не блокирует.

```bash
PARADEDB_PUBLISH_PORT=6543 NEO4J_BOLT_PUBLISH_PORT=7999 \
  docker compose -f ~/.config/rag-reviewer/docker-compose.yml up -d
```

Настраивайте переменными, а не правкой Compose-файла: отредактированный вручную
`~/.config/rag-reviewer/docker-compose.yml` перестаёт совпадать с записанным hash, поэтому
`reviewer update` считает его изменённым пользователем (статус `preserved`) и больше не доставляет
в него новые Compose-описания.
```

- [ ] **Step 5: Запустить тесты документации**

Run: `.venv/bin/pytest tests/docs/ -q`
Expected: PASS — новый тест и существующие guard-тесты паритета README
(`test_readmes_share_content_section_order`, `test_all_readme_links_and_local_anchors_resolve`).

- [ ] **Step 6: Прогнать весь unit-набор**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 7: Коммит**

```bash
git add README.md README.ru.md tests/docs/test_readme_onboarding.py
git commit -m "docs(readme): настраиваемые publish-порты хранилищ и ограничение preserved"
```

---

## Ручная приёмка (после всех задач)

Критерии 2 и 3 требуют живого стека и в unit-тесты не выносятся.

- [ ] `docker compose config` без переменных → публикуются `5433`, `7474`, `7687` (критерий 1).
- [ ] `PARADEDB_PUBLISH_PORT=6543 NEO4J_BOLT_PUBLISH_PORT=7999 docker compose up -d` → порты
      подняты; `PG_DSN`/`NEO4J_URI` с теми же портами → `reviewer check` проходит (критерий 2).
- [ ] `reviewer init` с нестандартным портом в `PG_DSN` → мастер предлагает выведенный
      publish-порт; итоговый `.env` согласован; стек поднимается без правки compose (критерий 3).
- [ ] Вернуть окружение к дефолтным портам (`docker compose up -d` без переменных).
