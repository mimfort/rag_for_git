# PRI-234 — fail-soft чтения коммиченного `.review.yml` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сбой чтения коммиченного слоя политики (сеть, токен, 404, битый YAML) больше не обнуляет домашние слои `home:review.yml` и `home:repos/<owner>/<name>.yml`, а фиксируется структурно и бессекретно; пути ревью, индексации и миграции остаются громкими.

**Architecture:** В `reviewer/config/layers.py` рядом с существующим `merge_home()` появляется `merge_committed()` с двумя раздельными `try` (доставка / разбор). Форму исключения фетчера классифицирует новая чистая функция `classify_fetch_error` из нового модуля `reviewer/config/fetch_errors.py`, не читая текст исключения. Пропущенные слои попадают в новое поле `ResolutionMeta.skipped: tuple[SkippedLayer, ...]`. Строгость коммиченного слоя управляется новым keyword-флагом `strict_committed` (дефолт `False`), который явно выставлен в каждой из пяти точек вызова.

**Tech Stack:** Python 3, pytest, click, pyyaml, dataclasses. Никаких новых зависимостей.

## Global Constraints

- Спека: `docs/superpowers/specs/2026-08-10-pri-234-policy-layers-fail-soft-design.md`. Бриф: `docs/superpowers/briefs/2026-08-10-PRI-234-vcs-failure-must-not-drop-local-policy-layers.md`.
- Язык проекта — русский: комментарии, докстринги, сообщения CLI. Докстринги существующих функций в `layers.py` — на английском (`"""Resolve global home, committed, and repository home policy layers."""`); новый код пишется по-русски, существующие английские докстринги не переводятся.
- Коммиты — Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`, упоминаний Claude).
- Рабочая ветка — `dev`. Перед началом: `git rev-parse --abbrev-ref HEAD` должно вернуть `dev`.
- Unit-тесты запрещено пускать в сеть и на localhost-сокеты. Все VCS-фетчеры в тестах — фейки/`MagicMock`.
- Запуск тестов: `.venv/bin/pytest -q` (integration-тесты исключены по умолчанию через `addopts` в `pyproject.toml`).
- Линт: `.venv/bin/ruff check <изменённые файлы>`. Репозиторий **не** чист от ruff целиком — проверять только свои файлы, repo-wide clean не гнаться.
- **Секреты:** ни `str(exc)`, ни `repr(exc)`, ни `exc.args`, ни `request.url` не попадают ни в `warnings`, ни в `skipped`, ни в текст исключения строгого режима. Разрешены к выводу: имя слоя, `repo` (`owner/name`), `ref`, категория, транспорт, HTTP-код.
- Категории `SkippedLayer.category`: `"unavailable"` (сбой доставки), `"malformed"` (текст получен, но не разбирается), `"invalid"` (недопустимое значение известного policy-ключа), `"credential"` (запрещённый credential-ключ).
- Транспорты `classify_fetch_error`: `"http"`, `"timeout"`, `"connection"`, `"unknown"`.
- `resolve_policy_data` не меняет контракт фетчера: `Callable[[str], str | None]` (смежная задача PRI-235 подставит локальный фетчер поверх).
- Версия пакета и содержимое `plugin/` в этой работе **не** трогаются — пересборка codex-манифестов не требуется.

---

### Task 1: Классификатор сбоя фетчера

**Files:**
- Create: `reviewer/config/fetch_errors.py`
- Test: `tests/config/test_fetch_errors.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `classify_fetch_error(exc: BaseException) -> tuple[str, int | None]` — возвращает `(transport, http_status)`, где `transport` ∈ `{"http", "timeout", "connection", "unknown"}`, а `http_status` — int в диапазоне 100–599 или `None`. Используется в Task 3.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/config/test_fetch_errors.py`:

```python
"""Классификатор сбоя фетчера коммиченного слоя (PRI-234).

Ключевой инвариант: решение принимается по ФОРМЕ исключения (атрибут
response.status_code, имена классов в MRO), а не по его тексту — там живут
URL и токен VCS-клиента.
"""
from reviewer.config.fetch_errors import classify_fetch_error


class _Response:
    def __init__(self, status_code) -> None:
        self.status_code = status_code


class _HTTPStatusError(Exception):
    """Форма httpx.HTTPStatusError: есть .response и .request с секретами."""

    def __init__(self, message: str, status_code) -> None:
        super().__init__(message)
        self.response = _Response(status_code)


class _ConnectTimeout(Exception):
    pass


class _ConnectError(Exception):
    pass


def test_http_status_is_extracted_from_response():
    assert classify_fetch_error(_HTTPStatusError("boom", 404)) == ("http", 404)
    assert classify_fetch_error(_HTTPStatusError("boom", 401)) == ("http", 401)


def test_timeout_wins_over_connection_in_class_name():
    assert classify_fetch_error(_ConnectTimeout("boom")) == ("timeout", None)


def test_connection_error_is_classified_by_class_name():
    assert classify_fetch_error(_ConnectError("boom")) == ("connection", None)
    assert classify_fetch_error(ConnectionError("boom")) == ("connection", None)


def test_unknown_exception_falls_back_to_unknown():
    assert classify_fetch_error(RuntimeError("boom")) == ("unknown", None)


def test_non_integer_and_out_of_range_status_is_ignored():
    # bool — подкласс int: не должен пролезать как HTTP-код.
    assert classify_fetch_error(_HTTPStatusError("boom", True)) == ("unknown", None)
    assert classify_fetch_error(_HTTPStatusError("boom", "404")) == ("unknown", None)
    assert classify_fetch_error(_HTTPStatusError("boom", 999)) == ("unknown", None)


def test_result_never_carries_exception_text():
    secret = "do-not-echo-token-xyz"
    exc = _HTTPStatusError(f"GET https://api/x?token={secret} -> 403", 403)
    exc.request = type("R", (), {"url": f"https://api/x?token={secret}"})()

    result = classify_fetch_error(exc)

    assert result == ("http", 403)
    assert secret not in repr(result)
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv/bin/pytest tests/config/test_fetch_errors.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.config.fetch_errors'`

- [ ] **Step 3: Написать минимальную реализацию**

Создать `reviewer/config/fetch_errors.py`:

```python
"""Классификация сбоя фетчера коммиченного слоя политики (PRI-234).

Модуль намеренно без зависимостей: `layers.py` не должен знать форму
HTTP-исключений VCS-клиента, а чистая функция тестируется на отсутствие
секретов в результате напрямую.
"""
from __future__ import annotations

TRANSPORT_HTTP = "http"
TRANSPORT_TIMEOUT = "timeout"
TRANSPORT_CONNECTION = "connection"
TRANSPORT_UNKNOWN = "unknown"


def _http_status(exc: BaseException) -> int | None:
    """HTTP-код из атрибута response, если он похож на настоящий код."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    # bool — подкласс int: True прошёл бы isinstance-проверку и стал бы «кодом 1».
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return status if 100 <= status <= 599 else None


def classify_fetch_error(exc: BaseException) -> tuple[str, int | None]:
    """Вернуть (transport, http_status) по форме исключения фетчера.

    Ни текст исключения, ни его args, ни request.url не читаются: там живут
    URL и токен VCS-клиента. Решение принимается только по атрибуту
    response.status_code и именам классов в MRO — они безопасны.
    """
    status = _http_status(exc)
    if status is not None:
        return TRANSPORT_HTTP, status
    names = " ".join(klass.__name__ for klass in type(exc).__mro__).lower()
    # Порядок важен: ConnectTimeout содержит и "connect", и "timeout".
    if "timeout" in names:
        return TRANSPORT_TIMEOUT, None
    if "connect" in names or "network" in names:
        return TRANSPORT_CONNECTION, None
    if "httpstatus" in names:
        return TRANSPORT_HTTP, None
    return TRANSPORT_UNKNOWN, None
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `.venv/bin/pytest tests/config/test_fetch_errors.py -q`
Expected: PASS, 6 passed

Run: `.venv/bin/ruff check reviewer/config/fetch_errors.py tests/config/test_fetch_errors.py`
Expected: `All checks passed!`

- [ ] **Step 5: Коммит**

```bash
git add reviewer/config/fetch_errors.py tests/config/test_fetch_errors.py
git commit -m "feat(config): бессекретный классификатор сбоя фетчера политики"
```

---

### Task 2: `SkippedLayer` и поле `ResolutionMeta.skipped`

**Files:**
- Modify: `reviewer/config/layers.py:36-47` (объявление `ResolutionMeta`), `reviewer/config/layers.py:284-333` (тело `resolve_policy_data`)
- Test: `tests/config/test_layers.py`

**Interfaces:**
- Consumes: ничего из Task 1.
- Produces:
  - `SkippedLayer(layer: str, repo: str, ref: str | None, category: str, transport: str | None = None, http_status: int | None = None)` с методом `as_dict() -> dict[str, object]`.
  - `ResolutionMeta.skipped: tuple[SkippedLayer, ...] = ()`, ключ `"skipped"` в `ResolutionMeta.as_dict()` (список словарей).
  - Внутреннее замыкание `record_skip(layer, category, *, ref_value=None, transport=None, http_status=None)` внутри `resolve_policy_data` — используется в Task 3.

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/config/test_layers.py`:

```python
def test_home_credential_key_is_recorded_as_skipped_layer(tmp_path):
    """PRI-234: пропуск домашнего слоя виден не только строкой в warnings."""
    _write(tmp_path / "repos/o/r.yml", "paths: {ignore: [x]}\ngithub_token: t\n")

    data, meta = resolve_policy_data(
        "o/r", "main", lambda _ref: "max_comments: 5\n", config_root=tmp_path
    )

    assert data == {"max_comments": 5}
    assert [item.as_dict() for item in meta.skipped] == [
        {
            "layer": "home:repos/o/r.yml",
            "repo": "o/r",
            "ref": None,
            "category": "credential",
            "transport": None,
            "http_status": None,
        }
    ]


def test_invalid_home_policy_value_is_recorded_as_skipped_layer(tmp_path):
    # Значение взято из существующего
    # test_invalid_home_task_sync_filter_is_quarantined_or_rejected: оно
    # гарантированно даёт HomePolicyError.
    _write(
        tmp_path / "review.yml",
        "task_board:\n  project: PRI\n  sync_filter: {max_age_days: 0}\n",
    )

    _data, meta = resolve_policy_data(
        "o/r", "main", lambda _ref: "max_comments: 5\n", config_root=tmp_path
    )

    assert [(item.layer, item.category) for item in meta.skipped] == [
        ("home:review.yml", "invalid")
    ]


def test_resolution_meta_as_dict_exposes_skipped_and_defaults_to_empty():
    meta = ResolutionMeta(sources={}, shadowed={}, warnings=())

    assert meta.skipped == ()
    assert meta.as_dict()["skipped"] == []
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/config/test_layers.py -q -k skipped`
Expected: FAIL — `AttributeError: 'ResolutionMeta' object has no attribute 'skipped'`

- [ ] **Step 3: Реализовать**

В `reviewer/config/layers.py` **перед** объявлением `ResolutionMeta` (сейчас строка 36-37) добавить:

```python
@dataclass(frozen=True)
class SkippedLayer:
    """Слой политики, который существовал (или мог существовать), но не применён.

    Отсутствие слоя записи не создаёт: фетчер, вернувший None, означает «слоя
    нет в репозитории», а запись здесь — «слой не прочитан» (PRI-234).
    """

    layer: str
    repo: str
    ref: str | None
    category: str
    transport: str | None = None
    http_status: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "layer": self.layer,
            "repo": self.repo,
            "ref": self.ref,
            "category": self.category,
            "transport": self.transport,
            "http_status": self.http_status,
        }
```

В `ResolutionMeta` добавить поле с дефолтом и ключ в `as_dict()`:

```python
@dataclass(frozen=True)
class ResolutionMeta:
    sources: dict[str, str]
    shadowed: dict[str, tuple[str, ...]]
    warnings: tuple[str, ...]
    # Дефолт обязателен: frozen dataclass без него сломал бы _empty_meta()
    # и существующие конструкторы в тестах.
    skipped: tuple[SkippedLayer, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "sources": dict(self.sources),
            "shadowed": {key: list(value) for key, value in self.shadowed.items()},
            "warnings": list(self.warnings),
            "skipped": [item.as_dict() for item in self.skipped],
        }
```

В `resolve_policy_data` рядом с `warnings: list[str] = []` (строка 275) добавить накопитель и замыкание:

```python
    skipped: list[SkippedLayer] = []

    def record_skip(
        layer: str,
        category: str,
        *,
        ref_value: str | None = None,
        transport: str | None = None,
        http_status: int | None = None,
    ) -> None:
        skipped.append(
            SkippedLayer(layer, repo, ref_value, category, transport, http_status)
        )
```

В `merge_home` дописать `record_skip` в три существующие ветки обработки (домашние слои читаются без ref, поэтому `ref_value` не передаётся):

```python
        except HomeCredentialError as exc:
            record_skip(source, "credential")
            warnings.append(str(exc))
        except HomePolicyError as exc:
            record_skip(source, "invalid")
            if strict_home:
                raise
            warnings.append(str(exc))
        except (
            OSError,
            UnicodeError,
            RecursionError,
            yaml.YAMLError,
            HomeConfigError,
        ) as exc:
            wrapped = HomeConfigError(
                f"{source}: конфиг не прочитан: {type(exc).__name__}"
            )
            record_skip(source, "malformed")
            if strict_home:
                raise wrapped from None
            warnings.append(str(wrapped))
```

В `return` дописать поле:

```python
    return merged, ResolutionMeta(
        sources=dict(sources),
        shadowed={key: tuple(value) for key, value in shadowed.items()},
        warnings=tuple(warnings),
        skipped=tuple(skipped),
    )
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `.venv/bin/pytest tests/config/test_layers.py -q`
Expected: PASS — все тесты файла, включая три новых

Run: `.venv/bin/ruff check reviewer/config/layers.py tests/config/test_layers.py`
Expected: `All checks passed!`

- [ ] **Step 5: Коммит**

```bash
git add reviewer/config/layers.py tests/config/test_layers.py
git commit -m "feat(config): структурный учёт пропущенных слоёв политики в ResolutionMeta"
```

---

### Task 3: Fail-soft чтение коммиченного слоя и флаг `strict_committed`

**Files:**
- Modify: `reviewer/config/layers.py:257-333` (`resolve_policy_data`)
- Test: `tests/config/test_layers.py`

**Interfaces:**
- Consumes: `classify_fetch_error` из Task 1; `SkippedLayer`, `record_skip` из Task 2.
- Produces: `resolve_policy_data(repo, ref, fetch_repo_yaml, *, config_root=None, strict_home=False, strict_committed=False)` — новый keyword-флаг используется в Task 4 и Task 5.

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/config/test_layers.py`:

```python
def test_committed_fetch_failure_keeps_both_home_layers(tmp_path):
    """PRI-234, критерий 2: сбой доставки коммиченного слоя не обнуляет home."""
    _write(tmp_path / "review.yml", "max_comments: 5\ncontext_limits: {graph: {hops: 2}}\n")
    _write(tmp_path / "repos/o/r.yml", "paths: {ignore: [home-repo]}\ntask_board:\n")

    def boom(_ref):
        raise RuntimeError("сеть недоступна")

    data, meta = resolve_policy_data("o/r", "main", boom, config_root=tmp_path)

    assert data["paths"] == {"ignore": ["home-repo"]}
    assert data["max_comments"] == 5
    assert data["context_limits"] == {"graph": {"hops": 2}}
    assert data["task_board"] is None
    assert len(meta.warnings) == 1
    assert [item.as_dict() for item in meta.skipped] == [
        {
            "layer": ".review.yml",
            "repo": "o/r",
            "ref": "main",
            "category": "unavailable",
            "transport": "unknown",
            "http_status": None,
        }
    ]


def test_committed_fetch_failure_is_loud_in_strict_committed_mode(tmp_path):
    _write(tmp_path / "repos/o/r.yml", "paths: {ignore: [home-repo]}\n")

    def boom(_ref):
        raise RuntimeError("сеть недоступна")

    with pytest.raises(HomeConfigError):
        resolve_policy_data(
            "o/r", "main", boom, config_root=tmp_path, strict_committed=True
        )


def test_committed_http_failure_reports_status_and_transport(tmp_path):
    class _Response:
        status_code = 403

    class _HTTPStatusError(Exception):
        response = _Response()

    def boom(_ref):
        raise _HTTPStatusError("forbidden")

    _data, meta = resolve_policy_data("o/r", "sha123", boom, config_root=tmp_path)

    assert meta.skipped[0].transport == "http"
    assert meta.skipped[0].http_status == 403
    assert meta.skipped[0].ref == "sha123"


def test_malformed_committed_yaml_is_soft_and_categorized(tmp_path):
    _write(tmp_path / "repos/o/r.yml", "max_comments: 5\n")

    data, meta = resolve_policy_data(
        "o/r", "main", lambda _ref: "paths: [broken\n", config_root=tmp_path
    )

    assert data == {"max_comments": 5}
    assert [(item.layer, item.category) for item in meta.skipped] == [
        (".review.yml", "malformed")
    ]
    with pytest.raises(HomeConfigError):
        resolve_policy_data(
            "o/r",
            "main",
            lambda _ref: "paths: [broken\n",
            config_root=tmp_path,
            strict_committed=True,
        )


def test_absent_committed_layer_records_nothing(tmp_path):
    """Фетчер вернул None: «слоя нет» — не то же, что «слой не прочитан»."""
    _write(tmp_path / "repos/o/r.yml", "max_comments: 5\n")

    data, meta = resolve_policy_data(
        "o/r", "main", lambda _ref: None, config_root=tmp_path
    )

    assert data == {"max_comments": 5}
    assert meta.skipped == ()
    assert meta.warnings == ()


def test_committed_failure_diagnostic_never_echoes_url_or_token(tmp_path):
    secret = "do-not-echo-token-xyz"

    class _Request:
        url = f"https://api.example/repos/o/r?token={secret}"

    class _HTTPStatusError(Exception):
        request = _Request()

    def boom(_ref):
        raise _HTTPStatusError(f"GET {_Request.url} -> 500")

    _data, meta = resolve_policy_data("o/r", "main", boom, config_root=tmp_path)

    rendered = " ".join(meta.warnings) + repr([i.as_dict() for i in meta.skipped])
    assert secret not in rendered
    assert "https://" not in rendered

    with pytest.raises(HomeConfigError) as excinfo:
        resolve_policy_data(
            "o/r", "main", boom, config_root=tmp_path, strict_committed=True
        )
    assert secret not in str(excinfo.value)
    assert secret not in "".join(traceback.format_exception(excinfo.value))
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/config/test_layers.py -q -k "committed_fetch or malformed_committed or absent_committed or committed_http or committed_failure"`
Expected: FAIL — `RuntimeError: сеть недоступна` пробрасывается наружу, `strict_committed` не является допустимым аргументом

- [ ] **Step 3: Реализовать**

В `reviewer/config/layers.py` добавить импорт рядом с остальными импортами `reviewer.config.*` (строки 19-21):

```python
from reviewer.config.fetch_errors import classify_fetch_error
```

Расширить сигнатуру и докстринг `resolve_policy_data`:

```python
def resolve_policy_data(
    repo: str,
    ref: str,
    fetch_repo_yaml: Callable[[str], str | None],
    *,
    config_root: Path | None = None,
    strict_home: bool = False,
    strict_committed: bool = False,
) -> tuple[dict[str, object], ResolutionMeta]:
    """Resolve global home, committed, and repository home policy layers.

    `strict_committed` управляет ТОЛЬКО коммиченным слоем и намеренно отделён
    от `strict_home`: `config show` вызывает резолвер со `strict_home=True`, и
    расширение того флага заставило бы его падать там, где он обязан печатать
    эффективную политику из домашних слоёв (PRI-234).
    """
```

Заменить строки 319-326 (чтение коммиченного слоя) вызовом новой функции. Саму функцию объявить после `merge_home`, перед строкой `merge_home(root / "review.yml", "home:review.yml")`:

```python
    def merge_committed() -> None:
        """Прочитать коммиченный слой, не обнуляя уже смерженные домашние.

        Два раздельных try: сбой доставки (сеть, токен, 404) и сбой разбора
        уже полученного текста — разные категории, но обе fail-soft, как у
        merge_home. Диагностик собирается только из структурированных полей:
        текст исключения VCS-клиента содержит URL и токен.
        """
        def fail(category: str, transport: str | None, http_status: int | None) -> None:
            record_skip(
                ".review.yml",
                category,
                ref_value=ref,
                transport=transport,
                http_status=http_status,
            )
            message = (
                f".review.yml: слой не прочитан (repo={repo}, ref={ref}, "
                f"category={category}, transport={transport}, "
                f"http_status={http_status})"
            )
            warnings.append(message)
            if strict_committed:
                # from None обязателен: цепочка исключений вынесла бы URL
                # VCS-клиента в трейсбек.
                raise HomeConfigError(message) from None

        try:
            text = fetch_repo_yaml(ref)
        except Exception as exc:  # noqa: BLE001 — сбой доставки слоя не должен
            # уничтожать резолв целиком (PRI-234)
            transport, http_status = classify_fetch_error(exc)
            fail("unavailable", transport, http_status)
            return
        try:
            committed = _read_mapping(text, ".review.yml")
        except (HomeConfigError, RecursionError, UnicodeError):
            fail("malformed", None, None)
            return
        if BRANCHES_KEY in committed:
            committed = {k: v for k, v in committed.items() if k != BRANCHES_KEY}
            warnings.append(
                ".review.yml: ключ repository игнорируется "
                "(ветки задаются домашним слоем, см. reviewer config show)"
            )
        merge(committed, ".review.yml")
```

И заменить старый блок на один вызов:

```python
    merge_home(root / "review.yml", "home:review.yml")
    merge_committed()
    repo_source = f"home:repos/{repo}.yml"
    merge_home(home_repo_path(repo, root), repo_source)
```

Важно: `_read_mapping` сам переводит `yaml.YAMLError` в `HomeConfigError`, поэтому `yaml.YAMLError` в `except` не нужен; `fetch_repo_yaml`, вернувший `None`, даёт `{}` без исключения — записи в `skipped` не будет.

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `.venv/bin/pytest tests/config/test_layers.py -q`
Expected: PASS — весь файл

Run: `.venv/bin/ruff check reviewer/config/layers.py tests/config/test_layers.py`
Expected: `All checks passed!`

- [ ] **Step 5: Коммит**

```bash
git add reviewer/config/layers.py tests/config/test_layers.py
git commit -m "fix(config): сбой чтения коммиченного .review.yml не обнуляет домашние слои"
```

---

### Task 4: Явный режим в каждой точке вызова

**Files:**
- Modify: `reviewer/services/review_service.py:222-226`
- Modify: `reviewer/entrypoints/cli.py:813-817`
- Modify: `reviewer/config/layers.py:770-772`, `reviewer/config/layers.py:926-928`
- Modify: `reviewer/mcp/service.py:1370-1374`
- Test: `tests/services/test_review_service.py`, `tests/config/test_layers.py`, `tests/mcp/test_context_limits_wiring.py`

**Interfaces:**
- Consumes: `strict_committed` из Task 3.
- Produces: поведенческий контракт — ревью PR, `reviewer index` и миграция громкие; MCP и `config show` мягкие.

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/services/test_review_service.py`:

```python
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_is_loud_when_committed_policy_cannot_be_read(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """PRI-234: тихая потеря политики в ревью недопустима — prepare падает."""
    from reviewer.config.layers import HomeConfigError

    vcs = _vcs_with_files([_changed("a.py")])

    def fetch(path, ref):
        if path == ".review.yml":
            raise RuntimeError("сеть недоступна")
        return "def foo(): pass"

    vcs.get_file_at_ref.side_effect = fetch
    service = ReviewService(settings, components)

    with pytest.raises(HomeConfigError):
        service.prepare("owner", "repo", 1, vcs_provider=vcs)
```

Добавить в конец `tests/config/test_layers.py`:

```python
def test_migration_is_loud_when_committed_layer_cannot_be_read(tmp_path):
    """Перенос неполной политики в home-файл необратим — миграция громкая."""
    def boom(_ref):
        raise RuntimeError("сеть недоступна")

    with pytest.raises(HomeConfigError):
        migrate_repo_config(
            "o/r", "main", boom, config_root=tmp_path, settings=Settings(_env_file=None)
        )
```

Добавить в конец `tests/mcp/test_context_limits_wiring.py` — это прямая проверка критерия приёмки №2 для MCP-пути:

```python
def test_home_repo_limits_survive_unavailable_vcs(isolated_xdg_config_home) -> None:
    """PRI-234: при недоступном VCS лимиты берутся из home-слоя, а не из env-дефолтов."""
    path = isolated_xdg_config_home / "rag-reviewer/repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("context_limits: {graph: {hops: 2}}\n", encoding="utf-8")
    s = _settings()
    components = MagicMock()
    vcs = MagicMock()
    vcs.get_file_at_ref.side_effect = RuntimeError("network down")
    svc = MCPReviewService(s, components, vcs_factory=lambda o, n: vcs)

    cl = svc._resolve_context_limits("o/r", "dev")

    # Дефолт graph.hops — 1; значение 2 доказывает, что home-слой применён,
    # хотя коммиченный слой недоступен.
    assert cl.graph.hops == 2
```

Существующий `test_resolve_context_limits_failsoft_returns_defaults` (`tests/mcp/test_context_limits_wiring.py:23`) остаётся зелёным: он не создаёт домашний слой, поэтому дефолты по-прежнему корректны.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/services/test_review_service.py -q -k loud tests/config/test_layers.py -q -k migration_is_loud`
Если объединённый запуск неудобен, выполнить по одному:
Run: `.venv/bin/pytest tests/services/test_review_service.py -k loud -q`
Expected: FAIL — поднимается `RuntimeError`, а не `HomeConfigError` (после Task 3 сбой стал мягким, и `prepare` не падает вовсе)
Run: `.venv/bin/pytest tests/config/test_layers.py -k migration_is_loud -q`
Expected: FAIL — `migrate_repo_config` не падает либо падает другим исключением
Run: `.venv/bin/pytest tests/mcp/test_context_limits_wiring.py -k unavailable_vcs -q`
Expected: FAIL — `assert 1 == 2`: сбой VCS всё ещё выбрасывает исключение, и `_resolve_context_limits` откатывается на дефолт-константы

- [ ] **Step 3: Реализовать**

`reviewer/services/review_service.py:222` — добавить флаг и комментарий:

```python
            # Политика резолвится по точному base-коммиту PR и переиспользуется
            # для досинка base-индекса, overlay и дальнейшего гейта.
            # strict_committed: в ревью тихая потеря политики недопустима —
            # неполный gate пропустил бы находки, которые репозиторий отключил.
            policy_data, policy_meta = resolve_policy_data(
                repo,
                prq.base_sha,
                lambda ref: vcs.get_file_at_ref(".review.yml", ref),
                strict_committed=True,
            )
```

`reviewer/entrypoints/cli.py:813` — добавить флаг и комментарий:

```python
        # strict_committed: неполный paths.ignore залил бы в индекс файлы,
        # которые репозиторий исключил; фетчер здесь локальный (file_at_ref),
        # его сбой означает битый клон, а не недоступную сеть.
        policy_data, policy_meta = resolve_policy_data(
            repo_id,
            ref,
            lambda selected_ref: file_at_ref(repo, ".review.yml", selected_ref),
            strict_committed=True,
        )
```

`reviewer/config/layers.py:770` (в `_existing_migration_result`) и `:926` (в `migrate_repo_config`) — добавить флаг к обоим вызовам:

```python
    data, meta = resolve_policy_data(
        repo, ref, fetch_repo_yaml, config_root=root,
        strict_home=True, strict_committed=True,
    )
```

```python
    before_data, before_meta = resolve_policy_data(
        repo, ref, fetch_snapshot, config_root=root,
        strict_home=True, strict_committed=True,
    )
```

Внимание: в `migrate_repo_config` строка 903 (`source_text = fetch_repo_yaml(ref)`) выполняется **до** резолва и уже сейчас пробрасывает исключение фетчера — поэтому тест миграции падает на ней; это ожидаемое громкое поведение, но исключение будет `RuntimeError`, а не `HomeConfigError`. Чтобы миграция давала единый бессекретный диагностик, обернуть эту строку:

```python
    try:
        source_text = fetch_repo_yaml(ref)
    except Exception as exc:  # noqa: BLE001 — единый бессекретный диагностик
        transport, http_status = classify_fetch_error(exc)
        raise HomeConfigError(
            f".review.yml: слой не прочитан (repo={repo}, ref={ref}, "
            f"category=unavailable, transport={transport}, "
            f"http_status={http_status})"
        ) from None
```

`reviewer/mcp/service.py:1370` — явный мягкий режим с обоснованием:

```python
            # strict_committed=False намеренно: у _resolve_policy четыре
            # потребителя, и три из них (_resolve_summary_depth,
            # _resolve_summary_topk_threshold, _resolve_context_limits) уже
            # обёрнуты в собственный fail-soft с откатом на env-дефолты.
            # Строгий режим здесь заставил бы их молча потерять
            # home:repos/<repo>.yml — то есть воспроизвёл бы исходный баг
            # PRI-234 этажом выше. Ревью остаётся громким за счёт
            # ReviewService.prepare.
            data, meta = resolve_policy_data(
                repo,
                branch,
                lambda ref: vcs.get_file_at_ref(".review.yml", ref),
                strict_committed=False,
            )
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `.venv/bin/pytest tests/services/ tests/config/ tests/mcp/ -q`
Expected: PASS — включая все три новых теста

Run: `.venv/bin/ruff check reviewer/services/review_service.py reviewer/entrypoints/cli.py reviewer/config/layers.py reviewer/mcp/service.py`
Expected: `All checks passed!`

- [ ] **Step 5: Коммит**

```bash
git add reviewer/services/review_service.py reviewer/entrypoints/cli.py reviewer/config/layers.py reviewer/mcp/service.py tests/services/test_review_service.py tests/config/test_layers.py tests/mcp/test_context_limits_wiring.py
git commit -m "feat(config): явный strict_committed в каждой точке резолва политики"
```

---

### Task 5: `config show` печатает эффективную политику и пропущенные слои

**Files:**
- Modify: `reviewer/entrypoints/cli.py:125-152` (`_render_config_report`), `reviewer/entrypoints/cli.py:199-231` (`config_show`)
- Modify: `reviewer/config/layers.py:450-472` (`build_config_report`)
- Test: `tests/entrypoints/test_cli_config_show_branches.py`, `tests/entrypoints/test_config_commands.py`

**Interfaces:**
- Consumes: `ResolutionMeta.skipped` (Task 2), мягкий режим по умолчанию (Task 3).
- Produces: ключ `"skipped"` в payload `config show` (список словарей формы `SkippedLayer.as_dict()`); код возврата `1` при `policy_error` или непустом `skipped`.

- [ ] **Step 1: Написать падающие тесты**

Переписать два теста в `tests/entrypoints/test_cli_config_show_branches.py`.

Заменить тело `test_branches_shown_even_when_policy_part_fails` (строки 8-35) на:

```python
def test_home_layers_survive_unavailable_vcs(tmp_path, monkeypatch):
    """PRI-234, критерий 1: недоступный VCS больше не обнуляет вывод политики."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    path = tmp_path / "rag-reviewer" / "repos" / "o" / "r.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "repository:\n  primary_branch: dev\n  index_branches: [dev, main]\n"
        "max_comments: 3\n",
        encoding="utf-8",
    )

    def boom(*args, **kwargs):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(
        "reviewer.services.review_service.ReviewService._create_vcs_provider", boom
    )
    result = CliRunner().invoke(cli, ["config", "show", "--repo", "o/r", "--json"])
    payload = json.loads(result.output)

    assert payload["branches"] == {
        "primary": "dev",
        "index": ["dev", "main"],
        "source": "home:repos/o/r.yml",
    }
    assert "policy_error" not in payload
    # Домашний слой применён, хотя коммиченный недоступен.
    assert payload["effective"]["max_comments"] == 3
    assert payload["sources"]["max_comments"] == "home:repos/o/r.yml"
    assert payload["skipped"] == [
        {
            "layer": ".review.yml",
            "repo": "o/r",
            "ref": "dev",
            "category": "unavailable",
            "transport": "unknown",
            "http_status": None,
        }
    ]
    # Политика неполная — сигнал внешним скриптам сохраняется.
    assert result.exit_code != 0
```

Заменить тело `test_policy_error_does_not_echo_raw_exception_text` (строки 38-55) на:

```python
def test_skipped_layer_does_not_echo_raw_exception_text(tmp_path, monkeypatch):
    """Диагностик пропущенного слоя структурный: ни str(exc), ни URL/токен."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    secret = "do-not-echo-token-xyz"

    def boom(*args, **kwargs):
        raise RuntimeError(f"https://api.example/x?token={secret}")

    monkeypatch.setattr(
        "reviewer.services.review_service.ReviewService._create_vcs_provider", boom
    )
    result = CliRunner().invoke(cli, ["config", "show", "--repo", "o/r", "--json"])
    payload = json.loads(result.output)

    assert payload["skipped"][0]["layer"] == ".review.yml"
    assert payload["skipped"][0]["category"] == "unavailable"
    assert secret not in result.output
    assert "https://" not in result.output
```

Добавить туда же новый тест текстового рендера:

```python
def test_config_show_text_output_prints_effective_and_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    path = tmp_path / "rag-reviewer" / "review.yml"
    path.parent.mkdir(parents=True)
    path.write_text("max_comments: 3\n", encoding="utf-8")

    def boom(*args, **kwargs):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(
        "reviewer.services.review_service.ReviewService._create_vcs_provider", boom
    )
    result = CliRunner().invoke(cli, ["config", "show", "--repo", "o/r"])

    assert "branches:" in result.output
    assert "max_comments: 3" in result.output
    assert "skipped: .review.yml" in result.output
    assert "category=unavailable" in result.output


def test_skipped_home_credential_layer_also_sets_exit_code(tmp_path, monkeypatch):
    """Осознанное изменение: credential-ключ в home-слое теперь даёт код 1.

    Раньше он был warning с кодом 0, но это то же событие — слой не применён.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    path = tmp_path / "rag-reviewer" / "review.yml"
    path.parent.mkdir(parents=True)
    path.write_text("github_token: t\n", encoding="utf-8")

    def fake_provider(self, owner, name):
        class _V:
            def get_file_at_ref(self, _path, ref):
                return "max_comments: 5\n"

            def close(self):
                return None

        return _V()

    monkeypatch.setattr(
        "reviewer.services.review_service.ReviewService._create_vcs_provider",
        fake_provider,
    )
    result = CliRunner().invoke(cli, ["config", "show", "--repo", "o/r", "--json"])
    payload = json.loads(result.output)

    assert payload["skipped"][0]["layer"] == "home:review.yml"
    assert payload["skipped"][0]["category"] == "credential"
    assert payload["effective"]["max_comments"] == 5
    assert result.exit_code != 0
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_config_show_branches.py -q`
Expected: FAIL — `KeyError: 'skipped'` / в payload всё ещё `policy_error`

- [ ] **Step 3: Реализовать**

`reviewer/config/layers.py` — в `build_config_report` добавить ключ:

```python
    return {
        "repo": normalize_repo(repo),
        "branch": branch,
        "effective": effective,
        "sources": sources,
        "shadowed": {key: list(value) for key, value in meta.shadowed.items()},
        "warnings": list(meta.warnings),
        "skipped": [item.as_dict() for item in meta.skipped],
    }
```

`reviewer/entrypoints/cli.py` — в `_render_config_report` после цикла по warnings добавить блок:

```python
    for warning in report["warnings"]:
        click.echo(f"warning: {warning}")
    for item in report.get("skipped") or ():
        assert isinstance(item, Mapping)
        click.echo(
            f"skipped: {item['layer']} (repo={item['repo']}, ref={item['ref']}, "
            f"category={item['category']}, transport={item['transport']}, "
            f"http_status={item['http_status']})"
        )
```

`reviewer/entrypoints/cli.py` — в `config_show` заменить переподнятие `vcs_error` фетчером и обновить условие кода возврата:

```python
    try:
        with _config_context(repo, branch) as ctx:
            settings, _components, vcs, repo_id, ref, branches, vcs_error = ctx
            payload = _branches_report(branches)

            def fetch_committed(selected_ref: str) -> str | None:
                # Недоступный VCS-провайдер — такой же сбой доставки слоя, как
                # сетевая ошибка: он уходит в общий механизм skipped, а не в
                # отдельную ветку policy_error (PRI-234).
                if vcs_error is not None:
                    raise vcs_error
                return vcs.get_file_at_ref(".review.yml", selected_ref)

            try:
                data, meta = resolve_policy_data(
                    repo_id,
                    ref,
                    fetch_committed,
                    strict_home=True,
                )
                payload.update(build_config_report(repo_id, ref, settings, data, meta))
            except (HomeConfigError, yaml.YAMLError) as exc:
                # Тот же санитайзер, что и у остальных config-команд: не эхоить
                # сырой YAML/normalization payload исключения.
                payload["policy_error"] = _config_error_message(exc)
            except Exception as exc:  # noqa: BLE001 — диагностика не должна падать целиком
                # Прочие сбои — без текста исключения: он может содержать
                # URL/токены из VCS-клиента.
                payload["policy_error"] = type(exc).__name__
    except (HomeConfigError, yaml.YAMLError) as exc:
        raise click.ClickException(_config_error_message(exc)) from exc
    if as_json:
        click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        _render_config_report(payload)
    if "policy_error" in payload or payload.get("skipped"):
        # Эффективная политика неполная (или не собралась вовсе): вывод уже
        # напечатан, но код возврата обязан сигналить внешним скриптам
        # (`config show; echo $?`).
        raise SystemExit(1)
```

Докстринг `config_show` дополнить абзацем:

```python
    """Показать effective policy и происхождение её верхних ключей.

    Секция веток печатается всегда, даже если VCS недоступен (нет сети, нет
    токена) — резолв веток чисто локальный и не зависит от policy-части.
    Недоступный коммиченный `.review.yml` тоже больше не обнуляет вывод:
    печатаются домашние слои, а сам пропуск попадает в `skipped` (PRI-234).
    Код возврата при этом остаётся ненулевым.
    """
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `.venv/bin/pytest tests/entrypoints/ -q`
Expected: PASS

Существующие тесты `tests/entrypoints/test_config_commands.py:98` (`test_config_show_rejects_invalid_known_home_value_without_echoing_literal`) и `:185` (`test_config_show_sanitizes_committed_yaml_and_closes_every_resource`) должны пройти без изменений: первый бьёт по `strict_home=True` и по-прежнему даёт `policy_error`, второй теперь получает `category=malformed` в `skipped` — его ассерты (`exit_code != 0`, `"branches:"`, `".review.yml"`, отсутствие секрета) выполняются и в новом выводе. Если второй тест всё же падает, привести его комментарий и ассерты в соответствие с новым выводом, сохранив проверку отсутствия секрета.

Run: `.venv/bin/pytest -q`
Expected: PASS — вся unit-сюита

Run: `.venv/bin/ruff check reviewer/entrypoints/cli.py reviewer/config/layers.py tests/entrypoints/test_cli_config_show_branches.py`
Expected: `All checks passed!`

- [ ] **Step 5: Коммит**

```bash
git add reviewer/entrypoints/cli.py reviewer/config/layers.py tests/entrypoints/
git commit -m "feat(cli): config show печатает домашние слои и пропущенный коммиченный слой"
```

---

### Task 6: Документация

**Files:**
- Modify: `CLAUDE.md` (раздел «Неочевидные факты»)
- Modify: `README.md:692-701` (таблица «Common failures»)
- Modify: `README.ru.md:682-691` (таблица «Типовые сбои»)

**Interfaces:**
- Consumes: итоговое поведение из Tasks 3-5.
- Produces: ничего для кода.

- [ ] **Step 1: Дописать пункт в `CLAUDE.md`**

Добавить в раздел «Неочевидные факты (не выводятся из кода)» после пункта про мульти-бранч base-индекс:

```markdown
- **Сбой чтения коммиченного `.review.yml` не обнуляет домашние слои.** `resolve_policy_data`
  (`reviewer/config/layers.py`) читает коммиченный слой fail-soft: сбой доставки (сеть, токен, 404 —
  категория `unavailable`) и сбой разбора (`malformed`) пропускают слой, пишут структурную запись в
  `ResolutionMeta.skipped` и продолжают резолв, поэтому `home:review.yml` и
  `home:repos/<owner>/<name>.yml` применяются. Строгость включается отдельным флагом
  `strict_committed` и выставлена в `True` только там, где тихая потеря политики недопустима:
  ревью PR (`services/review_service.py`), `reviewer index` (`entrypoints/cli.py`) и миграция
  конфига (`config/layers.py`). MCP `_resolve_policy` намеренно мягкий — три его потребителя уже
  откатываются на env-дефолты, и строгий режим заставил бы их потерять домашний слой. Диагностик
  бессекретный: слой, репо, ref, категория, транспорт, HTTP-код — никаких URL, заголовков и
  токенов (классификация формы исключения — `config/fetch_errors.py`). `reviewer config show`
  печатает эффективные значения и блок `skipped`, но код возврата остаётся `1`.
```

- [ ] **Step 2: Дописать строку в таблицу `README.md`**

В таблицу «Common failures» (после строки `| PR is skipped | ... |`) добавить:

```markdown
| `config show` reports a `skipped` `.review.yml` layer and exits non-zero | The committed policy layer could not be fetched (no network/token, 404) or could not be parsed | Home layers are still applied — check the reported `category`/`http_status`; fix the remote or the committed YAML. Review, indexing, and migration stay loud and fail instead |
```

- [ ] **Step 3: Дописать строку в таблицу `README.ru.md`**

В таблицу «Типовые сбои» (после строки `| PR пропущен | ... |`) добавить:

```markdown
| `config show` показывает пропущенный слой `.review.yml` и ненулевой код возврата | Коммиченный слой политики не доставлен (нет сети/токена, 404) или не разбирается | Домашние слои всё равно применены — смотрите `category`/`http_status` в выводе; почините remote или коммиченный YAML. Ревью, индексация и миграция остаются громкими и падают |
```

- [ ] **Step 4: Проверить, что документация согласована с кодом**

Run: `grep -n "strict_committed" CLAUDE.md reviewer/config/layers.py reviewer/services/review_service.py reviewer/entrypoints/cli.py reviewer/mcp/service.py`
Expected: флаг упомянут в `CLAUDE.md` и присутствует во всех пяти точках вызова

Run: `.venv/bin/pytest -q`
Expected: PASS — вся unit-сюита

- [ ] **Step 5: Коммит**

```bash
git add CLAUDE.md README.md README.ru.md
git commit -m "docs(config): описать fail-soft чтения коммиченного слоя политики"
```

---

## Проверка перед завершением

- [ ] `.venv/bin/pytest -q` — зелёная unit-сюита
- [ ] `.venv/bin/ruff check` по всем изменённым файлам — чисто
- [ ] `git log --oneline dev..HEAD` — шесть коммитов, ни одного с self-attribution
- [ ] Все пять критериев приёмки из спеки выполнены и закрыты тестами
