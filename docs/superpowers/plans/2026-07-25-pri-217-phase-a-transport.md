# PRI-217 Фаза A — общий транспорт и параллелизуемые фикстуры

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Поднять переиспользуемый REST/GraphQL-транспорт, курсорную/офсетную пагинацию, YFM-конвертер и
параллелизуемые contract-фикстуры, чтобы восемь новых адаптеров досок не копировали обвязку `jira.py`.

**Architecture:** Новые модули в `reviewer/tasks/boards/` поверх существующего `BoardHttpClient`:
`restbase.py` (обобщение конструктора `JiraCloudBoard`), `pagination.py` (чистые генераторы страниц),
`graphql.py` (тонкий GraphQL-фасад для Linear), `yfm.py` (Yandex Tracker → markdown). Contract-фикстуры
разъезжаются из монолитного `tests/tasks/boards/contract.py` в `tests/tasks/boards/fakes/<type>.py`, а
пороги тестов переезжают из хардкода в поля `ProviderAdapter`.

**Tech Stack:** Python 3.11–3.13, httpx (+`httpx.MockTransport`), pytest, ruff (line-length 100).

## Global Constraints

- Язык проекта — русский: комментарии, докстринги, сообщения. Идентификаторы — латиницей.
- Unit-тесты без сети и без localhost-сокетов (`tests/infrastructure_policy.py`); любой реальный сокет
  требует `@pytest.mark.integration`.
- `ruff check .` — line-length 100, target py311.
- Кроссплатформенность: только `pathlib`/httpx, никаких `subprocess`, shell, POSIX-путей, мутаций `os.environ`.
- Секреты никогда не попадают в текст ошибки, лог, `validate_connection` или provider options
  (`BoardProviderError(secrets=...)`, `sanitize_provider_text`).
- Обратная совместимость `BoardHttpClient`: существующие вызовы из `jira.py`/`yougile.py`/`youtrack.py` и
  тесты `tests/tasks/boards/test_board_http.py` должны продолжать работать без правок.
- Существующие адаптеры (`jira.py`, `yougile.py`, `youtrack.py`) в этой фазе **не переписываются** —
  меняются только их тестовые фикстуры.
- Каждый httpx-клиент принимает `transport=` и `sleeper=` (инъекция для тестов), как `JiraCloudBoard.__init__`.
- Коммиты: Conventional Commits на русском, **без** self-attribution.

---

### Task 1: `rate_limit_hint` в `BoardHttpClient`

GitHub отдаёт исчерпание лимита как `403` с `X-RateLimit-Remaining: 0`, а не как `429`, поэтому
классификация rate-limit должна быть переопределяемой провайдером.

**Files:**
- Modify: `reviewer/tasks/boards/http.py:17-127`
- Test: `tests/tasks/boards/test_board_http.py` (дописать в конец)

**Interfaces:**
- Consumes: ничего (первая задача).
- Produces: `BoardHttpClient(..., rate_limit_hint: Callable[[int, Mapping[str, str]], float | None] | None = None)`.
  Хук получает `(status_code, response_headers)` и возвращает секунды ожидания, если ответ означает
  rate-limit, иначе `None`. Значение зажимается `max_wait`. Когда хук вернул число: категория ошибки —
  `rate_limit`, а read-операция ретраится даже на статусе, который иначе был бы `permission`.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/tasks/boards/test_board_http.py — дописать в конец файла
def test_rate_limit_hint_turns_forbidden_into_retryable_rate_limit():
    responses = [
        httpx.Response(403, headers={"X-RateLimit-Remaining": "0"}, json={}),
        httpx.Response(200, json={"ok": True}),
    ]
    client = _client_returning(responses)
    sleeps: list[float] = []
    http = BoardHttpClient(
        client,
        attempts=3,
        max_wait=5.0,
        sleeper=sleeps.append,
        rate_limit_hint=lambda status, headers: (
            2.0 if status == 403 and headers.get("X-RateLimit-Remaining") == "0" else None
        ),
    )

    assert http.request_json("GET", "/issues", operation="read") == {"ok": True}
    assert sleeps == [2.0]


def test_rate_limit_hint_result_is_bounded_by_max_wait():
    client = _client_returning([httpx.Response(403, json={}), httpx.Response(200, json={})])
    sleeps: list[float] = []
    http = BoardHttpClient(
        client,
        attempts=2,
        max_wait=1.5,
        sleeper=sleeps.append,
        rate_limit_hint=lambda status, headers: 900.0,
    )

    http.request_json("GET", "/issues", operation="read")
    assert sleeps == [1.5]


def test_rate_limit_hint_error_category_is_rate_limit_when_attempts_exhausted():
    client = _client_returning([httpx.Response(403, json={})])
    http = BoardHttpClient(
        client,
        attempts=1,
        sleeper=lambda _: None,
        rate_limit_hint=lambda status, headers: 1.0,
    )

    with pytest.raises(BoardProviderError) as exc_info:
        http.request_json("GET", "/issues", operation="read")
    assert exc_info.value.category == "rate_limit"


def test_without_hint_forbidden_stays_permission_and_is_not_retried():
    client = _client_returning([httpx.Response(403, json={}), httpx.Response(200, json={})])
    sleeps: list[float] = []
    http = BoardHttpClient(client, attempts=3, sleeper=sleeps.append)

    with pytest.raises(BoardProviderError) as exc_info:
        http.request_json("GET", "/issues", operation="read")
    assert exc_info.value.category == "permission"
    assert sleeps == []


def test_rate_limit_hint_is_not_consulted_for_write_retry():
    client = _client_returning([httpx.Response(403, json={}), httpx.Response(200, json={})])
    sleeps: list[float] = []
    http = BoardHttpClient(
        client,
        attempts=3,
        sleeper=sleeps.append,
        rate_limit_hint=lambda status, headers: 1.0,
    )

    with pytest.raises(BoardProviderError) as exc_info:
        http.request_json("POST", "/issues", operation="write")
    assert exc_info.value.category == "rate_limit"
    assert sleeps == []
```

Если в файле ещё нет хелпера `_client_returning`, добавить его рядом с существующими хелперами:

```python
class _SequenceClient:
    """Клиент-заглушка: отдаёт заранее заданную последовательность ответов."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self._responses:
            raise AssertionError("unexpected extra request")
        return self._responses.pop(0)


def _client_returning(responses: list[httpx.Response]) -> _SequenceClient:
    return _SequenceClient(responses)
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/tasks/boards/test_board_http.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'rate_limit_hint'`.

- [ ] **Step 3: Реализовать хук**

```python
# reviewer/tasks/boards/http.py
from collections.abc import Callable, Collection, Mapping

class BoardHttpClient:
    def __init__(
        self,
        client: Any,
        *,
        attempts: int = 3,
        backoff_base: float = 1.0,
        max_wait: float = 8.0,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        secrets: Collection[str] = (),
        rate_limit_hint: Callable[[int, Mapping[str, str]], float | None] | None = None,
    ) -> None:
        ...
        self._rate_limit_hint = rate_limit_hint
```

В `request_json` после получения ответа и проверки 2xx:

```python
            hinted = self._hinted_wait(status, response.headers)
            retryable_status = status == 429 or 500 <= status < 600 or hinted is not None
            if retryable_status and operation == "read" and attempt < self._attempts - 1:
                wait = hinted if hinted is not None else self._wait_for(attempt, response.headers)
                self._sleep(min(wait, self._max_wait))
                continue
            raise self._error_for_status(status, operation, rate_limited=hinted is not None)
```

Хелпер и правка классификации:

```python
    def _hinted_wait(self, status: int, headers: Any) -> float | None:
        """Секунды ожидания из provider-специфичного хука; None — хука нет либо это не rate-limit."""
        if self._rate_limit_hint is None:
            return None
        try:
            wait = self._rate_limit_hint(status, headers or {})
        except Exception:
            return None
        if wait is None:
            return None
        return max(0.0, min(float(wait), self._max_wait))

    def _error_for_status(
        self,
        status: int,
        operation: str,
        *,
        rate_limited: bool = False,
    ) -> BoardProviderError:
        if rate_limited:
            category, hint = "rate_limit", "Wait before trying the board again."
        elif status == 401:
            ...
```

- [ ] **Step 4: Прогнать тесты — зелено, регрессий нет**

Run: `.venv/bin/pytest tests/tasks/boards/test_board_http.py -q && .venv/bin/pytest tests/tasks/boards -q`
Expected: PASS, число тестов не уменьшилось.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/tasks/boards/http.py tests/tasks/boards/test_board_http.py
git commit -m "feat(boards): provider-специфичный rate_limit_hint в BoardHttpClient"
```

---

### Task 2: `RestBoardBase` — общий REST-скелет адаптера

**Files:**
- Create: `reviewer/tasks/boards/restbase.py`
- Test: `tests/tasks/boards/test_restbase.py`

**Interfaces:**
- Consumes: `BoardHttpClient` (+ `rate_limit_hint` из Task 1), `BoardProviderError`.
- Produces:
  ```python
  class RestBoardBase:
      board_type: str = ""
      def __init__(self, *, base_url: str, secrets: Collection[str] = (),
                   key_pattern: str = "", url_template: str = "",
                   headers: Mapping[str, str] | None = None,
                   params: Mapping[str, str] | None = None,
                   auth: httpx.Auth | None = None, timeout: float = 30.0,
                   transport: httpx.BaseTransport | None = None,
                   sleeper: Callable[[float], None] = time.sleep, attempts: int = 3,
                   rate_limit_hint: Callable[[int, Mapping[str, str]], float | None] | None = None) -> None
      def _read(self, method: str, path: str, **kwargs: Any) -> Any
      def _write(self, method: str, path: str, **kwargs: Any) -> Any
      def _task_url(self, code: str) -> str
      def close(self) -> None
      @property
      def secrets(self) -> frozenset[str]
  ```
  `params` уходит в каждый запрос (модель Trello: `key`/`token` в query-string). `_task_url` подставляет
  `{code}` в `url_template`; пустой шаблон → `""`.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/tasks/boards/test_restbase.py
"""Тесты общего REST-скелета адаптеров досок."""
from __future__ import annotations

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.restbase import RestBoardBase


def _board(handler, **kwargs) -> RestBoardBase:
    return RestBoardBase(
        base_url="https://board.test/api",
        secrets=("top-secret-token",),
        key_pattern=r"PRI-\d+",
        url_template="https://board.test/task/{code}",
        headers={"Authorization": "Bearer top-secret-token"},
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
        **kwargs,
    )


def test_read_goes_through_injected_transport_with_headers():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    board = _board(handler)
    assert board._read("GET", "/tasks") == {"ok": True}
    assert seen[0].url.path == "/api/tasks"
    assert seen[0].headers["Authorization"] == "Bearer top-secret-token"
    board.close()


def test_query_params_are_attached_to_every_request():
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={})

    board = _board(handler, params={"key": "app-key", "token": "top-secret-token"})
    board._read("GET", "/cards")
    board._write("PUT", "/cards/1", json={"desc": "x"})
    assert seen == [
        {"key": "app-key", "token": "top-secret-token"},
        {"key": "app-key", "token": "top-secret-token"},
    ]
    board.close()


def test_task_url_uses_template_and_tolerates_empty_template():
    board = _board(lambda request: httpx.Response(200, json={}))
    assert board._task_url("PRI-7") == "https://board.test/task/PRI-7"
    board.close()

    plain = RestBoardBase(base_url="https://board.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={})))
    assert plain._task_url("PRI-7") == ""
    plain.close()


def test_close_closes_underlying_transport():
    closed: list[bool] = []

    class _Transport(httpx.MockTransport):
        def close(self) -> None:
            closed.append(True)
            super().close()

    board = RestBoardBase(
        base_url="https://board.test",
        transport=_Transport(lambda request: httpx.Response(200, json={})),
    )
    board.close()
    assert closed == [True]


def test_secret_never_leaks_into_error_text():
    board = _board(lambda request: httpx.Response(403, json={"token": "top-secret-token"}))
    with pytest.raises(BoardProviderError) as exc_info:
        board._read("GET", "/tasks")
    assert exc_info.value.category == "permission"
    assert "top-secret-token" not in f"{exc_info.value!s}{exc_info.value!r}"
    board.close()


def test_write_is_not_retried_but_read_is():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(500, json={})

    board = _board(handler, attempts=2)
    with pytest.raises(BoardProviderError):
        board._read("GET", "/tasks")
    read_calls = len(calls)
    calls.clear()
    with pytest.raises(BoardProviderError):
        board._write("POST", "/tasks", json={})
    assert read_calls == 2
    assert len(calls) == 1
    board.close()
```

- [ ] **Step 2: Прогнать — убедиться, что падают**

Run: `.venv/bin/pytest tests/tasks/boards/test_restbase.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.tasks.boards.restbase'`.

- [ ] **Step 3: Реализовать модуль**

```python
"""Общий REST-скелет адаптера доски: httpx-клиент, retry-обёртка, secrets, URL задачи."""
from __future__ import annotations

import time
from collections.abc import Callable, Collection, Mapping
from typing import Any

import httpx

from reviewer.tasks.boards.http import BoardHttpClient


class RestBoardBase:
    """База REST-адаптеров: единый транспорт вместо копий httpx-обвязки в каждом файле.

    Подклассы реализуют TaskBoardProvider поверх ``_read``/``_write`` и обязаны объявить
    ``board_type``. ``transport`` и ``sleeper`` инжектируются в тестах (без сети и без ожидания).
    """

    board_type: str = ""

    def __init__(
        self,
        *,
        base_url: str,
        secrets: Collection[str] = (),
        key_pattern: str = "",
        url_template: str = "",
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        auth: httpx.Auth | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        attempts: int = 3,
        rate_limit_hint: Callable[[int, Mapping[str, str]], float | None] | None = None,
    ) -> None:
        self._secrets = frozenset(value for value in secrets if value)
        self._key_pattern = key_pattern
        self._url_template = url_template
        self._client = httpx.Client(
            base_url=base_url,
            headers=dict(headers or {}),
            params=dict(params or {}),
            auth=auth,
            timeout=timeout,
            transport=transport,
        )
        self._http = BoardHttpClient(
            self._client,
            attempts=attempts,
            sleeper=sleeper,
            secrets=self._secrets,
            rate_limit_hint=rate_limit_hint,
        )

    @property
    def secrets(self) -> frozenset[str]:
        return self._secrets

    def _read(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._http.request_json(method, path, operation="read", **kwargs)

    def _write(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._http.request_json(method, path, operation="write", **kwargs)

    def _task_url(self, code: str) -> str:
        """Ссылка на задачу по шаблону из настроек; пустой шаблон → пустая строка."""
        if not self._url_template:
            return ""
        return self._url_template.format(code=code)

    def close(self) -> None:
        self._http.close()
```

- [ ] **Step 4: Прогнать — зелено**

Run: `.venv/bin/pytest tests/tasks/boards/test_restbase.py -q && .venv/bin/ruff check reviewer/tasks/boards/restbase.py tests/tasks/boards/test_restbase.py`
Expected: PASS, ruff без замечаний.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/tasks/boards/restbase.py tests/tasks/boards/test_restbase.py
git commit -m "feat(boards): RestBoardBase — общий REST-скелет адаптеров"
```

---

### Task 3: `pagination.py` — генераторы страниц

**Files:**
- Create: `reviewer/tasks/boards/pagination.py`
- Test: `tests/tasks/boards/test_pagination.py`

**Interfaces:**
- Consumes: `BoardProviderError`.
- Produces (все — генераторы элементов, лениво тянущие страницы, `max_pages` защищает от зацикливания):
  ```python
  def paginate_offset(fetch: Callable[[int, int], Any], *, page_size: int,
                      items: Callable[[Any], list] = ..., max_pages: int = 1000) -> Iterator[Any]
  def paginate_page(fetch: Callable[[int, int], Any], *, page_size: int,
                    items: Callable[[Any], list] = ..., start: int = 1, max_pages: int = 1000) -> Iterator[Any]
  def paginate_cursor(fetch: Callable[[str | None], Any], *, items: Callable[[Any], list],
                      next_cursor: Callable[[Any], str | None], max_pages: int = 1000) -> Iterator[Any]
  def paginate_link_header(fetch: Callable[[str | None], tuple[Any, Mapping[str, str]]], *,
                           items: Callable[[Any], list] = ..., max_pages: int = 1000) -> Iterator[Any]
  def next_link(headers: Mapping[str, str]) -> str | None
  ```
  `fetch` у `paginate_offset` вызывается как `fetch(offset, page_size)`, у `paginate_page` — как
  `fetch(page, page_size)`. Обход завершается, когда страница отдала меньше `page_size` элементов
  (offset/page), когда курсор стал `None` или когда в заголовках нет `rel="next"`.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/tasks/boards/test_pagination.py
"""Тесты генераторов пагинации провайдеров досок."""
from __future__ import annotations

import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.pagination import (
    next_link,
    paginate_cursor,
    paginate_link_header,
    paginate_offset,
    paginate_page,
)


def test_paginate_offset_walks_until_short_page():
    calls: list[tuple[int, int]] = []

    def fetch(offset: int, limit: int):
        calls.append((offset, limit))
        return [{"i": offset + n} for n in range(limit if offset < 4 else 1)]

    rows = list(paginate_offset(fetch, page_size=2))
    assert [row["i"] for row in rows] == [0, 1, 2, 3, 4]
    assert calls == [(0, 2), (2, 2), (4, 2)]


def test_paginate_offset_stops_on_empty_first_page():
    rows = list(paginate_offset(lambda offset, limit: [], page_size=50))
    assert rows == []


def test_paginate_page_starts_at_one_and_unwraps_items():
    calls: list[int] = []

    def fetch(page: int, size: int):
        calls.append(page)
        return {"data": [{"n": page}] * (size if page < 3 else 0)}

    rows = list(paginate_page(fetch, page_size=2, items=lambda payload: payload["data"]))
    assert len(rows) == 4
    assert calls == [1, 2, 3]


def test_paginate_cursor_follows_cursor_until_none():
    pages = {
        None: {"items": [1, 2], "next": "c1"},
        "c1": {"items": [3], "next": "c2"},
        "c2": {"items": [4], "next": None},
    }
    seen: list[str | None] = []

    def fetch(cursor: str | None):
        seen.append(cursor)
        return pages[cursor]

    rows = list(
        paginate_cursor(
            fetch,
            items=lambda payload: payload["items"],
            next_cursor=lambda payload: payload["next"],
        )
    )
    assert rows == [1, 2, 3, 4]
    assert seen == [None, "c1", "c2"]


def test_next_link_parses_rel_next_and_ignores_others():
    headers = {
        "Link": '<https://api.test/issues?page=2>; rel="next", '
        '<https://api.test/issues?page=9>; rel="last"'
    }
    assert next_link(headers) == "https://api.test/issues?page=2"
    assert next_link({"Link": '<https://api.test/issues?page=9>; rel="last"'}) is None
    assert next_link({}) is None


def test_paginate_link_header_follows_next_until_absent():
    pages = {
        None: ([1, 2], {"Link": '<https://api.test/issues?page=2>; rel="next"'}),
        "https://api.test/issues?page=2": ([3], {}),
    }
    seen: list[str | None] = []

    def fetch(url: str | None):
        seen.append(url)
        return pages[url]

    rows = list(paginate_link_header(fetch))
    assert rows == [1, 2, 3]
    assert seen == [None, "https://api.test/issues?page=2"]


def test_max_pages_guard_raises_instead_of_looping_forever():
    with pytest.raises(BoardProviderError) as exc_info:
        list(
            paginate_cursor(
                lambda cursor: {"items": [1], "next": "same"},
                items=lambda payload: payload["items"],
                next_cursor=lambda payload: payload["next"],
                max_pages=3,
            )
        )
    assert exc_info.value.category == "unsupported"
```

- [ ] **Step 2: Прогнать — падают**

Run: `.venv/bin/pytest tests/tasks/boards/test_pagination.py -q`
Expected: FAIL — модуль `pagination` не найден.

- [ ] **Step 3: Реализовать модуль**

```python
"""Модели пагинации досок: offset, номер страницы, курсор, Link-заголовок."""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from reviewer.tasks.boards.errors import BoardProviderError

_LINK_RE = re.compile(r'<(?P<url>[^>]+)>\s*;\s*rel="(?P<rel>[^"]+)"')


def _identity(payload: Any) -> list:
    return payload if isinstance(payload, list) else []


def _guard(pages: int, max_pages: int) -> None:
    if pages >= max_pages:
        raise BoardProviderError(
            "unsupported",
            "Board pagination exceeded the maximum page count.",
            hint="Narrow the board query or raise the page size.",
        )


def paginate_offset(
    fetch: Callable[[int, int], Any],
    *,
    page_size: int,
    items: Callable[[Any], list] = _identity,
    max_pages: int = 1000,
) -> Iterator[Any]:
    """Страницы по offset/limit: обход до первой неполной страницы."""
    offset = 0
    for page in range(max_pages):
        _guard(page, max_pages)
        batch = items(fetch(offset, page_size))
        yield from batch
        if len(batch) < page_size:
            return
        offset += page_size
    _guard(max_pages, max_pages)


def paginate_page(
    fetch: Callable[[int, int], Any],
    *,
    page_size: int,
    items: Callable[[Any], list] = _identity,
    start: int = 1,
    max_pages: int = 1000,
) -> Iterator[Any]:
    """Страницы по номеру: обход до первой неполной страницы."""
    number = start
    for page in range(max_pages):
        _guard(page, max_pages)
        batch = items(fetch(number, page_size))
        yield from batch
        if len(batch) < page_size:
            return
        number += 1
    _guard(max_pages, max_pages)


def paginate_cursor(
    fetch: Callable[[str | None], Any],
    *,
    items: Callable[[Any], list],
    next_cursor: Callable[[Any], str | None],
    max_pages: int = 1000,
) -> Iterator[Any]:
    """Курсорная пагинация: обход, пока провайдер отдаёт следующий курсор."""
    cursor: str | None = None
    for page in range(max_pages):
        _guard(page, max_pages)
        payload = fetch(cursor)
        yield from items(payload)
        cursor = next_cursor(payload)
        if not cursor:
            return
    _guard(max_pages, max_pages)


def next_link(headers: Mapping[str, str]) -> str | None:
    """URL страницы rel="next" из заголовка Link; None — следующей страницы нет."""
    raw = headers.get("Link") or headers.get("link") or ""
    for match in _LINK_RE.finditer(raw):
        if match.group("rel") == "next":
            return match.group("url")
    return None


def paginate_link_header(
    fetch: Callable[[str | None], tuple[Any, Mapping[str, str]]],
    *,
    items: Callable[[Any], list] = _identity,
    max_pages: int = 1000,
) -> Iterator[Any]:
    """Пагинация по заголовку Link (модель GitHub): fetch отдаёт (payload, headers)."""
    url: str | None = None
    for page in range(max_pages):
        _guard(page, max_pages)
        payload, headers = fetch(url)
        yield from items(payload)
        url = next_link(headers)
        if not url:
            return
    _guard(max_pages, max_pages)
```

- [ ] **Step 4: Прогнать — зелено**

Run: `.venv/bin/pytest tests/tasks/boards/test_pagination.py -q && .venv/bin/ruff check reviewer/tasks/boards/pagination.py tests/tasks/boards/test_pagination.py`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/tasks/boards/pagination.py tests/tasks/boards/test_pagination.py
git commit -m "feat(boards): переиспользуемые генераторы пагинации"
```

---

### Task 4: `graphql.py` — тонкий GraphQL-фасад (для Linear)

**Files:**
- Create: `reviewer/tasks/boards/graphql.py`
- Test: `tests/tasks/boards/test_graphql.py`

**Interfaces:**
- Consumes: `BoardHttpClient`, `BoardProviderError`.
- Produces:
  ```python
  class GraphQLClient:
      def __init__(self, http: BoardHttpClient, *, path: str = "/graphql",
                   secrets: Collection[str] = ()) -> None
      def execute(self, query: str, variables: Mapping[str, Any] | None = None, *,
                  operation: Literal["read", "write"] = "read") -> dict
      def paginate(self, query: str, variables: Mapping[str, Any], *,
                   connection: Callable[[dict], dict], operation: Literal["read", "write"] = "read",
                   max_pages: int = 1000) -> Iterator[dict]
  ```
  `execute` бросает `BoardProviderError` при непустом `errors[]`, категория — из
  `extensions.code`: `AUTHENTICATION_ERROR`→`authentication`, `FORBIDDEN`/`FEATURE_NOT_ACCESSIBLE`→`permission`,
  `RATELIMITED`→`rate_limit` (retryable), `NOT_FOUND`/`ENTITY_NOT_FOUND`→`not_found`, иначе `unsupported`.
  `paginate` подставляет в `variables` ключ `after` из `pageInfo.endCursor` и отдаёт `nodes` постранично;
  `connection(data)` возвращает объект connection (`{"nodes": [...], "pageInfo": {...}}`).

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/tasks/boards/test_graphql.py
"""Тесты GraphQL-фасада для адаптеров досок."""
from __future__ import annotations

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.graphql import GraphQLClient
from reviewer.tasks.boards.http import BoardHttpClient


def _gql(handler, *, secrets=("linear-secret",), attempts=1) -> GraphQLClient:
    client = httpx.Client(
        base_url="https://api.linear.app",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "linear-secret"},
    )
    http = BoardHttpClient(client, attempts=attempts, sleeper=lambda _: None, secrets=secrets)
    return GraphQLClient(http, secrets=secrets)


def test_execute_posts_query_and_returns_data():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"data": {"viewer": {"id": "u1"}}})

    client = _gql(handler)
    assert client.execute("query Me { viewer { id } }") == {"viewer": {"id": "u1"}}
    assert seen[0]["query"].startswith("query Me")
    assert seen[0]["variables"] == {}


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("AUTHENTICATION_ERROR", "authentication"),
        ("FORBIDDEN", "permission"),
        ("RATELIMITED", "rate_limit"),
        ("ENTITY_NOT_FOUND", "not_found"),
        ("INTERNAL_SERVER_ERROR", "unsupported"),
    ],
)
def test_graphql_errors_map_to_categories(code: str, category: str):
    client = _gql(
        lambda request: httpx.Response(
            200,
            json={"errors": [{"message": "boom", "extensions": {"code": code}}]},
        )
    )
    with pytest.raises(BoardProviderError) as exc_info:
        client.execute("query { viewer { id } }")
    assert exc_info.value.category == category


def test_graphql_error_text_never_contains_secret():
    client = _gql(
        lambda request: httpx.Response(
            200,
            json={"errors": [{"message": "token linear-secret rejected"}]},
        )
    )
    with pytest.raises(BoardProviderError) as exc_info:
        client.execute("query { viewer { id } }")
    assert "linear-secret" not in f"{exc_info.value!s}{exc_info.value!r}"


def test_paginate_follows_page_info_end_cursor():
    pages = [
        {
            "data": {
                "issues": {
                    "nodes": [{"id": "a"}, {"id": "b"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cur-1"},
                }
            }
        },
        {
            "data": {
                "issues": {
                    "nodes": [{"id": "c"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        },
    ]
    cursors: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content.decode())
        cursors.append(body["variables"].get("after"))
        return httpx.Response(200, json=pages[len(cursors) - 1])

    client = _gql(handler)
    nodes = list(
        client.paginate(
            "query Issues($after: String) { issues(after: $after) { nodes { id } pageInfo { hasNextPage endCursor } } }",
            {"first": 50},
            connection=lambda data: data["issues"],
        )
    )
    assert [node["id"] for node in nodes] == ["a", "b", "c"]
    assert cursors == [None, "cur-1"]
```

- [ ] **Step 2: Прогнать — падают**

Run: `.venv/bin/pytest tests/tasks/boards/test_graphql.py -q`
Expected: FAIL — модуль `graphql` не найден.

- [ ] **Step 3: Реализовать модуль**

```python
"""Тонкий GraphQL-фасад поверх безопасного board HTTP client."""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping
from typing import Any, Literal

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.http import BoardHttpClient

_CODE_CATEGORY = {
    "AUTHENTICATION_ERROR": "authentication",
    "UNAUTHENTICATED": "authentication",
    "FORBIDDEN": "permission",
    "FEATURE_NOT_ACCESSIBLE": "permission",
    "RATELIMITED": "rate_limit",
    "RATE_LIMITED": "rate_limit",
    "NOT_FOUND": "not_found",
    "ENTITY_NOT_FOUND": "not_found",
}


class GraphQLClient:
    """POST одного endpoint'а: транспортные ретраи берёт из BoardHttpClient,
    прикладные ошибки GraphQL переводит в BoardProviderError."""

    def __init__(
        self,
        http: BoardHttpClient,
        *,
        path: str = "/graphql",
        secrets: Collection[str] = (),
    ) -> None:
        self._http = http
        self._path = path
        self._secrets = frozenset(value for value in secrets if value)

    def execute(
        self,
        query: str,
        variables: Mapping[str, Any] | None = None,
        *,
        operation: Literal["read", "write"] = "read",
    ) -> dict:
        payload = self._http.request_json(
            "POST",
            self._path,
            operation=operation,
            json={"query": query, "variables": dict(variables or {})},
        )
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            raise self._error_for(errors)
        data = payload.get("data") if isinstance(payload, dict) else None
        return data or {}

    def paginate(
        self,
        query: str,
        variables: Mapping[str, Any],
        *,
        connection: Callable[[dict], dict],
        operation: Literal["read", "write"] = "read",
        max_pages: int = 1000,
    ) -> Iterator[dict]:
        """Обход connection по pageInfo.endCursor; переменная курсора — `after`."""
        cursor: str | None = None
        for _ in range(max_pages):
            data = self.execute(query, {**dict(variables), "after": cursor}, operation=operation)
            block = connection(data) or {}
            yield from block.get("nodes") or []
            info = block.get("pageInfo") or {}
            cursor = info.get("endCursor") if info.get("hasNextPage") else None
            if not cursor:
                return
        raise BoardProviderError(
            "unsupported",
            "Board GraphQL pagination exceeded the maximum page count.",
            secrets=self._secrets,
        )

    def _error_for(self, errors: Any) -> BoardProviderError:
        code = ""
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            extensions = errors[0].get("extensions") or {}
            code = str(extensions.get("code") or "")
        category = _CODE_CATEGORY.get(code, "unsupported")
        return BoardProviderError(
            category,
            "Board GraphQL request returned errors.",
            hint="Check the board GraphQL query, credentials and permissions.",
            retryable=category == "rate_limit",
            secrets=self._secrets,
        )
```

- [ ] **Step 4: Прогнать — зелено**

Run: `.venv/bin/pytest tests/tasks/boards/test_graphql.py -q && .venv/bin/ruff check reviewer/tasks/boards/graphql.py tests/tasks/boards/test_graphql.py`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/tasks/boards/graphql.py tests/tasks/boards/test_graphql.py
git commit -m "feat(boards): GraphQL-фасад для адаптеров досок"
```

---

### Task 5: `yfm.py` — Yandex Tracker YFM → markdown

**Files:**
- Create: `reviewer/tasks/boards/yfm.py`
- Test: `tests/tasks/boards/test_yfm.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `def yfm_to_md(text: str) -> str` — никогда не бросает; неизвестные конструкции остаются как есть.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/tasks/boards/test_yfm.py
"""Тесты конвертации YFM-разметки Yandex Tracker в markdown."""
from __future__ import annotations

from reviewer.tasks.boards.yfm import yfm_to_md


def test_code_block_without_language():
    assert yfm_to_md("до\n%%\nprint(1)\n%%\nпосле") == "до\n```\nprint(1)\n```\nпосле"


def test_code_block_with_language():
    assert yfm_to_md("%%(python)\nprint(1)\n%%") == "```python\nprint(1)\n```"


def test_inline_link_is_converted():
    assert yfm_to_md("см. ((https://example.test док))") == "см. [док](https://example.test)"


def test_link_without_text_keeps_url():
    assert yfm_to_md("((https://example.test))") == "<https://example.test>"


def test_cut_becomes_bold_heading_with_body():
    assert yfm_to_md("<{Детали\nтело\n}>") == "**Детали**\n\nтело"


def test_plain_markdown_is_unchanged():
    text = "# Заголовок\n\n- пункт\n\n`код`"
    assert yfm_to_md(text) == text


def test_unknown_constructs_are_left_as_is_and_never_raise():
    text = "!!(red)важно!!\n{{макрос}}"
    assert yfm_to_md(text) == text
    assert yfm_to_md("") == ""
```

- [ ] **Step 2: Прогнать — падают**

Run: `.venv/bin/pytest tests/tasks/boards/test_yfm.py -q`
Expected: FAIL — модуль `yfm` не найден.

- [ ] **Step 3: Реализовать модуль**

```python
"""Узкая конвертация YFM-разметки Yandex Tracker в markdown.

Покрывает конструкции, которые ломают markdown-инвариант normalize: блоки кода ``%%``,
ссылки ``((url текст))`` и cut ``<{Заголовок ... }>``. Всё остальное (макросы, цветной
текст) остаётся как есть — читаемо и не теряется. Функция никогда не бросает.
"""
from __future__ import annotations

import re

_CODE_RE = re.compile(r"%%(?:\((?P<lang>[^)\n]*)\))?\n(?P<body>.*?)\n%%", re.DOTALL)
_LINK_RE = re.compile(r"\(\((?P<url>\S+?)(?:\s+(?P<text>[^)]+?))?\)\)")
_CUT_RE = re.compile(r"<\{(?P<title>[^\n]*)\n(?P<body>.*?)\n?\}>", re.DOTALL)


def _code(match: re.Match[str]) -> str:
    lang = (match.group("lang") or "").strip()
    return f"```{lang}\n{match.group('body')}\n```"


def _link(match: re.Match[str]) -> str:
    url = match.group("url")
    text = (match.group("text") or "").strip()
    return f"[{text}]({url})" if text else f"<{url}>"


def _cut(match: re.Match[str]) -> str:
    title = match.group("title").strip()
    body = match.group("body").strip()
    return f"**{title}**\n\n{body}" if title else body


def yfm_to_md(text: str) -> str:
    """YFM → markdown; при любой ошибке возвращает исходный текст."""
    if not text:
        return ""
    try:
        out = _CODE_RE.sub(_code, text)
        out = _CUT_RE.sub(_cut, out)
        return _LINK_RE.sub(_link, out)
    except Exception:
        return text
```

- [ ] **Step 4: Прогнать — зелено**

Run: `.venv/bin/pytest tests/tasks/boards/test_yfm.py -q && .venv/bin/ruff check reviewer/tasks/boards/yfm.py tests/tasks/boards/test_yfm.py`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/tasks/boards/yfm.py tests/tasks/boards/test_yfm.py
git commit -m "feat(boards): конвертер YFM Yandex Tracker в markdown"
```

---

### Task 6: Разъезд contract-фикстур по файлам и вынос порогов в `ProviderAdapter`

Восемь новых провайдеров не должны править один общий файл фикстур; тест не должен хардкодить
пороги строк и пути страниц конкретных досок.

**Files:**
- Create: `tests/tasks/boards/fakes/__init__.py`
- Create: `tests/tasks/boards/fakes/base.py`
- Create: `tests/tasks/boards/fakes/yougile.py` (перенос `_yougile_task`/`_yougile_handler` + `ADAPTER`)
- Create: `tests/tasks/boards/fakes/youtrack.py` (перенос `_youtrack_*` + `ADAPTER`)
- Create: `tests/tasks/boards/fakes/jira.py` (перенос `_jira_*` + `ADAPTER`)
- Modify: `tests/tasks/boards/contract.py` (оставить только `ProviderAdapter`, `ADAPTERS`, `ProviderContract`)
- Modify: `tests/tasks/boards/test_provider_contract.py` (если импортирует перенесённые хелперы)
- Modify: `tests/tasks/boards/jira_helpers.py`, `provider_fakes.py` (только если импортируют перенесённое)

**Interfaces:**
- Consumes: существующие фейки yougile/youtrack/jira.
- Produces:
  ```python
  # tests/tasks/boards/fakes/base.py
  @dataclass
  class FakeState:
      calls: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
      closed: bool = False

  class RecordingTransport(httpx.MockTransport):
      def __init__(self, handler: Callable[[httpx.Request], httpx.Response], state: FakeState) -> None
      def close(self) -> None  # ставит state.closed = True

  def record(state: FakeState, request: httpx.Request) -> None
  def request_json(request: httpx.Request) -> dict[str, Any]

  # tests/tasks/boards/contract.py
  @dataclass(frozen=True)
  class ProviderAdapter:
      board_type: str
      secret: str
      project: str
      key: str
      finish_key: str
      target_id: str
      target_label: str
      missing_target: str
      factory: Callable[..., tuple[TaskBoardProvider, FakeState]]
      min_rows: int
      page_paths: tuple[str, ...]
      def provider_factory(self, *, state=None, forbidden=False, error_status=None) -> tuple[TaskBoardProvider, FakeState]
  ```
  Каждый `tests/tasks/boards/fakes/<type>.py` экспортирует `ADAPTER: ProviderAdapter`; `ADAPTERS` в
  `contract.py` собирается из явного списка импортов — добавление провайдера = одна строка.

- [ ] **Step 1: Убедиться, что базовый прогон зелёный (точка отсчёта)**

Run: `.venv/bin/pytest tests/tasks/boards -q`
Expected: PASS. Записать число пройденных тестов — после рефакторинга оно не должно уменьшиться.

- [ ] **Step 2: Создать общий модуль фейков**

```python
# tests/tasks/boards/fakes/__init__.py
"""Пофайловые фейки досок для общего contract-набора."""
```

```python
# tests/tasks/boards/fakes/base.py
"""Общая инфраструктура фейков: состояние, записывающий транспорт, хелперы."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class FakeState:
    """Наблюдаемое состояние фейка: вызовы и факт закрытия транспорта."""

    calls: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    closed: bool = False


class RecordingTransport(httpx.MockTransport):
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response], state: FakeState):
        super().__init__(handler)
        self._state = state

    def close(self) -> None:
        self._state.closed = True
        super().close()


def record(state: FakeState, request: httpx.Request) -> None:
    state.calls.append(
        (request.method, request.url.path, dict(request.url.params.multi_items()))
    )


def request_json(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode()) if request.content else {}
```

- [ ] **Step 3: Перенести три существующих фейка, каждый со своим `ADAPTER`**

Перенести из `contract.py` в `tests/tasks/boards/fakes/yougile.py` функции `_yougile_task`,
`_yougile_handler` и относящиеся к YouGile поля `_State` (как `State(FakeState)` с полями
`task_description`, `task_completed`, `task_column`, `created`), затем добавить:

```python
# tests/tasks/boards/fakes/yougile.py — хвост файла
from tests.tasks.boards.contract import ProviderAdapter  # noqa: E402  (циклов нет: contract не импортирует fakes на уровне модуля)


def build(*, state: State | None = None, forbidden: bool = False,
          error_status: int | None = None) -> tuple[YougileBoard, State]:
    state = state or State()
    status = error_status or (403 if forbidden else None)
    provider = YougileBoard(
        api_key=ADAPTER.secret,
        api_base="https://yougile.test/api-v2",
        key_pattern=r"PRI-\d+",
        url_template="https://yougile.test/#task/{code}",
    )
    provider._client.close()  # type: ignore[attr-defined]
    provider._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="https://yougile.test/api-v2",
        transport=RecordingTransport(_yougile_handler(state, error_status=status), state),
    )
    return provider, state


ADAPTER = ProviderAdapter(
    board_type="yougile",
    secret="yougile-contract-secret",
    project="PRI",
    key="ID-1",
    finish_key="ID-2",
    target_id="done-id",
    target_label="Done",
    missing_target="Missing",
    factory=build,
    min_rows=1000,
    page_paths=("/tasks",),
)
```

Чтобы `ADAPTER` мог ссылаться на `ADAPTER.secret` до своего создания, в `build` использовать литерал
секрета в отдельной константе:

```python
SECRET = "yougile-contract-secret"
```
и подставлять `api_key=SECRET`, а в `ADAPTER` указывать `secret=SECRET`.

Аналогично `fakes/youtrack.py` (`min_rows=200`, `page_paths=("/issues",)`, `key="PRI-1"`,
`finish_key="PRI-2"`, `target_id="Done"`) и `fakes/jira.py` (`min_rows=200`,
`page_paths=("/search/jql",)`, `key="PRI-1"`, `finish_key="PRI-2"`, `target_id="2"`), где
`JiraCloudBoard` создаётся сразу с `transport=RecordingTransport(...)` (конструкторная инъекция,
без подмены `_client`).

- [ ] **Step 4: Переписать `contract.py` на сборку из фейков и поля адаптера**

```python
# tests/tasks/boards/contract.py — верх файла
"""Повторно используемый contract-test набор для адаптеров досок."""
from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from reviewer.tasks.boards.base import TaskBoardProvider
from reviewer.tasks.boards.errors import BoardProviderError
from tests.tasks.boards.fakes.base import FakeState


@dataclass(frozen=True)
class ProviderAdapter:
    board_type: str
    secret: str
    project: str
    key: str
    finish_key: str
    target_id: str
    target_label: str
    missing_target: str
    factory: Callable[..., tuple[TaskBoardProvider, FakeState]]
    min_rows: int
    page_paths: tuple[str, ...]

    def provider_factory(
        self,
        *,
        state: FakeState | None = None,
        forbidden: bool = False,
        error_status: int | None = None,
    ) -> tuple[TaskBoardProvider, FakeState]:
        return self.factory(state=state, forbidden=forbidden, error_status=error_status)


def _adapters() -> tuple[ProviderAdapter, ...]:
    """Явный список зарегистрированных фейков: одна строка на провайдера."""
    from tests.tasks.boards.fakes import jira, yougile, youtrack

    return (yougile.ADAPTER, youtrack.ADAPTER, jira.ADAPTER)


ADAPTERS = _adapters()
```

Заменить хардкод в двух тестах `ProviderContract`:

```python
    def test_iter_raw_reads_all_pages_and_maps_stable_timestamp(
        self,
        adapter: ProviderAdapter,
    ) -> None:
        provider, state = adapter.provider_factory()
        rows = list(provider.iter_raw(adapter.project, None))
        assert len(rows) > adapter.min_rows
        assert rows[0].timestamp > 0
        page_calls = [call for call in state.calls if call[1].endswith(adapter.page_paths)]
        assert len(page_calls) >= 2
```

```python
    def test_finish_is_idempotent_and_uses_target(self, adapter: ProviderAdapter) -> None:
        provider, _ = adapter.provider_factory()
        first = provider.finish(
            adapter.finish_key, "https://github.test/pull/7", note="Проверено",
            target=adapter.target_id,
        )
        second = provider.finish(
            adapter.finish_key, "https://github.test/pull/7", note="Проверено",
            target=adapter.target_id,
        )
        assert first["pr_link_added"] is True
        assert first["done_set"] is True
        assert second["pr_link_added"] is False
        assert second["done_set"] is False
        assert second["already_closed"] is True
```

- [ ] **Step 5: Прогнать — то же число тестов, всё зелено**

Run: `.venv/bin/pytest tests/tasks/boards -q && .venv/bin/pytest -q && .venv/bin/ruff check tests/tasks/boards reviewer/tasks/boards`
Expected: PASS; количество тестов не меньше зафиксированного в Step 1.

- [ ] **Step 6: Коммит**

```bash
git add tests/tasks/boards
git commit -m "refactor(tests): пофайловые фейки досок и пороги в ProviderAdapter"
```

---

## Гейт фазы A

Прежде чем начинать фазу B (адаптеры):

- [ ] `.venv/bin/pytest tests/tasks/boards -q` — зелено, тестов не меньше, чем до рефакторинга.
- [ ] `.venv/bin/pytest -q` — полный unit-прогон зелёный.
- [ ] `.venv/bin/ruff check .` — новые файлы без замечаний.
- [ ] `reviewer/tasks/boards/` содержит `restbase.py`, `pagination.py`, `graphql.py`, `yfm.py`.
- [ ] `tests/tasks/boards/fakes/` содержит `base.py`, `yougile.py`, `youtrack.py`, `jira.py`.
