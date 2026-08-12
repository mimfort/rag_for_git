# PRI-242 — `reviewer start` / `reviewer stop`: управление локальной инфраструктурой из CLI

Бриф: `docs/superpowers/briefs/2026-08-12-PRI-242-reviewer-start-stop-infra-cli.md`

## Проблема

`reviewer` уже владеет управляемым `docker-compose.yml`: скачивает его с `main`
(`reviewer/update_lifecycle.py:19-21,63-73`), атомарно кладёт в `$XDG_CONFIG_HOME/rag-reviewer`
и не затирает пользовательские правки (`reviewer/update_lifecycle.py:179-220`). Но запустить
этот файл он не умеет — пользователь обязан сам знать путь к нему и держать в голове команду
`docker compose -f …`. Кода, вызывающего docker, в `reviewer/` нет вообще.

Вторая, менее очевидная половина проблемы: dev-сервисы `paradedb` и `neo4j` в compose
**не имеют healthcheck** — он есть только у test-профиля (`docker-compose.yml:51-57,67-73`).
Для сервиса без healthcheck `docker compose up -d --wait` возвращается по достижении состояния
`running`, то есть до того, как Postgres начинает принимать соединения. Наблюдаемое следствие:
сразу после старта контейнеров `reviewer status` получает `connection refused` и уходит в
retry/backoff. Команда `start`, которая рапортует об успехе в этот момент, врала бы.

## Архитектура

Новый модуль `reviewer/compose_lifecycle.py` владеет рантаймом инфраструктуры.
Он намеренно отделён от `reviewer/update_lifecycle.py`: тот отвечает за *доставку* артефактов
(скачать, атомарно записать, сохранить чужие правки), а новый — за *жизненный цикл* уже
доставленного файла. Разные поводы для изменения, разные зависимости.

Разделение ответственности внутри фичи: модуль **классифицирует** исход, CLI **формулирует**
русские сообщения и код возврата. Благодаря этому unit-тесты argv и классификации не зависят
от Click, а тесты CLI — от subprocess.

### Контракт модуля

```python
COMPOSE_PROJECT = "rag-reviewer"
WAIT_TIMEOUT_SECONDS = 300

class ComposeStatus(StrEnum):
    OK = "ok"
    COMPOSE_MISSING = "compose_missing"
    DOCKER_MISSING = "docker_missing"
    DAEMON_UNAVAILABLE = "daemon_unavailable"
    FAILED = "failed"

@dataclass(frozen=True)
class ComposeResult:
    status: ComposeStatus
    returncode: int
    stdout: str
    stderr: str
    compose_path: Path

def compose_file_path(config_dir: Path | None = None) -> Path
def build_compose_argv(compose_path: Path, *arguments: str) -> list[str]
def start_services(*, config_dir: Path | None = None, run: Callable = subprocess.run) -> ComposeResult
def stop_services(*, config_dir: Path | None = None, run: Callable = subprocess.run) -> ComposeResult
```

`run` инжектируется параметром — паттерн `run_fresh_artifact_refresh`
(`reviewer/update_lifecycle.py:76-99`), который делает subprocess мокаемым без патчинга
глобалей. Путь резолвится через существующий `default_config_dir()`
(`reviewer/update_lifecycle.py:102-104`), поэтому `XDG_CONFIG_HOME` уважается автоматически.

### Собираемые команды

```
docker compose -p rag-reviewer -f <config_dir>/docker-compose.yml up -d --wait --wait-timeout 300
docker compose -p rag-reviewer -f <config_dir>/docker-compose.yml stop
```

**Имя проекта задаётся явно (`-p rag-reviewer`).** Без него docker выводит имя из имени
директории compose-файла — неявный побочный эффект пути, ломающийся при смене
`XDG_CONFIG_HOME`. Явное имя также делает предсказуемым сосуществование с клоном репозитория:
`docker compose up -d` из клона создаёт проект `rag_for_git` с собственными томами, и два стека
конфликтуют за порт 5433. Разрешение — документация, а не код (см. «Документация»).

**`stop`, а не `down`.** У `docker compose stop` нет флага `-v` в принципе, поэтому запрет из
`CLAUDE.md` («никогда не `down -v`»: dev- и test-сервисы делят Compose-проект, и `-v` снесёт
production named volumes) соблюдается по конструкции, а не договорённостью. `down` без `-v`
дал бы тот же эффект по данным, но оставил бы катастрофу в одном символе от нормального
использования.

Идемпотентность обеспечивает Docker: повторный `up -d` и повторный `stop` возвращают 0.
Собственного состояния команды не держат.

### Классификация исходов

| Условие | Статус |
|---|---|
| compose-файла нет на диске | `COMPOSE_MISSING` — docker **не вызывается** |
| `run` бросает `OSError` (включая `FileNotFoundError`) | `DOCKER_MISSING` |
| ненулевой код, в stderr сигнатура недоступного демона | `DAEMON_UNAVAILABLE` |
| ненулевой код, прочее | `FAILED` |
| нулевой код | `OK` |

Сигнатуры демона (проверка по нижнему регистру stderr): `cannot connect to the docker daemon`,
`is the docker daemon running`, `docker daemon is not running`, `error during connect`.
Не совпало — исход `FAILED` с сырым stderr, то есть неизвестная ошибка показывается, а не
маскируется под известную.

Ни одна ветка не даёт трейсбека: отсутствие файла проверяется до вызова, `OSError`
перехватывается в модуле.

## Healthcheck dev-сервисов

Чтобы `--wait` означал готовность, а не факт запуска, dev-сервисы получают healthcheck по
образцу test-профиля, с их собственными кредами (`docker-compose.yml:5-7,14`):

```yaml
paradedb:
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U reviewer -d reviewer"]
    start_period: 180s
    start_interval: 2s
    interval: 30s
    timeout: 2s
    retries: 3
neo4j:
  healthcheck:
    test: ["CMD-SHELL", "cypher-shell -u neo4j -p reviewerpass 'RETURN 1'"]
    start_period: 180s
    start_interval: 2s
    interval: 300s
    timeout: 3s
    retries: 3
```

Редкий idle-`interval` у neo4j — наследие ID-268: `cypher-shell` поднимает JVM (~2.5 c CPU за
пробу), поэтому частые пробы допустимы только в фазе старта, через `start_interval`.
`start_interval` требует Docker Engine ≥ 25.0; на старых движках он игнорируется и старт ждёт
до первой обычной пробы — отсюда явный `--wait-timeout 300`: команда завершается с внятным
сообщением вместо неопределённо долгого ожидания, а контейнеры остаются поднятыми.

Правка идёт в `docker-compose.yml` репозитория и доезжает до пользователей через
`reviewer update` (`COMPOSE_URL` указывает на `main`).

## Поверхность CLI

Обе команды — без опций: имя проекта фиксировано, путь резолвится, таймаут — константа модуля.

| Статус | Сообщение | Код |
|---|---|---|
| `OK` (start) | `✓ Инфраструктура запущена (проект rag-reviewer): ParadeDB, Neo4j` | 0 |
| `OK` (stop) | `✓ Инфраструктура остановлена; тома и индекс сохранены` | 0 |
| `COMPOSE_MISSING` | `✗ <path> не найден — выполните reviewer update` | 1 |
| `DOCKER_MISSING` | `✗ docker не найден в PATH — установите Docker` | 1 |
| `DAEMON_UNAVAILABLE` | `✗ docker установлен, но демон не отвечает — запустите Docker и повторите` | 1 |
| `FAILED` | `✗ docker compose завершился с кодом N` + stderr | 1 |

Вывод через `click.echo` + `SystemExit(1)` — как в `check` (`reviewer/entrypoints/cli.py:840-842`),
без английского префикса `Error:`, который добавил бы `click.ClickException`.

### Подсказка в `check`

После блоков Postgres и Neo4j (`reviewer/entrypoints/cli.py:769-815`) печатается одна строка,
если хотя бы одно хранилище недоступно **и** его адрес — loopback (`localhost`, `127.0.0.1`,
`::1`):

```
  Подсказка: локальные хранилища не отвечают — запустите reviewer start
```

Ограничение по loopback добавлено сверх формулировки тикета осознанно: деплою с удалёнными
хранилищами совет поднять локальный docker заведомо не помогает и уводит диагностику в сторону.
Подсказка не влияет на код возврата.

### Регистрация в launcher

```python
("start",): CommandPresentation(
    summary="Запустить локальную инфраструктуру",
    details="Поднимает ParadeDB и Neo4j из управляемого docker-compose и ждёт готовности healthcheck.",
    effects=(Effect.NETWORK, Effect.WRITE),
    scenarios=("Перед индексацией", "После перезагрузки машины"),
    keywords=("docker", "compose", "postgres", "neo4j"),
),
("stop",): CommandPresentation(
    summary="Остановить локальную инфраструктуру",
    details="Останавливает контейнеры reviewer, сохраняя named volumes и построенный индекс.",
    effects=(Effect.WRITE,),
    scenarios=("Освободить ресурсы машины",),
    keywords=("docker", "compose", "stop"),
),
```

`stop` помечен `WRITE`, а не `DESTRUCTIVE`: данные переживают команду, а `DESTRUCTIVE` в этом
каталоге закреплён за `gc`, который действительно удаляет. Запись обязательна механически —
`tests/launcher/test_catalog.py:145` требует равенства множества презентаций и множества видимых
команд.

## Конфигурация окружения

Явной проводки переменных не требуется: `.env` и `docker-compose.yml` лежат в одном каталоге
`$XDG_CONFIG_HOME/rag-reviewer`, а при `-f <path>` docker берёт project directory из директории
compose-файла — значит publish-порты (`PARADEDB_PUBLISH_PORT`, `NEO4J_BOLT_PUBLISH_PORT`,
`NEO4J_HTTP_PUBLISH_PORT`, PRI-241) подхватываются оттуда же.

Граница, которую нужно знать: если reviewer запущен с `REVIEWER_ENV_FILE` или читает `./.env`
из клона репозитория (`reviewer/config/settings.py:26-35`), compose всё равно прочитает
конфиговый `.env`. Расхождение возможно; оно документируется, а не синхронизируется кодом.

## Тестирование

`tests/test_compose_lifecycle.py` — unit, без Click и без реального subprocess (инъекция `run`,
паттерн `tests/test_update_lifecycle.py:110-136`):

- точный argv `start` и точный argv `stop`;
- **guard инварианта**: argv `stop` не содержит ни `down`, ни `-v`, ни `--volumes`;
- `COMPOSE_MISSING` при отсутствующем файле — с проверкой, что `run` не вызывался ни разу;
- `DOCKER_MISSING` из `OSError`; `DAEMON_UNAVAILABLE` по сигнатуре stderr; `FAILED` на прочем;
- идемпотентность: два подряд `start` дают одинаковый argv и `OK` оба раза;
- `compose_file_path` уважает `XDG_CONFIG_HOME`.

`tests/entrypoints/test_infra_commands.py` — `CliRunner` + `monkeypatch.setattr` на функции
модуля (паттерн `tests/entrypoints/test_update_command.py:513-553`): по тесту на каждый статус —
код возврата, текст, отсутствие `Traceback` в выводе. Плюс две ветки `check`: недоступный
loopback-Postgres → подсказка есть; недоступный удалённый хост → подсказки нет.

`tests/test_infrastructure_policy.py` — dev-сервисы через существующий хелпер
`_assert_cheap_idle_healthcheck` (`tests/test_infrastructure_policy.py:234-254`): `paradedb`
с пробой `pg_isready -U reviewer -d reviewer` и `min_interval=30`, `neo4j` с `cypher-shell`
и `min_interval=300`. Так `--wait` перестаёт быть декоративным на уровне контракта файла.

Все тесты — unit: сети и docker не требуют, маркер `integration` не нужен.

## Документация

`README.md` и `README.ru.md` синхронно (обе команды, сохранность томов при `stop`) плюс два
предупреждения:

- контрибьютор в клоне репозитория пользуется `docker compose up -d` (проект `rag_for_git`) и не
  запускает оба стека одновременно — они конфликтуют за порт 5433 и имеют разные тома;
- вручную правленый compose получает от синка статус `preserved`, healthcheck к нему не приедет,
  и `--wait` у такого пользователя останется проверкой состояния `running`.

## Вне скоупа

Test-профиль (`paradedb-test`/`neo4j-test`) и его lifecycle; `web`-профиль, который без
`--profile web` не активируется; любые флаги CLI поверх названных; автоскачивание compose-файла
при его отсутствии — тикет требует подсказки `reviewer update`, и подмена подсказки сетевым
действием сделала бы `start` неожиданно сетевой командой.
