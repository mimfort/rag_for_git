# Brief — PRI-222 Вынести настройку порта web-контейнера из Dockerfile
https://ru.yougile.com/team/686c049c8af8/#PRI-222

## Task
- Ключ PRI-222 (store: ID-276, alias PRI-222), статус «Бэклог»; данные получены из reviewer store после `sync_board`.
- Убрать `EXPOSE` и литерал `8000` из `web/Dockerfile`; один image должен принимать внутренний host/port при запуске без rebuild.
- Сохранить единый startup-путь через `reviewer serve --host/--port` и совместимый пользовательский default 8000.
- Добавить документированный `docker run` и opt-in Compose-профиль `web`; обычный `docker compose up` остаётся только инфраструктурным.
- Приёмка: HTTP smoke-check одного image на двух внутренних портах, publish-port принадлежит runtime/deploy-конфигурации.

## Related work
- PRI-216 — переиспользовать паттерн изменения opt-in Compose-профиля вместе с декларативным unit-guard тестом и явной документацией ограничений.
- (dropped 6: задачи про init, launcher, lite-режим, изоляцию unit-тестов и сводки не задают конкретный Docker/runtime-паттерн для этой реализации)

## Subsystems
- reviewer/entrypoints — CLI-команда `reviewer serve` уже владеет host/port и должна остаться единственным серверным startup-интерфейсом.
- reviewer/web — FastAPI-приложение предоставляет HTTP endpoint для smoke-check и не должно получать Docker-специфичную конфигурацию.
- tests/entrypoints — существующие CLI-тесты показывают паттерн `CliRunner` и monkeypatch, если потребуется закрепить контракт `serve`.
- tests/docs — guard-тесты поддерживают синхронность пользовательских команд в `README.md` и `README.ru.md`.
- tests — инфраструктурная политика проверяет Compose как YAML без внешней сети и подходит для opt-in/profile/runtime-port инвариантов.

## Relevant code
- `reviewer/entrypoints/cli.py:533` — `serve` уже принимает `--host` и `--port`, default порта 8000 остаётся здесь как совместимый runtime-дефолт.
- `reviewer/web/app.py:33` — `create_app` создаёт HTTP-приложение независимо от listen-port; Docker-логика сюда не нужна.
- (dropped 23: остальные Python-хиты не требуется менять или копировать; ключевые Docker/Compose-файлы не индексируются как Python-код)

## Test exemplars
- `tests/test_infrastructure_policy.py:249` — парсит `docker-compose.yml` через `yaml.safe_load` и закрепляет profile/environment/ports декларативными unit-asserts без запуска контейнеров.
- `tests/docs/test_readme_onboarding.py:190` — проверяет команды onboarding одновременно в английском и русском README и задаёт паттерн документационного guard-теста.
- (dropped 23: остальные тестовые хиты покрывают FastAPI/CLI internals или несвязанные Docker-инварианты и не информируют проверку runtime-портов)

## Constraints / open questions
- `web/Dockerfile` и `docker-compose.yml` не попали в Python-only base retrieval; точные runtime-поля нужно подтвердить чтением рабочего дерева перед дизайном.
- Smoke-тест должен реально доказывать повторное использование одного image на двух внутренних портах, но не должен попадать в обычный unit-прогон с запретом localhost/network.
- Compose web-сервис обязан иметь явный `profiles` opt-in и не менять поведение базового `docker compose up`.
- Criteria в store пусты как отдельное поле, но полный раздел «Критерии приёмки» присутствует в description и перенесён в Task.
- Существующих артефактов PRI-222 не найдено; индекс `dev` свежий (`drift=0`), сводки были тёплыми и не обновлялись согласно ограничению «только на Luna».

Собран на: session model (premium), режим: inline
