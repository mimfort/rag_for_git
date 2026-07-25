# Brief — PRI-216 Healthcheck тестовых сервисов жжёт CPU вхолостую (docker-compose)
https://ru.yougile.com/team/686c049c8af8/#PRI-216

## Task
- Ключ PRI-216 (стор: ID-268, alias PRI-216), статус «Запуск / CI / хуки». Данные из стора reviewer после sync_board, тонких критериев не было (заголовок «## Критерии приёмки» есть в description — обогащение через board-MCP не требовалось).
- Проблема: `neo4j-test` healthcheck (`cypher-shell`, JVM-старт) с `interval: 2s` крутится бесконечно даже после healthy → ~47% ядра в простое; `paradedb-test` (`pg_isready`) с тем же `interval: 2s` — дешёвый бинарник, ~1.6%.
- Требуется: для обоих test-сервисов заменить постоянный `interval: 2s` на `start_period: 60s` + `start_interval: 2s` + `interval: 30s` (Docker >= 25.0/API >= 1.44 — на машине разработчика есть).
- Критерии приёмки: (1) neo4j-test в простое < 5% ядра (cgroup usage_usec, окно ≥10с); (2) `docker compose --profile test up -d --wait paradedb-test neo4j-test` не медленнее прежнего; (3) `.venv/bin/pytest -q -m integration` проходит как прежде.

## Related work
- (dropped 7: `search_tasks` вернул 7 задач помимо самой PRI-216 — zero-infra SQLite-режим, таймауты git/uvx в preflight, онбординг-мастер (done), CI-рецепт GitHub Action, флаг test-coverage gap, проверка свежести индекса (done), purge orphaned tasks (done) — все из смежной области CI/инфры, но ни одна не про healthcheck/interval/CPU-троттлинг docker-compose; `get_task_context` не нашёл связанных задач/PR)

## Relevant code
- `docker-compose.yml:32-36` — healthcheck `paradedb-test` (`pg_isready -U reviewer_test -d reviewer_test`, `interval: 2s`, `timeout: 2s`, `retries: 30`) — править по п.2 задачи.
- `docker-compose.yml:44-48` — healthcheck `neo4j-test` (`cypher-shell -u neo4j -p reviewer_test_pass 'RETURN 1'`, `interval: 2s`, `timeout: 3s`, `retries: 30`) — править по п.1 задачи, основной источник CPU.
- `tests/test_infrastructure_policy.py:214-245` (`test_compose_defines_isolated_test_profile_services`) — парсит `docker-compose.yml` через `yaml.safe_load` и делает **точное** `==`-сравнение словаря `healthcheck` для обоих сервисов (только ключи `test`/`interval`/`timeout`/`retries`). Тест **не помечен** `@pytest.mark.integration` → падает уже в обычном `pytest -q`, не только в `-m integration`. Любая правка `interval`/добавление `start_period`/`start_interval` ломает это утверждение — обязательно обновить assert вместе с compose-файлом.
- (dropped 3: `tests/test_infrastructure_policy.py:248-256`/`259-269`/`272-284` — тоже парсят `docker-compose.yml`, но проверяют `image`/`tmpfs`/teardown-комментарий, не `healthcheck` — задача их не трогает)

## Constraints / open questions
- Граф кода (SCIP) и `implementations`/`callers`/`related_symbols` не применимы — `docker-compose.yml` не Python-код, символов/node_id нет; изменения чисто конфигурационные + один YAML-парсящий unit-тест.
- CI (`.github/workflows/*.yml`) не запускает `docker compose --profile test` — грепом ничего не найдено, изменение не затрагивает CI-джобы.
- Обязательно синхронно обновить `tests/test_infrastructure_policy.py:226-244` (новые значения `healthcheck` для `paradedb-test` и `neo4j-test`, плюс новые ключи `start_period`/`start_interval`) — иначе даже unit-прогон (не только `-m integration`) красный.
- Критерий 1 (< 5% ядра) и критерий 4 задачи («замерить CPU») требуют ручного замера через cgroup `usage_usec` на живом простаивающем стенде — не автоматизируется тестами репозитория, это ручная/скриптовая проверка после правки.
- `start_interval` требует Docker Engine ≥ 25.0 / API ≥ 1.44 (задача подтверждает: на машине разработчика Docker 29.2.1/API 1.53, Compose v5.0.2 — совместимо); если CI/чужие машины используют более старый Docker, `start_interval` будет проигнорирован движком (не ошибка, но не даст быстрого healthy на старте) — стоит явно проверить перед мержем, если есть другие среды запуска.

Собран на: средний тир (Sonnet), режим: subagent

## Токены (этап solve-task)
Модель: claude-opus-5
fresh-in 58 · out 27.4K · cache-write 202.7K · cache-read 2.1M
Всего: 2.3M токенов
