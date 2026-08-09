# PRI-222 — Runtime-настройка порта web-контейнера

Задача: https://ru.yougile.com/team/686c049c8af8/#PRI-222
Бриф: `docs/superpowers/briefs/2026-08-09-PRI-222-runtime-web-container-port.md`

## Проблема

`web/Dockerfile` одновременно объявляет `EXPOSE 8000` и запускает Uvicorn с
`port=8000` внутри inline `python -c`. Это создаёт второй startup-путь рядом с
`reviewer serve --host/--port` и заставляет переопределять команду или пересобирать
образ для другого внутреннего порта.

Ранее сервис `web` был удалён из обычного Compose, потому что тяжёлая сборка SPA не
должна запускаться вместе с обязательной инфраструктурой. PRI-222 не отменяет этот
инвариант: Compose-сервис возвращается только под явным profile `web`.

## Цели

- Один Python startup-путь создаёт FastAPI app и запускает Uvicorn для host/port.
- `web/Dockerfile` не содержит `EXPOSE` и литерал `8000`.
- Один image без rebuild запускается минимум на двух внутренних портах.
- Внутренний listen-port и опубликованный host-port настраиваются независимо.
- Дефолтный контейнерный сценарий остаётся на порту 8000.
- Обычный `docker compose up` не собирает и не запускает web.

## Не цели

- Не менять API, SPA, storage-схему или Basic Auth.
- Не устанавливать в web-image полный RAG/graph/MCP dependency stack.
- Не переносить Docker-настройки в `Settings`: listen host/port принадлежат процессу,
  а не доменной конфигурации приложения.
- Не добавлять `EXPOSE`: это metadata образа, а не runtime-публикация порта.

## Выбранный подход

Добавить лёгкий модуль `reviewer.web.serve`, общий для Click-команды и контейнера.
Модуль импортируется без optional web dependencies; `uvicorn`, `Settings` и
`create_app` загружаются лениво только внутри `run_server`. Поэтому обычные команды
`reviewer` по-прежнему работают без `[web]` extra.

Альтернативы отклонены:

- shell-form `CMD` с inline `python -c` и env expansion сохраняет дублирующий startup;
- установка полного пакета с dependencies ради console script утяжеляет специализированный
  web-image и связывает его с RAG/graph stack.

## Runtime-модуль

`reviewer/web/serve.py` предоставляет:

- `DEFAULT_HOST = "127.0.0.1"` и `DEFAULT_PORT = 8000` для публичной Click-команды;
- `CONTAINER_DEFAULT_HOST = "0.0.0.0"` для module CLI внутри контейнера;
- `run_server(host: str, port: int) -> None`, который создаёт `Settings`, передаёт их в
  `create_app`, печатает адрес и вызывает `uvicorn.run`;
- `main(argv: Sequence[str] | None = None) -> None` на stdlib `argparse`;
- аргументы `--host`/`--port`, которые имеют приоритет над
  `REVIEWER_WEB_HOST`/`REVIEWER_WEB_PORT`;
- env/default chain: args → env → `0.0.0.0:8000` для module CLI.

Строковый env-default порта проходит через `argparse` с `type=int`. Невалидное значение
даёт стандартную понятную ошибку и ненулевой exit code до запуска Uvicorn.

`reviewer.entrypoints.cli::serve` сохраняет публичные defaults `127.0.0.1:8000`, но
вместо собственного создания app делегирует `run_server(host, port)`. Таким образом,
Click и контейнер различаются только способом получения аргументов, а запуск сервера
имеет одного владельца.

## Docker image

`web/Dockerfile` сохраняет текущую multi-stage сборку и минимальный набор web-зависимостей.
В runtime-набор добавляется отсутствующий `psycopg-pool>=3.2`, который напрямую импортирует
`reviewer.web.history`; полный dependency stack пакета не устанавливается. Из Dockerfile
удаляются `EXPOSE 8000` и inline Uvicorn. Новый exec-form startup:

```dockerfile
CMD ["python", "-m", "reviewer.web.serve"]
```

Dockerfile не задаёт host/port через `ENV`: module CLI предоставляет совместимый runtime
default, а deploy-слой может передать env или аргументы. Exec form сохраняет корректную
доставку сигналов Python-процессу.

## Compose

В `docker-compose.yml` добавляется сервис `web`:

```yaml
web:
  profiles: ["web"]
  build:
    context: .
    dockerfile: web/Dockerfile
  environment:
    PG_DSN: postgresql://reviewer:reviewer@paradedb:5432/reviewer
    REVIEWER_WEB_HOST: 0.0.0.0
    REVIEWER_WEB_PORT: ${REVIEWER_WEB_PORT:-8000}
  ports:
    - "127.0.0.1:${REVIEWER_WEB_PUBLISH_PORT:-8000}:${REVIEWER_WEB_PORT:-8000}"
  depends_on: ["paradedb"]
```

`REVIEWER_WEB_PORT` владеет внутренним listen-port. Отдельный
`REVIEWER_WEB_PUBLISH_PORT` владеет host mapping. Loopback binding сохраняет политику
безопасности dev-стека. Profile исключает web из обычного `docker compose up`, но
`docker compose --profile web up -d web` поднимает web и его обязательную зависимость.

## Документация

Секции Web admin в `README.md` и `README.ru.md` сохраняют host-run через
`reviewer serve` и добавляют два контейнерных сценария:

1. `docker build -f web/Dockerfile -t rag-reviewer-web .` и `docker run` с явным
   `PG_DSN`, независимыми внутренним портом и host mapping.
2. `docker compose --profile web up -d web`, плюс пример переопределения
   `REVIEWER_WEB_PORT` и `REVIEWER_WEB_PUBLISH_PORT`.

Текст явно предупреждает, что profile opt-in и обычный Compose остаётся инфраструктурным.
Английская и русская секции содержат одинаковые команды и env names.

## Тестирование

### Unit

- `tests/web/test_serve.py`: defaults и приоритет args/env; невалидный env-port;
  `run_server` создаёт app и передаёт точные host/port в `uvicorn.run`;
  Click `reviewer serve` делегирует общему runner.
- `tests/test_infrastructure_policy.py`: Dockerfile не содержит `EXPOSE` и `8000`,
  использует exec-form module startup; Compose-сервис имеет profile `web`, ожидаемые
  build/environment/depends_on и раздельные listen/publish переменные.
- `tests/docs/test_readme_onboarding.py`: обе Web admin секции документируют image build,
  `docker run`, opt-in profile и обе port variables.
- `docker compose --profile web config --quiet` проверяет итоговый Compose-синтаксис.

### Container smoke

Новый `tests/web/test_container_smoke.py` помечен `integration`:

- проверяет доступность Docker, иначе делает явный skip;
- один раз собирает image из `web/Dockerfile`;
- параметризованно запускает тот же image с двумя значениями
  `REVIEWER_WEB_PORT` без rebuild;
- публикует каждый внутренний порт на свободный loopback host-port и выполняет HTTP GET;
- всегда удаляет контейнеры и временный image в `finally`/fixture teardown.

Smoke не входит в обычный `pytest -q`, потому что unit-тестам запрещены сеть и внешние
сервисы. Он запускается адресно при приёмке на машине с Docker.

## Ошибки и безопасность

- Невалидный port останавливает процесс до bind.
- Занятый port и ошибки Uvicorn остаются явными runtime-ошибками без retry.
- Недоступный Postgres сохраняет существующий fail-soft startup FastAPI; UI/API сообщают
  проблему, но HTTP-процесс поднимается.
- Публикация Compose по умолчанию только на `127.0.0.1`; внешний доступ требует явной
  deploy-правки и настройки `WEB_ADMIN_USER`/`WEB_ADMIN_PASSWORD`.
- Секреты не добавляются в Dockerfile или Compose.

## Критерии готовности

- В `web/Dockerfile` отсутствуют `EXPOSE` и `8000`.
- Click и module CLI проходят через `run_server`.
- Один собранный image проходит HTTP smoke на двух внутренних портах.
- Compose config показывает `web` только как profile-сервис с независимыми port variables.
- `docker compose up` без profile не включает web.
- Default module CLI и Compose используют внутренний порт 8000 без дополнительной настройки.
- Unit-тесты, ruff, Compose config и container smoke проходят.
