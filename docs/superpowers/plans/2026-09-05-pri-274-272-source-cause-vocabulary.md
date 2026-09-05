# Словарь причин недоступности источника контекста — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Научить `prepare_task_context` называть настоящую причину обеднённого контекста — исчерпание пула отличать от лежащих контейнеров, а недоступный эмбеддер от отсутствия задачи на доске.

**Architecture:** Классификация решается типом исключения в одном модуле (`reviewer/storage_health.py`); пул остаётся внутри класса `storage_unavailable` и различается новым `cause_detail`, эмбеддер получает собственный `cause`. Замыкание `_StorageState` обобщается с хранилищ на источники. Причина проводится до `_safe` селективным `strict`-пробросом в двух точках пути контекста и структурным полем синка для секции `task`.

**Tech Stack:** Python 3.11+, psycopg / psycopg_pool, voyageai, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-05-pri-274-272-source-cause-vocabulary-design.md`

## Global Constraints

- Рабочий каталог — worktree `.claude/worktrees/pri-274-272-cause-vocabulary`. Все команды запускаются **находясь в нём**.
- `pytest` и `ruff` берутся из `.venv` основного дерева: `../../../.venv/bin/pytest`, `../../../.venv/bin/ruff`. Своего `.venv` в worktree нет, поэтому pre-commit-хук ruff молча пропускается — линт гоняется вручную.
- Unit-тестам запрещены Postgres, Neo4j, localhost-сокеты и внешняя сеть. Любой тест с реальной сетью обязан нести `@pytest.mark.integration`.
- Язык проекта — русский: комментарии, докстринги, сообщения. Тела скиллов (`plugin/skills/**/*.md`) — английские.
- Коммиты: Conventional Commits на русском, без self-attribution. Каждый коммит заканчивается строкой `Claude-Session: https://claude.ai/code/session_01BzGydxhAurwvd9yTrcPPmq`.
- Классификация причин решается **типом исключения**, не текстом: в тексте `psycopg.OperationalError` живут хост, порт, пользователь и имя базы.
- Публичный `search_codebase`, `/ask`, грунтовка и review-pr в этой работе **не меняются**.
- Базовая линия перед началом: `../../../.venv/bin/pytest -q` — зелёный. Любое падение после правки — регрессия, а не «известное падение».

### Существующие фикстуры (использовать их, новых не заводить)

| Файл | Что даёт |
|---|---|
| `tests/test_storage_health.py` | импортирует модуль как `sh`; стиль ассертов |
| `tests/mcp/test_prepare_task_context.py` | `FakeDeps(**overrides)` — **значение-исключение имитирует сбой секции**; `deps.calls` — список вызванных секций; `_gap_sections(payload)`; `_preflight_gap(exc)` |
| `tests/tasks/test_service_batch.py` | `_FakeStore(hashes=...)`, `_FakeGraph()`, `_FakeEmbedder()`, `_brief(key, alias, **over)`, `_TimingOutStore`; сервис строится как `TaskService(store, graph, emb)` |
| `tests/tasks/test_sync.py` | `FakeProvider(raws)`, `FakeTaskService()`, `FakeMeta()`, `_raw(key, ts)`; сервис — `SyncService([provider], tasks, meta).run()` |
| `tests/tasks/test_search_hits.py` | `_Svc` — носитель методов без зависимостей (для рендера) |
| `tests/skills/test_assembled_prompts.py` | `assemble(rel_path)` — разворачивает `<!-- include: -->` маркеры |

---

### Task 1: Классификатор — пул как деталь, эмбеддер как класс

**Files:**
- Modify: `reviewer/storage_health.py` (константы, докстринг `is_storage_unavailable`, `classify_storage_failure`, новый предикат)
- Test: `tests/test_storage_health.py`

**Interfaces:**
- Consumes: ничего (первая задача).
- Produces: `DETAIL_POOL_EXHAUSTED = "pool_exhausted"`, `CAUSE_EMBEDDER_UNAVAILABLE = "embedder_unavailable"`, `SOURCE_EMBEDDER = "embedder"`, `is_embedder_unavailable(exc: BaseException) -> bool`. `classify_storage_failure(exc, *endpoints)` начинает возвращать `StorageDiagnosis(detail="pool_exhausted", remedy=None, redacted=None)` для `psycopg_pool.PoolTimeout`.

- [ ] **Step 1: Написать падающие тесты классификатора**

Дописать в конец `tests/test_storage_health.py` (модуль там уже импортирован как `sh`; `psycopg`/`psycopg_pool` импортировать, если их ещё нет в шапке):

```python
def test_pool_timeout_gets_pool_exhausted_detail():
    """Исчерпание пула отличимо от закрытого порта — по типу, не по тексту."""
    d = sh.classify_storage_failure(
        psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec"),
        "postgresql://u:p@localhost:5433/reviewer")
    assert d.detail == sh.DETAIL_POOL_EXHAUSTED
    assert d.remedy is None
    assert d.redacted is None


def test_pool_timeout_stays_storage_unavailable():
    """Предикат НЕ меняется: иначе замыкание перестанет ловить пул (PRI-275)."""
    assert sh.is_storage_unavailable(psycopg_pool.PoolTimeout("timeout"))


def test_pool_detail_wins_over_text_patterns():
    """Тип конкретнее текста: сообщение с auth-маркером всё равно даёт пул."""
    d = sh.classify_storage_failure(
        psycopg_pool.PoolTimeout("password authentication failed"),
        "postgresql://u:p@localhost:5433/reviewer")
    assert d.detail == sh.DETAIL_POOL_EXHAUSTED


def test_voyage_errors_are_embedder_unavailable():
    from voyageai.error import APIError, AuthenticationError, ServiceUnavailableError
    for exc in (APIError("HTTP code 403"), AuthenticationError("bad key"),
                ServiceUnavailableError("503")):
        assert sh.is_embedder_unavailable(exc)


def test_rate_limit_is_not_embedder_unavailable():
    """RateLimitError штатно ретраится в with_voyage_retry — это не недоступность."""
    from voyageai.error import RateLimitError
    assert not sh.is_embedder_unavailable(RateLimitError("429"))


def test_storage_errors_are_not_embedder_unavailable():
    assert not sh.is_embedder_unavailable(psycopg.OperationalError("connection refused"))
    assert not sh.is_embedder_unavailable(RuntimeError("boom"))


def test_embedder_error_is_not_storage_unavailable():
    """Эмбеддер не хранилище: reviewer start его не чинит."""
    from voyageai.error import APIError
    assert not sh.is_storage_unavailable(APIError("HTTP code 403"))
```

- [ ] **Step 2: Прогнать и убедиться, что тесты падают**

Run: `../../../.venv/bin/pytest tests/test_storage_health.py -q`
Expected: FAIL — `AttributeError: module 'reviewer.storage_health' has no attribute 'DETAIL_POOL_EXHAUSTED'`.

- [ ] **Step 3: Добавить константы**

В `reviewer/storage_health.py` после строки `BACKEND_POSTGRES = "postgres"`:

```python
# Эмбеддер — третий источник контекста наравне с двумя хранилищами. Класс у него
# свой, а не уточнение storage_unavailable: Voyage не хранилище, контейнеры при
# его отказе подняты, и `reviewer start` не лечит ничего.
CAUSE_EMBEDDER_UNAVAILABLE = "embedder_unavailable"
SOURCE_EMBEDDER = "embedder"
```

Рядом с `DETAIL_AUTH_FAILED` / `DETAIL_MISSING_DATABASE`:

```python
DETAIL_POOL_EXHAUSTED = "pool_exhausted"
```

- [ ] **Step 4: Добавить предикат эмбеддера**

В `reviewer/storage_health.py` после `storage_backend`:

```python
def is_embedder_unavailable(exc: BaseException) -> bool:
    """Не отвечает ли эмбеддер — в отличие от штатного троттлинга.

    Иерархия `voyageai.error` плоская: все классы — прямые наследники
    `VoyageError`, поэтому одна проверка покрывает отказ фронтенда (403),
    неверный ключ, обрыв соединения и таймаут. `RateLimitError` вычитается
    намеренно: free tier — 3 RPM, троттлинг там штатное состояние, его уже
    отрабатывает `with_voyage_retry`, и звать это недоступностью значило бы
    поднимать тревогу на каждом втором прогоне.

    Импорт ленивый: модуль зовут потребители, которым эмбеддер не нужен вовсе,
    и стоимость импорта клиента им платить незачем.
    """
    try:
        from voyageai.error import RateLimitError, VoyageError
    except Exception:  # noqa: BLE001 — без клиента Voyage вопрос не стоит
        return False
    return isinstance(exc, VoyageError) and not isinstance(exc, RateLimitError)
```

- [ ] **Step 5: Добавить ветку пула в классификатор**

Добавить импорт `import psycopg_pool` в шапку модуля рядом с `import psycopg`.

В `classify_storage_failure`, сразу после `text = str(exc)` — то есть ДО цикла по `_DETAIL_PATTERNS`:

```python
    if isinstance(exc, psycopg_pool.PoolTimeout):
        # Тип конкретнее текста: сообщение таймаута может нести чужие маркеры,
        # а свободных соединений от этого не прибавится. Лекарство — поднять
        # pg_pool_max_size или снизить параллелизм, но локальной команды для
        # этого нет, поэтому remedy пуст: `reviewer start` здесь бесполезен.
        return StorageDiagnosis(DETAIL_POOL_EXHAUSTED, None, None)
```

- [ ] **Step 6: Обновить докстринг предиката — он утверждает обратное**

В `is_storage_unavailable` абзац про `PoolTimeout` сегодня объясняет, что одна проверка покрывает и таймаут пула. Это остаётся правдой, но перестаёт быть всей правдой — заменить абзац на:

```python
    `psycopg_pool.PoolTimeout` является подклассом `psycopg.OperationalError` и
    остаётся внутри этого класса намеренно (PRI-274): вывести его отсюда значило
    бы лишить замыкание `_StorageState` возможности поймать его на первом же
    сбое и вернуть восемь таймаутов пула вместо одного. Отличается он не
    предикатом, а `cause_detail` — см. `classify_storage_failure`.
```

- [ ] **Step 7: Прогнать тесты**

Run: `../../../.venv/bin/pytest tests/test_storage_health.py -q`
Expected: PASS, все тесты файла.

- [ ] **Step 8: Линт и коммит**

```bash
../../../.venv/bin/ruff check reviewer/storage_health.py tests/test_storage_health.py
git add reviewer/storage_health.py tests/test_storage_health.py
git commit -F - <<'COMMIT'
feat(storage-health): пул отличается деталью, эмбеддер получает свой класс

PoolTimeout остаётся внутри is_storage_unavailable: вывод из предиката лишил
бы замыкание возможности поймать его на первом сбое. Отличает его cause_detail,
решаемый isinstance до текстовых паттернов — тип конкретнее текста.

Claude-Session: https://claude.ai/code/session_01BzGydxhAurwvd9yTrcPPmq
COMMIT
```

---

### Task 2: Замыкание обобщается с хранилищ на источники

**Files:**
- Modify: `reviewer/mcp/task_context.py` (импорт из `storage_health`, константы формулировок, `_StorageState`, `_storage_gap` → `_source_gap`, `_safe`, третий вызов в `build_task_context`)
- Test: `tests/mcp/test_prepare_task_context.py`

**Interfaces:**
- Consumes: из Task 1 — `CAUSE_EMBEDDER_UNAVAILABLE`, `SOURCE_EMBEDDER`, `DETAIL_POOL_EXHAUSTED`, `is_embedder_unavailable`.
- Produces: `_StorageState.mark(source: str, exc: BaseException | None = None) -> StorageDiagnosis`, `_StorageState.cause_of(source: str) -> str`, `_source_gap(payload, section, state, source, *, skipped: bool) -> None`. Сигнатура `_safe(payload, section, produce, default, reason, state, backend=BACKEND_POSTGRES)` не меняется.

- [ ] **Step 1: Написать падающие тесты**

Дописать в конец `tests/mcp/test_prepare_task_context.py`. Сбой секции задаётся передачей исключения значением override — так устроен `FakeDeps`:

```python
def _build(**overrides):
    return task_context.build_task_context(
        FakeDeps(**overrides), repo="o/n", key="PRI-274", branch="dev",
        warm_board=True)


def _gap(payload, section):
    return next(g for g in payload["gaps"] if g["section"] == section)


def test_embedder_failure_gets_own_cause():
    """Сбой эмбеддера — свой класс, не storage_unavailable и не unknown."""
    from voyageai.error import APIError

    payload = _build(subsystems=APIError("HTTP code 403"))

    entry = _gap(payload, "subsystems")
    assert entry["cause"] == "embedder_unavailable"
    assert entry["remedy"] is None
    assert entry["cause_detail"] is None
    assert "эмбеддер" in entry["reason"]


def test_embedder_failure_does_not_close_storage_sections():
    """Мёртвый эмбеддер не отменяет секции, которым нужен только Postgres."""
    from voyageai.error import APIError

    payload = _build(subsystems=APIError("HTTP code 403"))

    assert payload["task_board"] is not None
    assert payload["task"] is not None
    assert "related.linked" not in _gap_sections(payload)


def test_embedder_closure_skips_later_embedder_sections():
    """Второй заход в мёртвый эмбеддер не делается: секция пропускается."""
    from voyageai.error import APIError

    deps = FakeDeps(similar=APIError("HTTP code 403"))
    payload = task_context.build_task_context(
        deps, repo="o/n", key="PRI-274", branch="dev", warm_board=True)

    assert "code" not in deps.calls
    assert "test_exemplars" not in deps.calls
    entry = _gap(payload, "code")
    assert entry["cause"] == "embedder_unavailable"
    assert entry["reason"].startswith("пропущено")


def test_embedder_closure_does_not_skip_postgres_sections():
    """Замыкание адресное: секции, которым эмбеддер не нужен, всё равно идут."""
    from voyageai.error import APIError

    deps = FakeDeps(similar=APIError("HTTP code 403"))
    task_context.build_task_context(
        deps, repo="o/n", key="PRI-274", branch="dev", warm_board=True)

    assert "task" in deps.calls
    assert "linked" in deps.calls
    assert deps.calls.count("similar") == 1


def test_pool_exhaustion_reports_detail_and_no_remedy():
    """Исчерпание пула: класс storage_unavailable, но лекарство не советуется."""
    entry = _preflight_gap(
        psycopg_pool.PoolTimeout("couldn't get a connection after 30.00 sec"))

    assert entry["cause"] == "storage_unavailable"
    assert entry["cause_detail"] == "pool_exhausted"
    assert entry["remedy"] is None
    assert "пул" in entry["reason"]
```

Перед написанием прочитать `_preflight_gap` (около строки 482) и подстроить последний тест под то, что он фактически возвращает — gap или payload. Соседние `test_auth_failure_is_named_and_loses_remedy` и `test_stopped_containers_still_get_remedy_and_no_detail` показывают ожидаемую форму; следовать ей буквально.

- [ ] **Step 2: Прогнать и убедиться, что падают**

Run: `../../../.venv/bin/pytest tests/mcp/test_prepare_task_context.py -q`
Expected: FAIL — `cause` приходит `unknown` для эмбеддера; для пула `remedy` равен `"reviewer start"`, `cause_detail` пуст.

- [ ] **Step 3: Расширить импорт и формулировки**

В `reviewer/mcp/task_context.py` дополнить импорт из `reviewer.storage_health` именами `CAUSE_EMBEDDER_UNAVAILABLE`, `DETAIL_POOL_EXHAUSTED`, `SOURCE_EMBEDDER`, `is_embedder_unavailable` (список уже многострочный — добавить в алфавитном порядке).

После `SKIPPED_REASON` добавить:

```python
EMBEDDER_REASON = "эмбеддер не отвечает"
EMBEDDER_SKIPPED_REASON = f"пропущено: {EMBEDDER_REASON}"

# Базовая формулировка по источнику: у эмбеддера своя, потому что «хранилище
# не отвечает» про него — неправда, а именно эта неправда и была дефектом.
_SOURCE_REASONS = {
    SOURCE_EMBEDDER: (EMBEDDER_REASON, EMBEDDER_SKIPPED_REASON),
}
_DEFAULT_REASONS = (STORAGE_REASON, SKIPPED_REASON)

# Секции, которым нужен эмбеддер. Разметка явная, а не выведенная: preflight,
# task_board, warm_board, task и related.linked его не трогают вовсе, и замыкать
# их отказом Voyage значило бы терять данные, которые собрались бы без него.
_EMBEDDER_SECTIONS = frozenset({
    "related.similar", "subsystems", "code", "test_exemplars",
})
```

В `DETAIL_REASONS` добавить строку:

```python
    DETAIL_POOL_EXHAUSTED: "свободных соединений в пуле не осталось",
```

- [ ] **Step 4: Обобщить состояние**

Заменить `__init__` и `mark` в `_StorageState`, добавить `cause_of`:

```python
    def __init__(self, endpoints: tuple[str, ...]) -> None:
        self.endpoints = endpoints
        self.down: set[str] = set()
        self.diagnoses: dict[str, StorageDiagnosis] = {}
        self.causes: dict[str, str] = {}

    def mark(self, source: str, exc: BaseException | None = None) -> StorageDiagnosis:
        """Взвести флаг источника; вердикт считается по его первому сбою.

        `exc` необязателен: недоступность эмбеддера может прийти не броском, а
        структурным полем свода синка (секция `task`), и тогда классифицировать
        нечего — класс известен из самого источника.
        """
        self.down.add(source)
        if source not in self.diagnoses:
            if source == SOURCE_EMBEDDER:
                self.diagnoses[source] = StorageDiagnosis(None, None, None)
                self.causes[source] = CAUSE_EMBEDDER_UNAVAILABLE
            else:
                self.diagnoses[source] = (
                    classify_storage_failure(exc, *self.endpoints)
                    if exc is not None else StorageDiagnosis(None, None, None))
                self.causes[source] = CAUSE_STORAGE_UNAVAILABLE
        return self.diagnoses[source]

    def cause_of(self, source: str) -> str:
        """Класс причины по источнику; `unknown`, пока источник не помечен."""
        return self.causes.get(source, CAUSE_UNKNOWN)

    def is_down(self, source: str) -> bool:
        return source in self.down
```

В докстринг класса добавить абзац:

```python
    Источник, а не только хранилище (PRI-272): эмбеддер отказывает независимо
    от Postgres и Neo4j, и его отказ обязан отменять ровно те секции, которым
    он нужен. Класс причины хранится рядом с вердиктом — «лежит хранилище» и
    «не отвечает эмбеддер» это разные классы, а не разные детали одного.
```

- [ ] **Step 5: Заменить `_storage_gap` на `_source_gap`**

Сегодня функция принимает готовый `reason` третьим позиционным аргументом. Формулировка теперь выводится из источника, поэтому вместо `reason` передаётся флаг `skipped`:

```python
def _source_gap(payload: dict, section: str, state: _StorageState, source: str,
                *, skipped: bool) -> None:
    """Записать в gaps пробел, вызванный недоступностью источника.

    Общая точка для трёх мест, различающихся только тем, пропущена секция или
    реально упала: skip- и except-ветки `_safe`, а также ветка
    `elif warm_board and not board` в build_task_context. Вердикт, класс и
    базовая формулировка берутся по источнику секции, а не общие на вызов.
    """
    diagnosis = state.diagnoses.get(source)
    base, skipped_base = _SOURCE_REASONS.get(source, _DEFAULT_REASONS)
    reason = skipped_base if skipped else base
    payload["gaps"].append(gap(
        section, _reason_with_detail(reason, diagnosis),
        cause=state.cause_of(source),
        cause_detail=diagnosis.detail if diagnosis is not None else None,
        remedy=diagnosis.remedy if diagnosis is not None else None))
```

`_reason_with_detail` не трогать: она подставляет деталь вместо подстроки `STORAGE_REASON`, а у эмбеддера `detail` всегда `None` — подстановка не сработает по построению.

- [ ] **Step 6: Научить `_safe` распознавать эмбеддер**

Skip-ветку в начале `_safe` заменить на проверку обоих источников:

```python
    if section in _EMBEDDER_SECTIONS and state.is_down(SOURCE_EMBEDDER):
        _source_gap(payload, section, state, SOURCE_EMBEDDER, skipped=True)
        return default
    if state.is_down(backend):
        _source_gap(payload, section, state, backend, skipped=True)
        return default
```

Блок `except` заменить на:

```python
        except Exception as exc:  # noqa: BLE001 — источник секции недоступен, это штатный случай
            log.warning("prepare_task_context: секция %s недоступна", section, exc_info=True)
            if is_embedder_unavailable(exc):
                state.mark(SOURCE_EMBEDDER, exc)
                _source_gap(payload, section, state, SOURCE_EMBEDDER, skipped=False)
            elif is_storage_unavailable(exc):
                failed = storage_backend(exc) or backend
                state.mark(failed, exc)
                _source_gap(payload, section, state, failed, skipped=False)
            else:
                payload["gaps"].append(gap(section, reason))
            return default
```

Порядок веток обязателен: эмбеддер проверяется первым, потому что `is_storage_unavailable` про ошибки Voyage ложен, а обратная проверка ничего не стоит и оставляет поведение хранилищ неизменным.

- [ ] **Step 7: Обновить третий вызов**

В `build_task_context`, в ветке `elif warm_board and not board`, заменить

```python
            _storage_gap(payload, "warm_board", SKIPPED_REASON, state, BACKEND_POSTGRES)
```

на

```python
            _source_gap(payload, "warm_board", state, BACKEND_POSTGRES, skipped=True)
```

После этого имени `_storage_gap` в файле остаться не должно — проверить `grep -n "_storage_gap" reviewer/mcp/task_context.py`.

- [ ] **Step 8: Прогнать тесты**

Run: `../../../.venv/bin/pytest tests/mcp/test_prepare_task_context.py tests/test_storage_health.py -q`
Expected: PASS. Существующие тесты файла (`test_storage_failure_names_cause_and_remedy`, `test_both_stores_down_keep_separate_diagnoses`, `test_warm_board_gap_reflects_storage_down_not_misconfigured_board`) обязаны остаться зелёными без правок — они и есть проверка, что поведение хранилищ не изменилось.

- [ ] **Step 9: Линт и коммит**

```bash
../../../.venv/bin/ruff check reviewer/mcp/task_context.py tests/mcp/test_prepare_task_context.py
git add reviewer/mcp/task_context.py tests/mcp/test_prepare_task_context.py
git commit -F - <<'COMMIT'
feat(task-context): замыкание по источникам, а не только по хранилищам

Эмбеддер отказывает независимо от Postgres и Neo4j, поэтому его отказ обязан
отменять ровно свои секции. Класс причины теперь хранится рядом с вердиктом:
«лежит хранилище» и «не отвечает эмбеддер» — разные классы, а не детали одного.

Claude-Session: https://claude.ai/code/session_01BzGydxhAurwvd9yTrcPPmq
COMMIT
```

---

### Task 3: Синк сообщает о сбое эмбеддера структурно

**Files:**
- Modify: `reviewer/tasks/service.py` (`_skipped_result`, `index_batch`), `reviewer/tasks/sync.py` (`_sync_provider` — подсчёт и `one.update`; `run` — `agg`, суммирование, `by_board`)
- Test: `tests/tasks/test_service_batch.py`, `tests/tasks/test_sync.py`

**Interfaces:**
- Consumes: из Task 1 — `is_embedder_unavailable`.
- Produces: каждая строка результата `index_batch` получает ключ `failure: str | None` (`"embedder"` | `"storage"` | `None`). Свод `sync_board` получает булев ключ `embedder_failed` — и в `agg`, и в каждом элементе `by_board`.

- [ ] **Step 1: Написать падающие тесты батча**

Дописать в конец `tests/tasks/test_service_batch.py`. Смешанный стор взят с существующего `test_index_batch_embed_error_marks_changed_warns_but_meta_only_ok`: первая задача неизменна (meta-only), вторая доходит до `to_embed` — однородная фикстура оставила бы `to_embed` пустым структурно, и тест был бы зелёным по построению:

```python
def test_batch_marks_embedder_failure_as_embedder():
    """Сбой эмбеддера и сбой стора различимы полем, а не текстом warnings."""
    from voyageai.error import APIError

    class _VoyageDownEmbedder(_FakeEmbedder):
        def embed_documents(self, texts):
            super().embed_documents(texts)
            raise APIError("HTTP code 403")

    t1 = build_task_text("Add logout", "Clear session", ["redirects"])
    store = _FakeStore(hashes={"ID-1": task_content_hash(t1)})
    tasks = [_brief("ID-1", "PRI-1"),
             _brief("ID-2", "PRI-2", title="Fix bug", description="desc", links=[])]
    results = TaskService(store, _FakeGraph(), _VoyageDownEmbedder()).index_batch(tasks)

    assert results[0]["failure"] is None      # unchanged — meta-only, сбоя не видела
    assert results[1]["failure"] == "embedder"
    assert results[1]["retry_required"] is True


def test_batch_marks_storage_failure_as_storage():
    """Отказ стора остаётся отказом стора — класс не подменяется эмбеддером."""
    results = TaskService(
        _TimingOutStore(), _FakeGraph(), _FakeEmbedder()
    ).index_batch([_brief()])

    assert results[0]["failure"] == "storage"


def test_batch_unknown_embed_error_is_not_called_embedder():
    """Непонятный сбой классом не награждается: врать про причину нельзя."""
    class _BrokenEmbedder(_FakeEmbedder):
        def embed_documents(self, texts):
            raise RuntimeError("voyage down")

    results = TaskService(
        _FakeStore(), _FakeGraph(), _BrokenEmbedder()
    ).index_batch([_brief()])

    assert results[0]["failure"] is None
    assert results[0]["retry_required"] is True


def test_batch_reports_no_failure_on_success():
    results = TaskService(_FakeStore(), _FakeGraph(), _FakeEmbedder()).index_batch(
        [_brief()])
    assert results[0]["failure"] is None
```

- [ ] **Step 2: Прогнать и убедиться, что падают**

Run: `../../../.venv/bin/pytest tests/tasks/test_service_batch.py -q`
Expected: FAIL — `KeyError: 'failure'`.

- [ ] **Step 3: Провести класс через `index_batch`**

Добавить импорт `is_embedder_unavailable` из `reviewer.storage_health` в шапку `reviewer/tasks/service.py` (рядом с существующим импортом `is_storage_unavailable`).

В `_skipped_result` добавить в возвращаемый словарь `"failure": "storage"` — это ранний выход по уже замкнутому стору.

В `index_batch` рядом с существующим `storage_down = False` добавить:

```python
        # Симметрично storage_down: первый же отказ эмбеддера гасит дальнейшие
        # попытки. Класс решается типом исключения ЗДЕСЬ, пока объект жив: этажом
        # выше от него остаётся только строка, и различить причину уже нечем.
        embedder_down = False
```

В обработчике `embed_documents` дополнить:

```python
            except Exception as e:
                log.warning("index_batch: сбой embed_documents", exc_info=True)
                embed_err = f"embedder: {type(e).__name__}: {e}"
                embedder_down = is_embedder_unavailable(e)
```

Каждая строка результата получает ключ `failure`. Правило заполнения:
- ветка, где сработал `embed_err` **и** `embedder_down` — `"embedder"`;
- любая ветка, ставящая warning вида `store: …` или срабатывающая по `storage_down` — `"storage"`;
- все остальные, включая раннюю `"task has no key"`, успех и `graph unavailable`, — `None`.

Пройти по всем `return` и присваиваниям `results[i]` в `index_batch` и `_skipped_result` и добавить ключ; отсутствие ключа хотя бы в одной ветке уронит тест Step 1 на `KeyError`.

- [ ] **Step 4: Прогнать тесты батча**

Run: `../../../.venv/bin/pytest tests/tasks/test_service_batch.py -q`
Expected: PASS, включая существующие `test_first_pool_timeout_stops_further_store_calls` и `test_pool_timeout_skips_voyage_call_entirely`.

- [ ] **Step 5: Написать падающий тест агрегата синка**

Дописать в конец `tests/tasks/test_sync.py`:

```python
class _EmbedderDownTaskService(FakeTaskService):
    """Батч, где эмбеддер лёг: index_batch отдаёт класс структурно."""

    def index_batch(self, tasks):
        rows = super().index_batch(tasks)
        for row in rows:
            row.update({"embedded": False, "failure": "embedder",
                        "retry_required": True,
                        "warnings": ["embedder: APIError: HTTP code 403"]})
        return rows


def test_sync_aggregates_embedder_failure_flag():
    """Свод синка несёт булев признак, а не только текст в warnings."""
    provider = FakeProvider([_raw("ID-1", 100)])
    summary = SyncService([provider], _EmbedderDownTaskService(), FakeMeta()).run()

    assert summary["embedder_failed"] is True
    assert summary["by_board"][0]["embedder_failed"] is True


def test_sync_embedder_flag_false_without_embedder_failure():
    provider = FakeProvider([_raw("ID-1", 100)])
    summary = SyncService([provider], FakeTaskService(), FakeMeta()).run()

    assert summary["embedder_failed"] is False
    assert summary["by_board"][0]["embedder_failed"] is False
```

- [ ] **Step 6: Прогнать и убедиться, что падает**

Run: `../../../.venv/bin/pytest tests/tasks/test_sync.py -q`
Expected: FAIL — `KeyError: 'embedder_failed'`.

- [ ] **Step 7: Агрегировать признак в синке**

В `_sync_provider`, рядом с `failed = sum(1 for r in results if r.get("warnings"))`:

```python
        embedder_failed = any(r.get("failure") == "embedder" for r in results)
```

В `one.update({...})` добавить `"embedder_failed": embedder_failed,`.

В инициализации `agg` в `run` добавить `"embedder_failed": False,` (рядом с `"cursor_advanced": False`).

Сразу после строки, объединяющей `cursor_advanced`:

```python
            agg["embedder_failed"] = agg["embedder_failed"] or one["embedder_failed"]
```

В кортеж ключей, копируемых в `by_board`, добавить `"embedder_failed"`. В кортеж числового суммирования (`for k in ("enumerated", …): agg[k] += one[k]`) этот ключ попасть не должен — он булев.

- [ ] **Step 8: Прогнать тесты**

Run: `../../../.venv/bin/pytest tests/tasks/ -q`
Expected: PASS.

- [ ] **Step 9: Линт и коммит**

```bash
../../../.venv/bin/ruff check reviewer/tasks/service.py reviewer/tasks/sync.py tests/tasks/
git add reviewer/tasks/service.py reviewer/tasks/sync.py tests/tasks/
git commit -F - <<'COMMIT'
feat(tasks): синк сообщает о сбое эмбеддера полем, а не строкой warnings

Класс решается типом исключения там, где объект ещё жив: этажом выше от него
оставалась только строка, и отличить отказ Voyage от отказа стора было нечем.
Замыкание embedder_down симметрично существующему storage_down.

Claude-Session: https://claude.ai/code/session_01BzGydxhAurwvd9yTrcPPmq
COMMIT
```

---

### Task 4: Селективный проброс на пути контекста

**Files:**
- Modify: `reviewer/tasks/service.py` (`search_hits`), `reviewer/mcp/service.py` (`_search_codebase_multi`, `_TaskContextDeps.similar` / `.code` / `.test_exemplars`)
- Test: `tests/tasks/test_search_hits.py`, `tests/mcp/test_service.py`

**Interfaces:**
- Consumes: из Task 1 — `is_embedder_unavailable`.
- Produces: `TaskService.search_hits(query, top_k=None, project=None, *, strict: bool = False)`, `MCPReviewService._search_codebase_multi(repo, queries, branch=None, include_tests=False, augment_sources=None, *, strict: bool = False)`. При `strict=True` пробрасывается только распознанный сбой эмбеддера; остальное поведение неизменно.

- [ ] **Step 1: Написать падающие тесты `search_hits`**

Дописать в конец `tests/tasks/test_search_hits.py`:

```python
import pytest


class _EmbedderRaising:
    def __init__(self, exc):
        self._exc = exc

    def embed_query(self, query):
        raise self._exc


def _svc(exc):
    return TaskService(None, None, _EmbedderRaising(exc))


def test_search_hits_swallows_embedder_failure_by_default():
    """Публичный путь остаётся немым: /ask и грунтовка на этом стоят."""
    from voyageai.error import APIError
    assert _svc(APIError("HTTP code 403")).search_hits("q") is None


def test_search_hits_reraises_embedder_failure_when_strict():
    from voyageai.error import APIError
    with pytest.raises(APIError):
        _svc(APIError("HTTP code 403")).search_hits("q", strict=True)


def test_search_hits_stays_soft_on_other_failures_when_strict():
    """Строгость адресная: непонятный сбой по-прежнему гасится."""
    assert _svc(RuntimeError("boom")).search_hits("q", strict=True) is None
```

`TaskService` в этом файле уже импортирован. Аргументы позиционные — та же форма, что в `test_service_batch.py` (`TaskService(store, graph, emb)`); `store`/`graph` не участвуют, потому что исключение бросается раньше.

- [ ] **Step 2: Написать падающие тесты `_search_codebase_multi`**

Дописать в `tests/mcp/test_service.py`, переиспользуя ту конструкцию MCP-сервиса, что уже принята в этом файле (не заводить свою). Провал подкладывается в `components.retriever`, чтобы `search_multi` бросил:

```python
def test_search_codebase_multi_swallows_embedder_failure_by_default():
    """Приватный мультизапрос по умолчанию нем — как и был."""
    from voyageai.error import APIError

    svc = _service_with_failing_retriever(APIError("HTTP code 403"))
    assert svc._search_codebase_multi("o/n", ["q"], "dev") == "(ничего не найдено)"


def test_search_codebase_multi_reraises_embedder_failure_when_strict():
    from voyageai.error import APIError

    svc = _service_with_failing_retriever(APIError("HTTP code 403"))
    with pytest.raises(APIError):
        svc._search_codebase_multi("o/n", ["q"], "dev", strict=True)


def test_search_codebase_multi_stays_soft_on_other_failures_when_strict():
    svc = _service_with_failing_retriever(RuntimeError("boom"))
    assert svc._search_codebase_multi(
        "o/n", ["q"], "dev", strict=True) == "(ничего не найдено)"
```

Хелпер `_service_with_failing_retriever(exc)` написать рядом: он строит сервис так же, как соседние тесты файла, и подменяет `components.retriever` объектом, чьи методы бросают `exc`. Разрешение `repo`/`branch` обязано проходить — иначе метод вернёт строку ошибки раньше, чем дойдёт до `search_multi`, и тест окажется зелёным по построению.

- [ ] **Step 3: Прогнать и убедиться, что падают**

Run: `../../../.venv/bin/pytest tests/tasks/test_search_hits.py tests/mcp/test_service.py -q -k "strict or embedder"`
Expected: FAIL — `TypeError: search_hits() got an unexpected keyword argument 'strict'`.

- [ ] **Step 4: Добавить строгость в `search_hits`**

```python
    def search_hits(self, query: str, top_k: int | None = None,
                    project: str | None = None, *, strict: bool = False) -> list | None:
```

Блок `except` заменить на:

```python
        except Exception as e:
            if strict and is_embedder_unavailable(e):
                raise
            log.warning("search_hits: сбой поиска по запросу %r", query, exc_info=True)
            return None
```

В докстринг добавить абзац:

```
        `strict` (PRI-272) — keyword-only, как у `_repo_clone_path` (PRI-275):
        при нём распознанный отказ эмбеддера пробрасывается вызывающему, чтобы
        тот назвал причину. Зовёт со `strict=True` только сборка контекста
        задачи; публичный `search_tasks` остаётся немым намеренно.
```

- [ ] **Step 5: Добавить строгость в `_search_codebase_multi`**

```python
    def _search_codebase_multi(self, repo: str, queries: list[str],
                               branch: str | None = None,
                               include_tests: bool = False,
                               augment_sources: list | None = None,
                               *, strict: bool = False) -> str:
```

Блок `except` заменить на:

```python
        except Exception as exc:
            if strict and is_embedder_unavailable(exc):
                raise
            log.warning("_search_codebase_multi: сбой поиска", exc_info=True)
            return "(ничего не найдено)"
```

Добавить `is_embedder_unavailable` в импорты `reviewer/mcp/service.py`. Публичный `search_codebase` **не трогать**.

- [ ] **Step 6: Передать строгость из провайдера секций**

В `_TaskContextDeps` — три метода:

```python
    def similar(self, query: str, project: str | None) -> str:
        service = self._service.components.task_service
        hits = service.search_hits(query, project=project, strict=True)
        self._similar_hits = list(hits or [])
        return service.render_hits(hits)
```

в `code` — добавить `strict=True` в вызов `self._service._search_codebase_multi(repo, queries, branch, False, augment_sources=sources)`;

в `test_exemplars` — `return self._service._search_codebase_multi(repo, queries, branch, True, strict=True)`.

Докстринги `similar` не сокращать. `_augment_paths` не трогать: он обёрнут собственным `try` намеренно, чтобы сбой подмешивания не обнулял всю секцию.

- [ ] **Step 7: Прогнать тесты**

Run: `../../../.venv/bin/pytest tests/tasks/ tests/mcp/ -q`
Expected: PASS.

- [ ] **Step 8: Линт и коммит**

```bash
../../../.venv/bin/ruff check reviewer/tasks/service.py reviewer/mcp/service.py tests/tasks/test_search_hits.py tests/mcp/test_service.py
git add reviewer/tasks/service.py reviewer/mcp/service.py tests/tasks/test_search_hits.py tests/mcp/test_service.py
git commit -F - <<'COMMIT'
feat(context): адресный проброс отказа эмбеддера на пути контекста задачи

Два fail-soft барьера гасили APIError до _safe, и никакой класс причины не мог
появиться в gaps. Строгость keyword-only и включается единственным вызывающим —
сборкой контекста; публичный search_codebase, /ask и review-pr не меняются.

Claude-Session: https://claude.ai/code/session_01BzGydxhAurwvd9yTrcPPmq
COMMIT
```

---

### Task 5: Секция `task` берёт причину из синка

**Files:**
- Modify: `reviewer/mcp/task_context.py` (ветка `if warm_board and board` и блок секции `task` в `build_task_context`)
- Test: `tests/mcp/test_prepare_task_context.py`

**Interfaces:**
- Consumes: из Task 2 — `_StorageState.mark`, `cause_of`, `SOURCE_EMBEDDER`, `EMBEDDER_REASON`, хелперы `_build`/`_gap`; из Task 3 — ключ `embedder_failed` в своде `warm_board`.
- Produces: ничего для последующих задач.

- [ ] **Step 1: Написать падающий тест**

Дописать в `tests/mcp/test_prepare_task_context.py`:

```python
def test_task_gap_blames_embedder_not_the_board():
    """Задачи нет в сторе, потому что её туда не пустил упавший синк.

    Секция `task` не бросает вовсе, поэтому проброс её не лечит: причина
    рождается этажом выше, в своде warm_board.
    """
    payload = _build(
        warm_board={"enumerated": 126, "changed": 3, "failed": 3,
                    "embedder_failed": True},
        task=None)

    entry = _gap(payload, "task")
    assert entry["cause"] == "embedder_unavailable"
    assert "эмбеддер" in entry["reason"]
    assert "нет в сторе" not in entry["reason"]


def test_task_gap_stays_board_shaped_without_embedder_failure():
    """Без сбоя эмбеддера формулировка прежняя — задачи действительно нет."""
    payload = _build(
        warm_board={"enumerated": 126, "changed": 0, "failed": 0,
                    "embedder_failed": False},
        task=None)

    entry = _gap(payload, "task")
    assert entry["cause"] == "unknown"
    assert entry["reason"] == "задачи нет в сторе"


def test_legacy_warm_board_summary_without_the_flag_still_works():
    """Свод без нового ключа (старый деплой) проходит без исключения."""
    payload = _build(warm_board={"enumerated": 10, "changed": 0}, task=None)
    assert _gap(payload, "task")["reason"] == "задачи нет в сторе"
```

Существующий `test_task_missing_is_a_gap_not_an_error` обязан остаться зелёным — он и есть проверка обратной совместимости формулировки.

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `../../../.venv/bin/pytest tests/mcp/test_prepare_task_context.py -q -k task_gap`
Expected: FAIL — `cause` приходит `unknown`, reason «задачи нет в сторе».

- [ ] **Step 3: Прочитать признак после прогрева доски**

В `build_task_context`, внутри ветки `if warm_board and board:` — сразу после `payload["warnings"].append({"warm_board": result})`:

```python
        if isinstance(result, dict) and result.get("embedder_failed"):
            # Синк уже назвал класс структурно; исключения здесь нет и быть не
            # может — index_batch отработал его сам, отметив задачи к повтору.
            state.mark(SOURCE_EMBEDDER)
```

`isinstance` обязателен: свод старого деплоя ключа не несёт, а `result` в принципе может оказаться не словарём.

- [ ] **Step 4: Уточнить формулировку секции `task`**

Заменить блок после `payload["task"] = task`:

```python
    if task is None and not any(g["section"] == "task" for g in payload["gaps"]):
        if state.is_down(SOURCE_EMBEDDER):
            # «Задачи нет в сторе» указывает на доску, а виноват эмбеддер: задача
            # на доске есть, её не удалось проиндексировать.
            payload["gaps"].append(gap(
                "task", f"задача не проиндексирована: {EMBEDDER_REASON}",
                cause=state.cause_of(SOURCE_EMBEDDER)))
        else:
            payload["gaps"].append(gap("task", "задачи нет в сторе"))
```

- [ ] **Step 5: Прогнать тесты**

Run: `../../../.venv/bin/pytest tests/mcp/ -q`
Expected: PASS.

- [ ] **Step 6: Линт и коммит**

```bash
../../../.venv/bin/ruff check reviewer/mcp/task_context.py tests/mcp/test_prepare_task_context.py
git add reviewer/mcp/task_context.py tests/mcp/test_prepare_task_context.py
git commit -F - <<'COMMIT'
fix(task-context): «задачи нет в сторе» больше не сваливает вину на доску

Секция task не бросает: задача на доске есть, её не пустил внутрь упавший синк.
Причина читается структурным признаком свода warm_board, который выполняется
раньше секции, — порядок сборки менять не потребовалось.

Claude-Session: https://claude.ai/code/session_01BzGydxhAurwvd9yTrcPPmq
COMMIT
```

---

### Task 6: Клиент ветвится по классу, а не по одному значению

**Files:**
- Modify: `plugin/skills/solve-task/references/preflight.md` (шаг 0a)
- Create: `tests/skills/test_source_cause_vocabulary.py`
- Modify: манифесты, перестраиваемые `scripts/update_codex_plugin_manifest.py`

**Interfaces:**
- Consumes: из Task 1 — константы `CAUSE_*` и `DETAIL_*`.
- Produces: ничего для последующих задач.

- [ ] **Step 1: Прочитать текущий шаг 0a**

Run: `sed -n '40,90p' plugin/skills/solve-task/references/preflight.md`

Понадобится точная форма существующего блока: правка сохраняет три варианта выбора и правило «в `full-auto` не спрашивать».

- [ ] **Step 2: Написать падающий guard-тест**

Создать `tests/skills/test_source_cause_vocabulary.py`:

```python
"""Guard: словарь классов причин в коде и в тексте скилла не расходится (PRI-272).

Расхождение здесь молчаливо: незнакомый класс проходит мимо ветки шага 0a,
и пользователь снова не узнаёт причину — ровно тот дефект, который чинится.
"""
from reviewer import storage_health as sh

from .test_assembled_prompts import assemble


def _skill() -> str:
    return assemble("solve-task/SKILL.md")


def test_every_cause_constant_is_named_in_the_skill():
    text = _skill()
    for cause in (sh.CAUSE_STORAGE_UNAVAILABLE, sh.CAUSE_EMBEDDER_UNAVAILABLE,
                  sh.CAUSE_UNKNOWN):
        assert cause in text, f"класс {cause} не назван в скилле"


def test_every_detail_constant_is_named_in_the_skill():
    text = _skill()
    for detail in (sh.DETAIL_AUTH_FAILED, sh.DETAIL_MISSING_DATABASE,
                   sh.DETAIL_POOL_EXHAUSTED):
        assert detail in text, f"уточнение {detail} не названо в скилле"


def test_skill_does_not_gate_on_a_single_cause_equality():
    """Шаг 0a обязан ветвиться по классу вообще, а не по равенству одному."""
    text = _skill()
    assert "`cause` is `storage_unavailable`" not in text
```

Строку в третьем тесте выверить по фактическому тексту из Step 1 — она обязана совпадать с той формулировкой равенства, которая там сейчас стоит, иначе тест зелен по построению.

- [ ] **Step 3: Прогнать и убедиться, что падает**

Run: `../../../.venv/bin/pytest tests/skills/test_source_cause_vocabulary.py -q`
Expected: FAIL — `pool_exhausted` и `embedder_unavailable` в тексте отсутствуют, а старая формулировка равенства присутствует.

- [ ] **Step 4: Переписать шаг 0a**

Переписать блок так, чтобы он:
- искал любой gap, чей `cause` не равен `unknown`, — не равенство одному классу;
- называл класс пользователю по-русски: `storage_unavailable` → «хранилище не отвечает», `embedder_unavailable` → «эмбеддер не отвечает»;
- перечислял `cause_detail`: `auth_failed` → «хранилище отвергло учётные данные», `missing_database` → «базы данных не существует», `pool_exhausted` → «свободных соединений в пуле не осталось: поднять `pg_pool_max_size` или снизить параллелизм; `reviewer start` здесь не поможет»;
- показывал вариант «Поднять сейчас» **только** при непустом `remedy`, и прямо оговаривал, что при `embedder_unavailable` и при `pool_exhausted` `remedy` пуст по построению;
- сохранял три варианта выбора и правило «в `full-auto` не спрашивать».

Тело блока — английское, как остальной скилл; фразы, которые скилл произносит пользователю, — русские.

- [ ] **Step 5: Прогнать guard**

Run: `../../../.venv/bin/pytest tests/skills/ -q`
Expected: PASS.

- [ ] **Step 6: Мутационная проверка guard-теста**

Guard, зелёный по построению, не проверяет ничего. Проверять на копии дерева ВНЕ worktree, чтобы рабочие файлы не пострадали. Абсолютный путь к pytest снять заранее: `PYTEST=$(cd ../../.. && pwd)/.venv/bin/pytest`.

```bash
TMP=$(mktemp -d)
cp -R plugin tests reviewer pyproject.toml "$TMP"/
perl -pi -e 's/pool_exhausted/XXX_REMOVED/g' "$TMP/plugin/skills/solve-task/references/preflight.md"
cd "$TMP" && "$PYTEST" tests/skills/test_source_cause_vocabulary.py -q
```

Expected: FAIL — `test_every_detail_constant_is_named_in_the_skill` краснеет.

Повторить, снимая по одному: `embedder_unavailable` (краснеет тест классов) и вернув в копию формулировку равенства (краснеет третий тест). Тест, оставшийся зелёным после снятия своего условия, переписать. В конце вернуться в worktree и удалить `"$TMP"`.

- [ ] **Step 7: Пересобрать манифесты**

Любая правка контента под `plugin/` меняет codex payload-digest.

Run: `../../../.venv/bin/python scripts/update_codex_plugin_manifest.py`
Затем: `../../../.venv/bin/pytest tests/ -q -k "install or manifest"`
Expected: PASS.

- [ ] **Step 8: Линт и коммит**

```bash
../../../.venv/bin/ruff check tests/skills/test_source_cause_vocabulary.py
git add plugin/ tests/skills/test_source_cause_vocabulary.py
git commit -F - <<'COMMIT'
feat(solve-task): шаг 0a ветвится по классу причины, а не по одному значению

Проверка на равенство storage_unavailable пропускала бы новый класс молча —
ровно тот дефект, ради которого задача и делалась. Guard-тест сверяет словарь
классов в коде с текстом скилла, чтобы следующий класс не разъехался снова.

Claude-Session: https://claude.ai/code/session_01BzGydxhAurwvd9yTrcPPmq
COMMIT
```

---

### Task 7: Сквозная верификация и живая приёмка

**Files:**
- Create: `eval/pri274_272_acceptance.md`
- Modify: прочее — только по факту найденного

**Interfaces:**
- Consumes: всё, произведённое задачами 1-6.
- Produces: отчёт о приёмке.

- [ ] **Step 1: Полный прогон unit-тестов**

Run: `../../../.venv/bin/pytest -q`
Expected: PASS целиком. Базовая линия зелёная перед началом работы, поэтому любое падение здесь — регрессия.

- [ ] **Step 2: Проверить, что контракт PRI-275 цел**

Run: `../../../.venv/bin/pytest tests/mcp/test_service.py::test_task_context_deps_preflight_raises_and_stops_after_one_store_call -v`
Expected: PASS. Тест оставлен PRI-275 как индикатор: его покраснение означало бы, что `PoolTimeout` всё-таки вывели из предиката.

- [ ] **Step 3: Линт всего изменённого**

Run: `../../../.venv/bin/ruff check reviewer/ tests/`
Expected: чисто.

- [ ] **Step 4: Приёмка PRI-274 на живом деплое**

Занять весь пул (`pg_pool_max_size = 4`) и вызвать `prepare_task_context`. Замер выполнять **в одиночку**: параллельная сессия reviewer займёт те же соединения и исказит опыт.

Проверить: `cause == "storage_unavailable"`, `cause_detail == "pool_exhausted"`, `remedy is None`, время ответа — один таймаут (~30 с), а не восемь.

- [ ] **Step 5: Приёмка PRI-272 на живом деплое**

Сделать эмбеддер недоступным при живых хранилищах (например, подменить базовый URL Voyage на неотвечающий адрес — конкретный способ выбрать при исполнении, ключ в конфиге не портить).

Проверить: секции `related.similar`, `subsystems`, `code`, `test_exemplars` несут `cause == "embedder_unavailable"` с пустым `remedy`; секция `task` говорит «задача не проиндексирована: эмбеддер не отвечает», а не «задачи нет в сторе»; хранилищные секции (`preflight`, `task_board`) собрались нормально.

- [ ] **Step 6: Записать отчёт приёмки и закоммитить**

Записать наблюдаемые величины обоих опытов в `eval/pri274_272_acceptance.md` — числа и фактические payload'ы, а не утверждения об успехе.

```bash
git add eval/pri274_272_acceptance.md
git commit -F - <<'COMMIT'
docs(pri-274,pri-272): замер приёмки — пул и эмбеддер называются по имени

Claude-Session: https://claude.ai/code/session_01BzGydxhAurwvd9yTrcPPmq
COMMIT
```

---

## Self-Review

**Покрытие спеки.** Решение 1 (пул как деталь) — Task 1. Решение 2 (класс эмбеддера) — Task 1. Решение 3 (один модуль) — Task 1. Решение 4 (замыкание по источникам) — Task 2. Решение 5 (два барьера, `strict`) — Task 4. Решение 6 (структурный признак синка) — Task 3 и Task 5. Решение 7 (клиент и guard) — Task 6. Раздел «Совместимость» проверяется Task 7 шагом 2 и тестом `test_legacy_warm_board_summary_without_the_flag_still_works` в Task 5. Раздел «Тестирование» распределён по задачам 1-6, мутационная проверка — Task 6 шаг 6. Пробелов нет.

**Плейсхолдеры.** Три места намеренно описывают требования вместо буквального текста, и каждое закрыто машинной проверкой: тело шага 0a (Task 6 шаг 4 — свойства перечислены, guard-тест из шага 2 проверяет их), хелпер `_service_with_failing_retriever` (Task 4 шаг 2 — стиль задан соседними тестами файла, а требование «разрешение repo/branch должно проходить» названо явно, потому что иначе тест зелен по построению) и способ сделать Voyage недоступным (Task 7 шаг 5 — зависит от окружения и не должен фиксироваться ценой порчи конфига). Заполнение ключа `failure` по веткам `index_batch` (Task 3 шаг 3) задано правилом, а не построчным диффом: число веток видно только в файле, а пропуск любой из них немедленно роняет тест шага 1 на `KeyError`.

**Согласованность типов.** `failure` (Task 3) читается в Task 5 через ключ свода `embedder_failed`, а не напрямую. `SOURCE_EMBEDDER` и `EMBEDDER_REASON` объявлены в Task 2, используются в Task 5. `_source_gap` заменяет `_storage_gap` в Task 2 целиком — все три вызова перечислены (шаги 6 и 7), и шаг 7 требует убедиться `grep`'ом, что старого имени не осталось. `strict` keyword-only в обеих точках Task 4. Фикстуры во всех тестах — существующие: `FakeDeps`, `_FakeStore`/`_FakeGraph`/`_FakeEmbedder`/`_brief`/`_TimingOutStore`, `FakeProvider`/`FakeTaskService`/`FakeMeta`/`_raw`, `assemble`; хелперы `_build`/`_gap` заводятся один раз в Task 2 и переиспользуются в Task 5.

## Резолв стратегии исполнения

Рубрика `auto`, первое совпадение выигрывает:

1. **Риск-сигнал присутствует** — изменение публичного контракта MCP-тула (поле `gaps[].cause` в ответе `prepare_task_context`). Правило 1 срабатывает сразу.

Дополнительно: 7 задач и 10 файлов — по объёму тоже у границы.

**Стратегия: `subagent`** — `superpowers:subagent-driven-development`, свежий субагент на задачу с проверкой между задачами.
