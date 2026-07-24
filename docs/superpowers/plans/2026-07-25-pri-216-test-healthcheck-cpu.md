# PRI-216: снижение CPU healthcheck тестовых сервисов — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Развести стартовую и установившуюся фазы healthcheck у сервисов test-профиля, чтобы простаивающий `neo4j-test` жёг < 1% ядра вместо 51%, не замедлив старт стенда.

**Architecture:** Правка декларативная — в `docker-compose.yml` у обоих test-сервисов добавляются `start_period` + `start_interval` (частые пробы только в фазе старта) и увеличивается `interval` (редкие пробы в простое). Команда пробы не меняется, поэтому семантика готовности прежняя. Тест-страж `tests/test_infrastructure_policy.py` переводится с точного сравнения словаря `healthcheck` на гибрид: команда пробы и `retries` — точным `==`, тайминги — инвариантами.

**Tech Stack:** Docker Compose (test-профиль), pytest, PyYAML.

## Global Constraints

- `start_interval` требует **Docker Engine ≥ 25.0 / API ≥ 1.44**; на машине разработчика — 29.2.1 / 1.53.
- Язык проекта — русский: комментарии, докстринги, сообщения.
- Коммиты — Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By` и упоминаний Claude).
- `ruff check .` — line-length 100, target py311.
- Удаление тестовых сервисов — **только** `docker compose --profile test rm -sfv paradedb-test neo4j-test`. **Никогда** `docker compose --profile test down -v` (снесёт dev-сервисы и именованные тома).
- Ветка работы: `feature/pri-216` (уже создана, спека и бриф в ней закоммичены). `dev` защищена PR-required.
- Unit-тестам запрещены внешние и localhost-сокеты; тест политики читает `docker-compose.yml` как файл и остаётся unit-тестом (без `@pytest.mark.integration`).

---

## Task 1: Гибридный ассерт healthcheck + новые тайминги в compose

**Files:**
- Modify: `tests/test_infrastructure_policy.py:214-245` (тело `test_compose_defines_isolated_test_profile_services`) + два новых модульных хелпера рядом с ним
- Modify: `docker-compose.yml:32-36` (healthcheck `paradedb-test`), `docker-compose.yml:44-48` (healthcheck `neo4j-test`)
- Test: `tests/test_infrastructure_policy.py`

**Interfaces:**
- Consumes: ничего от предыдущих задач (первая задача).
- Produces: модульные хелперы `_duration_seconds(value: str | int | float) -> float` и `_assert_cheap_idle_healthcheck(healthcheck: dict, *, probe: list[str]) -> None` в `tests/test_infrastructure_policy.py`. Task 2 их не использует, но обязана их не сломать.

- [ ] **Step 1: Переписать тест-страж на гибридную форму**

В `tests/test_infrastructure_policy.py` **перед** функцией `test_compose_defines_isolated_test_profile_services` добавить два хелпера:

```python
def _duration_seconds(value: str | int | float) -> float:
    """Переводит длительность compose ('2s', '300s', '1m30s') в секунды."""
    if isinstance(value, (int, float)):
        return float(value)
    units = {"us": 1e-6, "ms": 1e-3, "h": 3600.0, "m": 60.0, "s": 1.0}
    parts = re.findall(r"(\d+(?:\.\d+)?)(us|ms|h|m|s)", value)
    if not parts:
        raise ValueError(f"не удалось разобрать длительность: {value!r}")
    return sum(float(number) * units[unit] for number, unit in parts)


def _assert_cheap_idle_healthcheck(healthcheck: dict, *, probe: list[str]) -> None:
    """Проба и retries фиксированы точно, тайминги — инвариантами.

    Инвариант: дорогая проба не крутится в установившемся режиме (interval >= 30s),
    но старт остаётся быстрым — внутри start_period пробы идут часто (start_interval <= 5s).
    Тюнинг конкретных чисел не должен ломать тест, поэтому равенства на них нет.
    """
    assert healthcheck["test"] == probe
    assert healthcheck["retries"] == 3
    assert _duration_seconds(healthcheck["interval"]) >= 30
    assert _duration_seconds(healthcheck["start_interval"]) <= 5
    assert _duration_seconds(healthcheck["start_period"]) > 0
```

Добавить `import re` в блок stdlib-импортов в начале файла (после `from pathlib import Path`, по алфавиту — между `Path` и `socket`: строки 5–6).

Заменить в `test_compose_defines_isolated_test_profile_services` два блока точного сравнения `healthcheck` на вызовы хелпера. Итоговое тело функции:

```python
def test_compose_defines_isolated_test_profile_services() -> None:
    root = Path(__file__).parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))

    paradedb = compose["services"]["paradedb-test"]
    assert paradedb["profiles"] == ["test"]
    assert paradedb["environment"] == {
        "POSTGRES_USER": "reviewer_test",
        "POSTGRES_PASSWORD": "reviewer_test",
        "POSTGRES_DB": "reviewer_test",
    }
    assert paradedb["ports"] == ["127.0.0.1:55433:5432"]
    _assert_cheap_idle_healthcheck(
        paradedb["healthcheck"],
        probe=["CMD-SHELL", "pg_isready -U reviewer_test -d reviewer_test"],
    )

    neo4j = compose["services"]["neo4j-test"]
    assert neo4j["profiles"] == ["test"]
    assert neo4j["environment"] == {"NEO4J_AUTH": "neo4j/reviewer_test_pass"}
    assert neo4j["ports"] == ["127.0.0.1:17474:7474", "127.0.0.1:17687:7687"]
    _assert_cheap_idle_healthcheck(
        neo4j["healthcheck"],
        probe=[
            "CMD-SHELL",
            "cypher-shell -u neo4j -p reviewer_test_pass 'RETURN 1'",
        ],
    )
```

- [ ] **Step 2: Прогнать тест — он должен упасть**

Run: `.venv/bin/pytest tests/test_infrastructure_policy.py::test_compose_defines_isolated_test_profile_services -q`

Expected: FAIL — `assert 30 == 3` на `healthcheck["retries"]` для `paradedb-test` (в compose ещё старые значения). Если бы `retries` совпал, следующим упал бы `KeyError: 'start_interval'`.

- [ ] **Step 3: Обновить healthcheck обоих test-сервисов в compose**

В `docker-compose.yml` заменить блок healthcheck у `paradedb-test` (строки 32–36) на:

```yaml
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U reviewer_test -d reviewer_test"]
      start_period: 60s
      start_interval: 2s
      interval: 30s
      timeout: 2s
      retries: 3
```

и блок healthcheck у `neo4j-test` (строки 44–48) на:

```yaml
    # cypher-shell поднимает JVM (~2.5 с CPU за вызов): в простое проба идёт раз в 5 минут,
    # частые пробы — только в фазе старта (start_period + start_interval).
    healthcheck:
      test: ["CMD-SHELL", "cypher-shell -u neo4j -p reviewer_test_pass 'RETURN 1'"]
      start_period: 60s
      start_interval: 2s
      interval: 300s
      timeout: 3s
      retries: 3
```

- [ ] **Step 4: Прогнать тест — он должен пройти**

Run: `.venv/bin/pytest tests/test_infrastructure_policy.py -q`

Expected: PASS, все тесты модуля зелёные (в том числе `test_compose_pins_only_test_service_images_by_digest`, `test_compose_test_services_use_only_disposable_tmpfs`, `test_compose_documents_only_safe_test_profile_teardown` — их правка не затрагивала).

- [ ] **Step 5: Проверить синтаксис compose**

Run: `docker compose --profile test config --quiet`

Expected: пустой вывод, код возврата 0 (YAML валиден, ключи healthcheck распознаны).

- [ ] **Step 6: Линт**

Run: `.venv/bin/ruff check tests/test_infrastructure_policy.py`

Expected: `All checks passed!`

- [ ] **Step 7: Коммит**

```bash
git add docker-compose.yml tests/test_infrastructure_policy.py
git commit -m "perf(infra): развести стартовые и фоновые пробы healthcheck test-профиля (PRI-216)"
```

---

## Task 2: Комментарий о требовании Docker ≥ 25.0 и тест-страж на него

**Files:**
- Modify: `docker-compose.yml:17-22` (шапка блока test-профиля)
- Modify: `tests/test_infrastructure_policy.py` (новый тест после `test_compose_documents_only_safe_test_profile_teardown`, строка 285)
- Test: `tests/test_infrastructure_policy.py`

**Interfaces:**
- Consumes: изменённый в Task 1 `docker-compose.yml` (ключи `start_interval` уже на месте).
- Produces: тест `test_compose_documents_start_interval_docker_requirement() -> None`.

- [ ] **Step 1: Написать падающий тест**

В `tests/test_infrastructure_policy.py` **после** функции `test_compose_documents_only_safe_test_profile_teardown` (заканчивается на строке 284) добавить:

```python
def test_compose_documents_start_interval_docker_requirement() -> None:
    root = Path(__file__).parents[1]
    compose_text = (root / "docker-compose.yml").read_text(encoding="utf-8")
    normalized = "\n".join(line.lstrip() for line in compose_text.splitlines())

    assert (
        "# start_interval требует Docker Engine >= 25.0 / API >= 1.44: на старых движках\n"
        "# ключ игнорируется, стартовые пробы идут с редким interval и `up -d --wait`\n"
        "# ждёт минутами вместо секунд."
    ) in normalized
```

- [ ] **Step 2: Прогнать тест — он должен упасть**

Run: `.venv/bin/pytest tests/test_infrastructure_policy.py::test_compose_documents_start_interval_docker_requirement -q`

Expected: FAIL — `assert '# start_interval требует Docker Engine >= 25.0 ...' in normalized` (комментария в compose ещё нет).

- [ ] **Step 3: Добавить комментарий в шапку блока test-профиля**

В `docker-compose.yml` в блоке-шапке test-профиля (строки 17–22) дописать три строки перед закрывающей линией `# ====`. Итоговая шапка:

```yaml
  # ============================================================================
  # ВАЖНО: test-profile использует общий Compose project с dev-сервисами.
  # Безопасное удаление: docker compose --profile test rm -sfv paradedb-test neo4j-test
  # НИКОГДА не используйте `docker compose --profile test down -v`: это остановит
  # dev-сервисы и удалит production named volumes.
  #
  # start_interval требует Docker Engine >= 25.0 / API >= 1.44: на старых движках
  # ключ игнорируется, стартовые пробы идут с редким interval и `up -d --wait`
  # ждёт минутами вместо секунд.
  # ============================================================================
```

- [ ] **Step 4: Прогнать весь модуль тестов**

Run: `.venv/bin/pytest tests/test_infrastructure_policy.py -q`

Expected: PASS. Особое внимание — `test_compose_documents_only_safe_test_profile_teardown` остаётся зелёным: он проверяет вхождение своих двух строк подряд, а новые строки добавлены после них, через строку-разделитель `#`.

- [ ] **Step 5: Полный unit-прогон и линт**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`

Expected: тесты зелёные; ruff по изменённому файлу без замечаний (репозиторий в целом может иметь фоновые замечания — важно, чтобы их не прибавилось в `tests/test_infrastructure_policy.py`).

- [ ] **Step 6: Коммит**

```bash
git add docker-compose.yml tests/test_infrastructure_policy.py
git commit -m "docs(infra): закрепить требование Docker >= 25.0 для start_interval (PRI-216)"
```

---

## Task 3: Приёмка на живом стенде (ручные замеры)

Автотеста на CPU нет по решению из спеки — замер шаткий на загруженной машине. Задача выполняется вручную, её результат — цифры в описании PR.

**Files:**
- Modify: ничего (только прогоны и снятие метрик)

**Interfaces:**
- Consumes: `docker-compose.yml` после Task 1 и Task 2.
- Produces: три числа для описания PR — CPU `neo4j-test` в простое, CPU `paradedb-test` в простое, время холодного старта стенда.

- [ ] **Step 1: Пересоздать тестовые сервисы и замерить холодный старт**

Новые healthcheck применяются только к пересозданным контейнерам.

```bash
docker compose --profile test rm -sfv paradedb-test neo4j-test
time docker compose --profile test up -d --wait paradedb-test neo4j-test
```

Expected: оба сервиса становятся healthy; `real` — порядка прежнего времени старта (десятки секунд, определяется стартом Neo4j, а не частотой проб). Кратный рост означал бы, что `start_interval` не применился — проверить `docker version --format '{{.Server.APIVersion}}'` (нужно ≥ 1.44).

- [ ] **Step 2: Замерить CPU в простое**

Дать стенду постоять в простое (тесты не запускать), затем выполнить скрипт ниже. Агентам: в Bash-инструменте foreground `sleep` заблокирован — запускать с `run_in_background: true` и прочитать файл вывода по завершении.

```bash
for c in neo4j-test paradedb-test; do
  cid=$(docker compose ps -q $c)
  a=$(docker exec "$cid" awk '/^usage_usec/{print $2}' /sys/fs/cgroup/cpu.stat)
  sleep 20
  b=$(docker exec "$cid" awk '/^usage_usec/{print $2}' /sys/fs/cgroup/cpu.stat)
  echo "$c: $(( (b-a)/1000 )) ms CPU за 20 с = $(( (b-a)/200000 ))% ядра"
done
```

Expected: `neo4j-test` < 1% ядра (baseline был 51%), `paradedb-test` < 1% (baseline 1.4%). Критерий приёмки задачи — < 5%.

- [ ] **Step 3: Прогнать integration-тесты**

Run: `.venv/bin/pytest -q -m integration`

Expected: результат не хуже, чем до правки (пайплайну также нужен `VOYAGE_API_KEY`; тесты, падавшие по внешним причинам до правки, падают так же — правка на них не влияет).

- [ ] **Step 4: Зафиксировать цифры**

Записать в черновик описания PR: baseline (`neo4j-test` 51% / `paradedb-test` 1.4%), значения после правки из Step 2, время холодного старта из Step 1, версию Docker.

Коммита в этой задаче нет — она ничего не меняет в репозитории.

---

## Готовность к PR

После Task 3: ветка `feature/pri-216` содержит спеку, бриф и два коммита правок. PR открывается в `dev` (защищена, прямой пуш запрещён), в описание кладутся цифры замеров. После создания PR — предложить закрыть задачу скиллом `/reviewer_finish-task`.
