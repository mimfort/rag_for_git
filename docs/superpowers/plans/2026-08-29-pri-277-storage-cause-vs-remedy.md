# PRI-277 — причина сбоя хранилища против уместности лекарства: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Научить систему отличать неверные креды и несуществующую БД от остановленных контейнеров и перестать советовать `reviewer start` там, где он уже выполнен.

**Architecture:** Новая функция `classify_storage_failure` в `reviewer/storage_health.py` становится единственным источником двух машинных решений — класса причины и уместности совета. Её зовут оба канала: MCP-путь (`reviewer/mcp/task_context.py`, где вычисление вердикта переезжает из старта `build_task_context` в `except`-ветку `_safe`, где исключение наконец доступно) и CLI (`reviewer check`). Русские формулировки остаются у каждого канала свои: у них разные адресаты.

**Tech Stack:** Python 3.11+, psycopg 3, neo4j-driver, Click, pytest. Тесты — только unit, без Postgres, Neo4j и сети.

**Spec:** `docs/superpowers/specs/2026-08-29-pri-277-storage-cause-vs-remedy-design.md`

## Global Constraints

- Язык проекта — русский: комментарии, докстринги, сообщения CLI. Новый код пишется в этом стиле.
- Коммиты — Conventional Commits на русском, **без self-attribution**: никаких `Co-Authored-By`, никаких упоминаний Claude.
- Тесты запускаются как `.venv/bin/pytest -q`. Baseline зелёный; любое падение — регрессия, а не «известное».
- Unit-тестам запрещены внешние и localhost-сокеты. Ни одна новая проверка не поднимает Postgres и Neo4j.
- `is_storage_unavailable` и значение `cause` не меняются: `storage_unavailable` остаётся верным во всех трёх случаях (критерий приёмки 4).
- Ветка `feat/pri-277-storage-cause-vs-remedy` уже создана от `origin/dev` и активна; спека закоммичена в `35a00cd`.
- `git push`, создание PR и любая запись в доску требуют отдельного подтверждения пользователя. План их не выполняет.
- В дереве есть незакоммиченные правки `eval/solve_task_metrics_history.jsonl` и `eval/solve_task_metrics_report.md`, принадлежащие другой задаче. **Не добавлять их в коммиты**: каждый `git add` в этом плане перечисляет файлы поимённо.

---

### Task 1: Классификатор причины в `storage_health`

Отдельная задача, потому что ревьюер может осмысленно принять или отвергнуть сам классификатор — его паттерны, вымарывание и контракт — независимо от того, как им потом пользуются каналы.

**Files:**
- Modify: `reviewer/storage_health.py` (докстринг модуля 1-14; добавления после `storage_remedy`, строка 64)
- Test: `tests/test_storage_health.py`

**Interfaces:**
- Consumes: существующие `is_storage_unavailable`, `storage_remedy`, `REMEDY_START` того же модуля.
- Produces:
  - `StorageDiagnosis` — frozen dataclass с полями `detail: str | None`, `remedy: str | None`, `redacted: str | None`
  - `classify_storage_failure(exc: BaseException, *endpoints: str) -> StorageDiagnosis`
  - константы `DETAIL_AUTH_FAILED = "auth_failed"`, `DETAIL_MISSING_DATABASE = "missing_database"`

- [ ] **Step 1: Написать падающие тесты классификатора**

Дописать в конец `tests/test_storage_health.py`:

```python
# ---------------------------------------------------------------------------
# Тесты PRI-277: класс причины отдельно от уместности лекарства
# ---------------------------------------------------------------------------

_LOCAL_DSN = "postgresql://reviewer:s3cretpw@127.0.0.1:5433/reviewer"
_REMOTE_DSN = "postgresql://reviewer:s3cretpw@db.example.com:5432/prod"


def test_auth_failure_is_classified_and_loses_remedy():
    """Контейнеры живы, пароль неверен — reviewer start уже выполнен и не поможет."""
    exc = psycopg.OperationalError(
        'connection failed: FATAL:  password authentication failed for user "reviewer"')
    diagnosis = sh.classify_storage_failure(exc, _LOCAL_DSN)
    assert diagnosis.detail == sh.DETAIL_AUTH_FAILED
    assert diagnosis.remedy is None
    assert diagnosis.redacted is None


def test_missing_database_is_classified_and_loses_remedy():
    exc = psycopg.OperationalError(
        'connection failed: FATAL:  database "nosuchdb" does not exist')
    diagnosis = sh.classify_storage_failure(exc, _LOCAL_DSN)
    assert diagnosis.detail == sh.DETAIL_MISSING_DATABASE
    assert diagnosis.remedy is None
    assert diagnosis.redacted is None


def test_unrecognised_local_failure_keeps_remedy_and_redacts():
    """Нераспознанный сбой ведёт себя как прежде, но перестаёт быть немым."""
    exc = psycopg.OperationalError(
        "connection to server at 127.0.0.1, port 5433 failed: Connection refused")
    diagnosis = sh.classify_storage_failure(exc, _LOCAL_DSN)
    assert diagnosis.detail is None
    assert diagnosis.remedy == sh.REMEDY_START
    assert "[REDACTED]" in diagnosis.redacted


def test_unrecognised_remote_failure_has_no_remedy():
    """Удалённому деплою локальный docker-стек не помогает и здесь."""
    exc = psycopg.OperationalError("connection to server failed: Connection refused")
    diagnosis = sh.classify_storage_failure(exc, _REMOTE_DSN)
    assert diagnosis.detail is None
    assert diagnosis.remedy is None


def test_non_storage_exception_is_empty_verdict():
    """AuthError neo4j — неверные креды, не лежачее хранилище: совета быть не должно."""
    diagnosis = sh.classify_storage_failure(AuthError("unauthorized"), "bolt://localhost:7687")
    assert (diagnosis.detail, diagnosis.remedy, diagnosis.redacted) == (None, None, None)


def test_redacted_never_carries_dsn_literals():
    """Критерий 3: ни пароль, ни хост, ни имя пользователя, ни база в выдачу не попадают."""
    exc = psycopg.OperationalError(
        "connection to server at db.example.com, port 5432 failed: "
        'FATAL:  odd failure for user "reviewer" in database "prod" (password s3cretpw)')
    diagnosis = sh.classify_storage_failure(exc, _REMOTE_DSN)
    for secret in ("s3cretpw", "db.example.com", "reviewer", "prod", "5432"):
        assert secret not in repr(diagnosis), secret


def test_short_literals_do_not_mangle_the_message():
    """Односимвольный пароль не должен выесть все свои буквы из сообщения.

    Литерал короче трёх символов не вымарывается: замена превратила бы текст в
    кашу, а замер PRI-269 показал, что пароль в текст libpq и не попадает.
    """
    exc = psycopg.OperationalError("connection to server at 127.0.0.1 failed")
    diagnosis = sh.classify_storage_failure(exc, "postgresql://u:p@127.0.0.1:5433/reviewer")
    assert "connection to server" in diagnosis.redacted


def test_auth_pattern_wins_over_missing_database():
    """Порядок паттернов важен: сообщение может нести оба маркера сразу."""
    exc = psycopg.OperationalError(
        'FATAL:  password authentication failed for user "reviewer"\n'
        'FATAL:  database "reviewer" does not exist')
    assert sh.classify_storage_failure(exc, _LOCAL_DSN).detail == sh.DETAIL_AUTH_FAILED
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/test_storage_health.py -q`
Expected: FAIL — `AttributeError: module 'reviewer.storage_health' has no attribute 'classify_storage_failure'`

- [ ] **Step 3: Поправить докстринг модуля**

В `reviewer/storage_health.py` заменить абзац (строки 8-13), начинающийся с «Решение принимается по ТИПУ исключения», на:

```
Класс недоступности решается по ТИПУ исключения (`is_storage_unavailable`), а
уточнение причины внутри этого класса — по ТЕКСТУ (`classify_storage_failure`).
Разделение не косметическое: при сбое на этапе установления соединения libpq не
возвращает результат, поэтому SQLSTATE пуст и ветвиться по коду ошибки нельзя
(замер PRI-269). Прежний довод «в тексте живёт DSN с паролем» этим же замером
опровергнут для данного класса ошибок: пароль в `str(exc)` не попадает, а хост,
порт, имя пользователя и имя базы — попадают, и потому вымарываются. Наружу
текст выходит только там, где класс назвать не удалось.
```

- [ ] **Step 4: Реализовать классификатор**

В `reviewer/storage_health.py` добавить `from dataclasses import dataclass` к импортам, затем дописать после `storage_remedy` (после строки 64):

```python
DETAIL_AUTH_FAILED = "auth_failed"
DETAIL_MISSING_DATABASE = "missing_database"

# Обрезка отрывка: диагностика не должна раздувать payload MCP-тула.
_MAX_REDACTED_CHARS = 200
# Литерал короче этого не вымарывается: замена односимвольного пароля выела бы
# из сообщения все вхождения одной буквы и сделала бы его нечитаемым.
_MIN_SECRET_CHARS = 3

# Порядок значим: сообщение может нести оба маркера, и auth-сбой конкретнее
# (тот же класс граблей, что у ConnectTimeout в config/fetch_errors.py).
_DETAIL_PATTERNS = (
    (DETAIL_AUTH_FAILED, re.compile(r"password authentication failed", re.IGNORECASE)),
    (DETAIL_MISSING_DATABASE, re.compile(r"database\b.*\bdoes not exist", re.IGNORECASE)),
)


@dataclass(frozen=True)
class StorageDiagnosis:
    """Вердикт по одному сбою: класс причины, лекарство и безопасный отрывок.

    `detail` — закрытая метка либо None, если класс назвать не удалось.
    `remedy` — команда-лекарство, только когда она действительно применима.
    `redacted` — вымаранный отрывок текста; заполнен лишь при пустом `detail`.
    """

    detail: str | None
    remedy: str | None
    redacted: str | None


def _endpoint_secrets(endpoint: str) -> set[str]:
    """Чувствительные литералы одного эндпоинта: хост, порт, пользователь, пароль, база."""
    values: set[str] = set()
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        parts = None
    if parts is not None:
        for value in (parts.hostname, parts.username, parts.password,
                      parts.path.lstrip("/")):
            if value:
                values.add(str(value))
        try:
            if parts.port:
                values.add(str(parts.port))
        except ValueError:
            pass  # порт не число — вымарывать нечего, остальные поля уже собраны
    for keyword in ("host", "user", "password", "dbname", "port"):
        values.update(re.findall(rf"\b{keyword}=([^\s]+)", endpoint))
    return {value for value in values if len(value) >= _MIN_SECRET_CHARS}


def _redact(text: str, *endpoints: str) -> str:
    """Заменить литералы эндпоинтов на [REDACTED], схлопнуть пробелы и обрезать.

    Длинные значения заменяются первыми: иначе значение-префикс оставило бы хвост
    более длинного. Обрезка идёт ПОСЛЕ вымарывания — иначе литерал, попавший на
    границу, уцелел бы наполовину.
    """
    secrets: set[str] = set()
    for endpoint in endpoints:
        if endpoint:
            secrets |= _endpoint_secrets(endpoint)
    rendered = text
    for secret in sorted(secrets, key=len, reverse=True):
        rendered = rendered.replace(secret, "[REDACTED]")
    return " ".join(rendered.split())[:_MAX_REDACTED_CHARS]


def classify_storage_failure(exc: BaseException, *endpoints: str) -> StorageDiagnosis:
    """Класс причины недоступности и уместность совета `reviewer start`.

    Единственный источник обоих машинных решений: и MCP (`mcp/task_context.py`),
    и CLI (`reviewer check`) зовут её, поэтому разойтись им нечем — раньше каждый
    решал сам и оба ошибались одинаково (PRI-277).

    Пустой вердикт при не-storage исключении не заглушка, а рабочая ветка: ею
    чинится Neo4j `AuthError`, который в `is_storage_unavailable` не входит.
    """
    if not is_storage_unavailable(exc):
        return StorageDiagnosis(None, None, None)
    text = str(exc)
    for detail, pattern in _DETAIL_PATTERNS:
        if pattern.search(text):
            # Хранилище ответило отказом, значит контейнеры подняты и лекарство
            # неприменимо; класс уже назван, поэтому текст наружу не нужен.
            return StorageDiagnosis(detail, None, None)
    return StorageDiagnosis(None, storage_remedy(*endpoints), _redact(text, *endpoints))
```

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `.venv/bin/pytest tests/test_storage_health.py -q`
Expected: PASS, все тесты файла, включая восемь прежних.

- [ ] **Step 6: Прогнать линт и полный unit-набор**

Run: `.venv/bin/ruff check reviewer/storage_health.py tests/test_storage_health.py && .venv/bin/pytest -q`
Expected: ruff чист; pytest зелёный. Ни один прежний тест не падает — `is_storage_unavailable` и `storage_remedy` не тронуты.

- [ ] **Step 7: Коммит**

```bash
git add reviewer/storage_health.py tests/test_storage_health.py
git commit -m 'feat(storage-health): классификатор причины сбоя хранилища

classify_storage_failure отделяет класс причины (auth_failed,
missing_database) от уместности совета reviewer start. SQLSTATE при сбое
установления соединения пуст, поэтому решение по тексту; текст выходит
наружу только для нераспознанного случая и только вымаранным.'
```

---

### Task 2: Вердикт в `task_context` и пятый ключ записи

Отдельная задача: здесь меняется наблюдаемый payload публичного MCP-тула — ревьюер может принять классификатор, но потребовать другой формы записи.

**Files:**
- Modify: `reviewer/mcp/task_context.py` (импорты 17-19; `gap` 32-39; `_StorageState` 42-52; `_storage_gap` 55-62; `_safe` 78-85; `_remedy` 116-129; `build_task_context` 138)
- Modify: `CLAUDE.md` (утверждение «`gap()` теперь всегда 4-ключевой»)
- Test: `tests/mcp/test_prepare_task_context.py`

**Interfaces:**
- Consumes: `StorageDiagnosis`, `classify_storage_failure`, `DETAIL_AUTH_FAILED`, `DETAIL_MISSING_DATABASE` из Task 1.
- Produces: запись `gap()` с пятью ключами `{"section", "reason", "cause", "cause_detail", "remedy"}`.

- [ ] **Step 1: Написать падающие тесты**

Дописать в конец `tests/mcp/test_prepare_task_context.py`:

```python
# ---------------------------------------------------------------------------
# Тесты PRI-277: класс причины отдельно от уместности лекарства
# ---------------------------------------------------------------------------

def _preflight_gap(exc):
    """Запись gaps секции preflight при заданном сбое источника."""
    payload = task_context.build_task_context(
        FakeDeps(preflight=exc), repo="o/n", key="PRI-277", branch="dev",
        warm_board=False)
    return next(g for g in payload["gaps"] if g["section"] == "preflight")


def test_auth_failure_is_named_and_loses_remedy():
    """Критерий 1 и 2: неверный пароль отличим и не получает совета."""
    entry = _preflight_gap(psycopg.OperationalError(
        'FATAL:  password authentication failed for user "reviewer"'))
    assert entry["cause"] == "storage_unavailable"      # критерий 4: не переклассифицируем
    assert entry["cause_detail"] == "auth_failed"
    assert entry["remedy"] is None
    assert "учётные данные" in entry["reason"]


def test_missing_database_is_named_and_loses_remedy():
    entry = _preflight_gap(psycopg.OperationalError(
        'FATAL:  database "nosuchdb" does not exist'))
    assert entry["cause"] == "storage_unavailable"
    assert entry["cause_detail"] == "missing_database"
    assert entry["remedy"] is None


def test_stopped_containers_still_get_remedy_and_no_detail():
    """Третий случай остаётся отличимым от первых двух с другой стороны."""
    entry = _preflight_gap(psycopg.OperationalError("Connection refused"))
    assert entry["cause_detail"] is None
    assert entry["remedy"] == "reviewer start"


def test_unrecognised_failure_carries_redacted_excerpt():
    """Нераспознанный сбой перестаёт быть немым, но секретов не несёт."""
    entry = _preflight_gap(psycopg.OperationalError(
        "connection to server at 127.0.0.1, port 5433 failed: no space left on device"))
    assert "no space left on device" in entry["reason"]
    assert "127.0.0.1" not in entry["reason"]


def test_skipped_sections_reuse_the_same_verdict():
    """Все записи одного прогона согласованы: вердикт считается один раз."""
    payload = task_context.build_task_context(
        FakeDeps(preflight=psycopg.OperationalError(
            'FATAL:  password authentication failed for user "reviewer"')),
        repo="o/n", key="PRI-277", branch="dev", warm_board=False)
    assert all(g["cause_detail"] == "auth_failed" for g in payload["gaps"])
    skipped = next(g for g in payload["gaps"] if g["section"] == "code")
    assert skipped["reason"].startswith("пропущено: ")
    assert skipped["remedy"] is None


def test_non_storage_gap_has_empty_detail():
    """Аддитивность: запись не про хранилище получает пятый ключ пустым."""
    payload = task_context.build_task_context(
        FakeDeps(subsystems=RuntimeError("нет сводок")), repo="o/n",
        key="PRI-277", branch="dev", warm_board=False)
    entry = next(g for g in payload["gaps"] if g["section"] == "subsystems")
    assert entry["cause"] == "unknown"
    assert entry["cause_detail"] is None
```

В том же файле обновить существующий guard формы записи (строка 381) — с четырёх ключей на пять:

```python
def test_existing_gaps_keep_section_and_reason():
    """Расширение аддитивно: прежние ключи записи на месте, добавлен cause_detail."""
    payload = task_context.build_task_context(
        FakeDeps(subsystems=RuntimeError("нет сводок")), repo="o/n",
        key="PRI-268", branch="dev", warm_board=False)
    entry = next(g for g in payload["gaps"] if g["section"] == "subsystems")
    assert entry["reason"] == "сводки подсистем недоступны"
    assert set(entry) == {"section", "reason", "cause", "cause_detail", "remedy"}
```

**Два теста-якоря на строках 314 и 353 не трогать.** Они требуют `remedy == "reviewer start"` и `remedy is None` для `OperationalError("connection refused")`; текст не совпадает ни с одним паттерном, поэтому случай идёт прежней веткой и оба остаются зелёными по построению. Если хоть один покраснел — классификатор захватил лишнее.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/mcp/test_prepare_task_context.py -q`
Expected: FAIL — `KeyError: 'cause_detail'` в новых тестах и в обновлённом guard.

- [ ] **Step 3: Реализовать изменения в `task_context.py`**

Импорты (строки 17-19) — `storage_remedy` больше не нужен, добавляются четыре имени:

```python
from reviewer.storage_health import (
    CAUSE_STORAGE_UNAVAILABLE, CAUSE_UNKNOWN, DETAIL_AUTH_FAILED,
    DETAIL_MISSING_DATABASE, StorageDiagnosis, classify_storage_failure,
    is_storage_unavailable,
)
```

Константы причин (строки 28-29) — `SKIPPED_REASON` строится из `STORAGE_REASON`, а не повторяет его текст: на этом строится подстановка класса:

```python
STORAGE_REASON = "хранилище не отвечает"
SKIPPED_REASON = f"пропущено: {STORAGE_REASON}"

# Формулировки распознанных классов. Живут здесь, а не в storage_health: их
# читает LLM и вставляет в бриф, а у CLI свой адресат и свои строки.
DETAIL_REASONS = {
    DETAIL_AUTH_FAILED: "хранилище отвергло учётные данные",
    DETAIL_MISSING_DATABASE: "базы данных не существует",
}
```

`gap` (строки 32-39):

```python
def gap(section: str, reason: str, *, cause: str = CAUSE_UNKNOWN,
        cause_detail: str | None = None, remedy: str | None = None) -> dict:
    """Структурная запись о пробеле: секция, причина и её класс, без секретов.

    `cause` — машиночитаемый класс причины: скилл и тесты ветвятся по нему, а не
    по прозе `reason`. `cause_detail` — уточнение внутри класса, когда оно
    установлено (PRI-277). `remedy` — команда-лекарство, когда она есть и уместна.
    """
    return {"section": section, "reason": reason, "cause": cause,
            "cause_detail": cause_detail, "remedy": remedy}
```

`_StorageState` (строки 42-52):

```python
class _StorageState:
    """Взведён ли флаг «хранилище не отвечает», и каков вердикт по первому сбою.

    Флаг живёт на один вызов `build_task_context`: первая же недоступность
    хранилища отменяет остальные store-секции, иначе каждая добавила бы к
    времени ответа собственный таймаут пула (30 с × 8 секций).

    Вердикт считается лениво, при первом реальном сбое: до PRI-277 лекарство
    фиксировалось на старте, когда исключения ещё нет, и потому не могло
    зависеть от причины.
    """

    def __init__(self, endpoints: tuple[str, ...]) -> None:
        self.endpoints = endpoints
        self.diagnosis: StorageDiagnosis | None = None
        self.down = False

    def diagnose(self, exc: BaseException) -> StorageDiagnosis:
        """Вердикт по первому сбою; последующие пропуски переиспользуют его."""
        if self.diagnosis is None:
            self.diagnosis = classify_storage_failure(exc, *self.endpoints)
        return self.diagnosis
```

`_storage_gap` (строки 55-62):

```python
def _storage_gap(payload: dict, section: str, reason: str, state: _StorageState) -> None:
    """Записать в gaps пробел, вызванный недоступностью хранилища.

    Общая точка для трёх мест, различающихся только `reason`: skip- и except-
    ветки `_safe`, а также `elif warm_board and not board` в build_task_context.
    """
    diagnosis = state.diagnosis
    detail = diagnosis.detail if diagnosis is not None else None
    remedy = diagnosis.remedy if diagnosis is not None else None
    payload["gaps"].append(gap(section, _reason_with_detail(reason, diagnosis),
                               cause=CAUSE_STORAGE_UNAVAILABLE,
                               cause_detail=detail, remedy=remedy))


def _reason_with_detail(base: str, diagnosis: StorageDiagnosis | None) -> str:
    """Причина с учётом вердикта: класс замещает общую формулировку, отрывок дополняет.

    Подстановка работает и для `SKIPPED_REASON`, потому что тот собран из
    `STORAGE_REASON`: «пропущено: хранилище не отвечает» превращается в
    «пропущено: базы данных не существует», а не теряет отметку о пропуске.
    """
    if diagnosis is None:
        return base
    if diagnosis.detail:
        return base.replace(STORAGE_REASON, DETAIL_REASONS[diagnosis.detail])
    if diagnosis.redacted:
        return f"{base}: {diagnosis.redacted}"
    return base
```

`_safe` — в ветке `is_storage_unavailable` (строки 80-82) вердикт считается ДО записи:

```python
        if is_storage_unavailable(exc):
            state.down = True
            state.diagnose(exc)
            _storage_gap(payload, section, STORAGE_REASON, state)
```

`_remedy` (строки 116-129) заменяется на `_endpoints` с теми же `getattr` и fail-soft:

```python
def _endpoints(deps) -> tuple[str, ...]:
    """Эндпоинты хранилищ, если провайдер умеет их назвать.

    Читается через `getattr`, как `augment_gaps`: модуль намеренно не знает про
    Settings, а старый провайдер без этого метода обязан продолжать работать.
    """
    getter = getattr(deps, "storage_endpoints", None)
    if not callable(getter):
        return ()
    try:
        return tuple(getter() or ())
    except Exception:  # noqa: BLE001 — источник эндпоинтов недоступен, это не повод падать
        log.warning("prepare_task_context: эндпоинты хранилищ недоступны", exc_info=True)
        return ()
```

`build_task_context` (строка 138):

```python
    state = _StorageState(_endpoints(deps))
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `.venv/bin/pytest tests/mcp/test_prepare_task_context.py -q`
Expected: PASS, включая оба нетронутых якоря (`test_storage_failure_names_cause_and_remedy`, `test_remote_deploy_gets_cause_without_remedy`) и `test_deps_without_storage_endpoints_still_work`.

- [ ] **Step 5: Обновить CLAUDE.md**

В разделе «Неочевидные факты», в абзаце про PRI-268, заменить фразу «**`gap()` теперь всегда 4-ключевой** (`section`, `reason`, `cause`, `remedy`)» на:

```
**`gap()` теперь всегда 5-ключевой** (`section`, `reason`, `cause`,
`cause_detail`, `remedy`)
```

и дописать в конце того же абзаца:

```
Пятый ключ `cause_detail` добавлен в PRI-277 и уточняет причину ВНУТРИ класса
`storage_unavailable`: `auth_failed` | `missing_database` | `null`. Сам `cause`
при этом не меняется намеренно — шаг 0a скилла `solve-task` ищет равенство
`storage_unavailable`, и уточнение самого `cause` тихо перестало бы показывать
пользователю auth-сбой вовсе. Решение принимает `classify_storage_failure`
(`reviewer/storage_health.py`) — единственный источник и класса, и уместности
совета `reviewer start`, общий для MCP и `reviewer check`. Ограничение, которое
надо знать: сообщения libpq локализуются по `lc_messages` сервера, поэтому на
не-английской локали паттерны не совпадут и случай уедет в нераспознанную ветку
с вымаранным отрывком — деградация безопасная, но различение не гарантировано.
```

- [ ] **Step 6: Прогнать линт и полный unit-набор**

Run: `.venv/bin/ruff check reviewer/mcp/task_context.py tests/mcp/test_prepare_task_context.py && .venv/bin/pytest -q`
Expected: ruff чист (в частности, нет неиспользуемого импорта `storage_remedy`); pytest зелёный.

- [ ] **Step 7: Коммит**

```bash
git add reviewer/mcp/task_context.py tests/mcp/test_prepare_task_context.py CLAUDE.md
git commit -m 'feat(mcp): cause_detail в записи gaps и вердикт по факту сбоя

Вычисление лекарства переехало из старта build_task_context в except-ветку
_safe, где исключение наконец доступно: до этого remedy фиксировался до
первого обращения к хранилищу и зависеть от причины не мог. Запись gaps
получила пятый ключ cause_detail; cause остаётся storage_unavailable.'
```

---

### Task 3: `reviewer check` перестаёт врать про лекарство

Отдельная задача: второй канал того же диагноза, со своими сообщениями и своими тестами. Ревьюер может принять MCP-часть и отдельно спорить о формулировках CLI.

**Files:**
- Modify: `reviewer/entrypoints/cli.py` (импорт 69; `check` 845, 876-908)
- Test: `tests/entrypoints/test_cli.py`

**Interfaces:**
- Consumes: `classify_storage_failure`, `DETAIL_AUTH_FAILED`, `DETAIL_MISSING_DATABASE` из Task 1.
- Produces: ничего для последующих задач — это конечный потребитель.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `tests/entrypoints/test_cli.py` после `test_check_fails_on_neo4j_error`:

```python
def _check_settings():
    """Settings для check с локальными эндпоинтами (иначе совет не положен вовсе)."""
    s = MagicMock()
    s.voyage_api_key = "key"
    s.github_token = "key"
    s.pg_dsn = "postgresql://reviewer:s3cretpw@127.0.0.1:5433/reviewer"
    s.pg_pool_min_size = 1
    s.pg_pool_max_size = 4
    s.neo4j_uri = "bolt://localhost:7687"
    s.neo4j_user = "u"
    s.neo4j_password = "p"
    return s


@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_wrong_password_does_not_advise_start(
    mock_settings_cls, mock_chunk_cls, mock_graph_cls, runner, recwarn
):
    """Дефект Д-6: контейнеры живы, совет reviewer start бессмыслен."""
    mock_settings_cls.return_value = _check_settings()
    mock_chunk_cls.side_effect = psycopg.OperationalError(
        'connection failed: FATAL:  password authentication failed for user "reviewer"')
    mock_graph_cls.return_value = MagicMock()

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 1
    assert "reviewer start" not in result.output
    assert "учётные данные" in result.output
    _assert_no_socket_warnings(recwarn)


@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_missing_database_is_not_reported_as_missing_schema(
    mock_settings_cls, mock_chunk_cls, mock_graph_cls, runner, recwarn
):
    """«does not exist» несут оба случая; несуществующая БД не лечится reviewer index."""
    mock_settings_cls.return_value = _check_settings()
    mock_chunk_cls.side_effect = psycopg.OperationalError(
        'connection failed: FATAL:  database "nosuchdb" does not exist')
    mock_graph_cls.return_value = MagicMock()

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 1
    assert "reviewer start" not in result.output
    assert "схема не инициализирована" not in result.output
    assert "базы данных не существует" in result.output
    _assert_no_socket_warnings(recwarn)


@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_stopped_containers_still_advise_start(
    mock_settings_cls, mock_chunk_cls, mock_graph_cls, runner, recwarn
):
    """Обратная сторона: настоящему простою совет по-прежнему выдаётся."""
    mock_settings_cls.return_value = _check_settings()
    mock_chunk_cls.side_effect = psycopg.OperationalError(
        "connection to server at 127.0.0.1, port 5433 failed: Connection refused")
    mock_graph_cls.return_value = MagicMock()

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 1
    assert "reviewer start" in result.output
    _assert_no_socket_warnings(recwarn)


@patch("reviewer.entrypoints.cli.GraphStore")
@patch("reviewer.entrypoints.cli.ChunkStore")
@patch("reviewer.entrypoints.cli.Settings")
def test_check_neo4j_auth_error_does_not_advise_start(
    mock_settings_cls, mock_chunk_cls, mock_graph_cls, runner, recwarn
):
    """Неверные креды Neo4j запуском контейнеров не лечатся (AuthError вне предиката)."""
    mock_settings_cls.return_value = _check_settings()
    mock_chunk_cls.return_value = MagicMock()
    graph = MagicMock()
    graph._driver.verify_connectivity.side_effect = Neo4jAuthError("unauthorized")
    mock_graph_cls.return_value = graph

    result = runner.invoke(cli, ["check"])

    assert result.exit_code == 1
    assert "reviewer start" not in result.output
    _assert_no_socket_warnings(recwarn)
```

Импорты в шапке `tests/entrypoints/test_cli.py` дополнить (если их там ещё нет):

```python
import psycopg
from neo4j.exceptions import AuthError as Neo4jAuthError
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/entrypoints/test_cli.py -q -k check`
Expected: FAIL — новые четыре теста; в первом и третьем `"reviewer start"` присутствует всегда, потому что решение принимается по loopback без учёта причины.

- [ ] **Step 3: Реализовать изменения в `cli.py`**

Импорт (строка 69) — `is_loopback_endpoint` перестаёт использоваться и удаляется, иначе ruff даст F401:

```python
from reviewer.storage_health import (
    DETAIL_AUTH_FAILED, DETAIL_MISSING_DATABASE, classify_storage_failure,
)
```

Рядом с прочими модульными константами `cli.py` добавить:

```python
# Сообщения CLI отдельны от формулировок MCP-payload: у них разные адресаты —
# терминал оператора против брифа, который собирает LLM.
_STORAGE_DETAIL_MESSAGES = {
    DETAIL_AUTH_FAILED: "хранилище отвергло учётные данные — проверьте пароль в .env",
    DETAIL_MISSING_DATABASE: "базы данных не существует — проверьте имя базы в PG_DSN",
}
```

В `check` строку 845 заменить:

```python
    storage_remedy_hint: str | None = None
```

Ветку Postgres (строки 876-887) заменить на:

```python
    except Exception as e:
        err = str(e)
        diagnosis = classify_storage_failure(e, s.pg_dsn)
        if diagnosis.detail:
            # Первым делом: «database ... does not exist» несут и несуществующая
            # БД, и отсутствующая таблица chunks, но лечатся они разным.
            click.echo(f"✗ Postgres: {_STORAGE_DETAIL_MESSAGES[diagnosis.detail]}")
        elif "chunks" in err or "does not exist" in err:
            click.echo(
                "✗ Postgres: схема не инициализирована — выполните reviewer index"
            )
        else:
            click.echo(f"✗ Postgres: {err}")
        failed = True
        storage_remedy_hint = storage_remedy_hint or diagnosis.remedy
```

Ветку Neo4j (строки 900-903) заменить на:

```python
    except Exception as e:
        click.echo(f"✗ Neo4j: {e}")
        failed = True
        storage_remedy_hint = (
            storage_remedy_hint or classify_storage_failure(e, s.neo4j_uri).remedy
        )
```

Подсказку (строки 905-908) заменить на:

```python
    if storage_remedy_hint:
        click.echo(
            f"  Подсказка: локальные хранилища не отвечают — запустите {storage_remedy_hint}"
        )
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `.venv/bin/pytest tests/entrypoints/test_cli.py -q`
Expected: PASS, включая прежние `test_check_fails_on_postgres_error` и `test_check_fails_on_neo4j_error` — они бросают `RuntimeError`, который не является недоступностью хранилища, и потому подсказки больше не получают; их ассерты (`exit_code == 1`, имя хранилища в выводе) этого не касаются.

- [ ] **Step 5: Прогнать линт и полный unit-набор**

Run: `.venv/bin/ruff check reviewer/entrypoints/cli.py tests/entrypoints/test_cli.py && .venv/bin/pytest -q`
Expected: ruff чист; pytest зелёный целиком.

- [ ] **Step 6: Коммит**

```bash
git add reviewer/entrypoints/cli.py tests/entrypoints/test_cli.py
git commit -m 'fix(cli): check не советует reviewer start при живых контейнерах

Решение о подсказке принимал _is_loopback_endpoint, игнорируя пойманное
рядом исключение. Теперь оба хранилища идут через classify_storage_failure:
неверный пароль и несуществующая БД называются по имени, AuthError neo4j
подсказки не получает, а несуществующая БД перестала выдаваться за
неинициализированную схему.'
```

---

### Task 4: Потребитель пятого ключа на стороне плагина

Задача добавлена не при написании плана, а по итогам pre-flight сверки плана со спекой: `remedy: null`
на стороне скилла означало ровно одно — «хранилища удалённые». После Task 2 то же `null` приходит и
при живых локальных контейнерах с неверным паролем, и скилл начнёт утверждать заведомо ложное. Критерий
приёмки 2 — «пользователю не советуется `reviewer start` при живых контейнерах»; заменить бесполезный
совет ложным утверждением значит спрятать дефект, а не выполнить критерий.

Отдельная задача, потому что это prompt-слой с собственной проверкой (регенерация codex-манифеста) —
ревьюер может принять весь код и отдельно спорить о формулировках скилла.

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md` (форма записи `gaps`, строка 39)
- Modify: `plugin/skills/solve-task/references/preflight.md` (шаг 0a: описание `remedy`, строки 49-52; ветка «`remedy` is `null`», строки 64-65)
- Modify: манифесты codex — регенерируются скриптом, вручную не правятся
- Test: `.venv/bin/pytest -q tests/skills tests/install`

**Interfaces:**
- Consumes: пятый ключ `cause_detail` и его значения `auth_failed` / `missing_database` / `null` из Task 2.
- Produces: ничего для последующих задач — конечный потребитель.

- [ ] **Step 1: Обновить форму записи в `SKILL.md`**

В `plugin/skills/solve-task/SKILL.md` заменить фрагмент строки 39-41

```
`gaps` (a list of `{section, reason, cause, remedy}` — branch on `cause`, the
   machine-readable class, not the prose `reason`; copy every entry into **Constraints / open
   questions** in the Step 4 brief verbatim)
```

на

```
`gaps` (a list of `{section, reason, cause, cause_detail, remedy}` — branch on `cause`, the
   machine-readable class, not the prose `reason`; `cause_detail` refines it INSIDE the class
   (`auth_failed` | `missing_database` | `null`) and decides the wording of Step 0a; copy every
   entry into **Constraints / open questions** in the Step 4 brief verbatim)
```

- [ ] **Step 2: Починить шаг 0a в `references/preflight.md`**

Заменить абзац (строки 49-52)

```
       gutted context silently.** The gaps list also carries `remedy` — the command that fixes
       it (`reviewer start`) or `null` when the deployment's storages are remote, where a local
       docker stack fixes nothing.
```

на

```
       gutted context silently.** The gaps list also carries `cause_detail` and `remedy`.
       `remedy` is the command that fixes it (`reviewer start`), or `null` when no command
       applies. `cause_detail` says WHY it does not apply: `auth_failed` — the storage rejected
       the credentials; `missing_database` — the database does not exist; `null` — the cause was
       not named, and only then does a `null` `remedy` mean the storages are remote, where a
       local docker stack fixes nothing.
```

и заменить абзац (строки 64-65)

```
       When `remedy` is `null`, option 1 is not shown at all — say plainly that the storages are
       remote and `reviewer start` does not apply here.
```

на

```
       When `remedy` is `null`, option 1 is not shown at all, and what you say depends on
       `cause_detail`: `auth_failed` → the containers ARE up and the storage rejected the
       credentials, so the password in `.env` is what to check; `missing_database` → the
       containers ARE up but the database does not exist, so the database name in `PG_DSN` is
       what to check; `null` → the storages are remote and `reviewer start` does not apply here.
       Never say «хранилища удалённые» on a named `cause_detail`: the containers are running and
       the claim is false.
```

- [ ] **Step 3: Регенерировать манифест codex**

Любая правка контента под `plugin/` меняет payload-digest, и без регенерации краснеет
`tests/install/test_codex_plugin_payload.py`.

Run: `.venv/bin/python scripts/update_codex_plugin_manifest.py`
Expected: скрипт отрабатывает без ошибок; `git status` показывает изменённые файлы манифеста.

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest -q tests/skills tests/install && .venv/bin/pytest -q`
Expected: зелёно. Guard-тесты сборки промптов (`tests/skills/`) не завязаны на изменённые формулировки — проверено сверкой; если какой-то из них покраснел, это настоящая находка, а не ожидаемое следствие.

- [ ] **Step 5: Коммит**

```bash
git add plugin/skills/solve-task/SKILL.md plugin/skills/solve-task/references/preflight.md
git add <файлы манифеста, которые изменил скрипт — перечислить поимённо по git status>
git commit -m 'docs(solve-task): шаг 0a различает класс причины, а не только лекарство

Пустой remedy означал у скилла ровно одно — «хранилища удалённые». После
пятого ключа cause_detail тот же null приходит и при живых контейнерах с
неверным паролем, и формулировка становилась ложной. Теперь ветка выбирается
по cause_detail, а форма записи gaps в SKILL.md описана целиком.'
```

---

## Проверка приёмки

После Task 4 сверить с критериями задачи:

1. **Различимость.** `cause_detail` равен `auth_failed` / `missing_database` / `null` — три случая различимы машиночитаемо (тесты Task 2).
2. **Нет ложного совета.** `remedy is None` при распознанном классе в обоих каналах (тесты Task 2 и Task 3), и скилл на этом `null` больше не выдаёт ложное «хранилища удалённые» (Task 4).
3. **Нет секретов.** `test_redacted_never_carries_dsn_literals` (Task 1) плюс структурная гарантия: у распознанных случаев `redacted` пуст вовсе.
4. **Закреплено тестом.** Каждая задача заканчивается зелёным `.venv/bin/pytest -q`.

Что в скоуп **не** входит и остаётся как есть: текстовая классификация ошибок Neo4j (нет замера), гранулярность `PoolTimeout` внутри `is_storage_unavailable` (задача ID-330), ретрофит `sanitize_provider_text` в нейтральный модуль.

`git push` и создание PR не выполняются без отдельного подтверждения пользователя.
