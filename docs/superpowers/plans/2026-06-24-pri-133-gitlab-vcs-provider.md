# GitLab-провайдер + деплой-уровневый резолв VCS — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать ревью мульти-платформенным (GitHub + GitLab) — добавить `GitLabProvider` и резолвить тип провайдера/токен на уровне деплоя (auto-derive платформы из git remote при индексации → таблица `repo_vcs`; токены в ENV).

**Architecture:** Платформа определяется из `git remote` при `reviewer index` (локальный клон) и пишется в `repo_vcs`. При ревью (API-only движок) `_create_vcs_provider` читает `repo_vcs` (DB-рид до любого API), выбирает `GitHubProvider`/`GitLabProvider` и подставляет токен из ENV по платформе. Секретов в `.review.yml` нет.

**Tech Stack:** Python 3.11–3.13, httpx (VCS-провайдеры через REST), psycopg (Postgres/ParadeDB), pytest (`httpx.MockTransport` для unit), ruff.

## Global Constraints

- Язык проекта — **русский**: комментарии, докстринги, сообщения CLI.
- Коммиты: **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude). Conventional Commits на русском (`feat(vcs): …`, `feat(index): …`).
- Ветка работы: `feat/pri-133-gitlab-vcs-provider`.
- `ruff check .` — line-length 100, target py311.
- Внешние сервисы (GitHub/GitLab/Postgres) изолированы за интерфейсами и **мокаются** в unit-тестах; реальные вызовы — только в integration.
- `pytest` по умолчанию исключает `integration` (`addopts = -m 'not integration'`).
- `VCSProvider` Protocol (`reviewer/vcs/base.py`) — контракт из 7 методов: `get_pull_request`, `get_changed_files`, `get_file_at_ref`, `list_existing_fingerprints`, `publish_review`, `compare_files`, `close`.
- Маркер идемпотентности — `<!-- ai-review:<hash> -->` (общий для всех провайдеров).

---

### Task 1: Вынести `_RetryTransport` в общий `reviewer/vcs/_http.py`

DRY-рефакторинг: retry-транспорт и маркер-regex общие для обоих провайдеров. Тесты `test_github.py` импортируют `_RetryTransport` из `github.py` — сохраняем реэкспорт, чтобы они не сломались.

**Files:**
- Create: `reviewer/vcs/_http.py`
- Modify: `reviewer/vcs/github.py:1-12` (убрать определения, импортировать из `_http`)
- Test: существующий `tests/vcs/test_github.py` (должен остаться зелёным)

**Interfaces:**
- Produces: `reviewer.vcs._http._RetryTransport` (класс, конструктор `(_wrapped, *, attempts=3, backoff_base=1.0, max_wait=8.0, _sleep=time.sleep)`), `reviewer.vcs._http._FP` (`re.Pattern`), `reviewer.vcs._http._RETRY_CODES` (`set[int]`).
- `reviewer.vcs.github` реэкспортирует `_RetryTransport` и `_FP` (импортом), сигнатура `GitHubProvider` без изменений.

- [ ] **Step 1: Создать `reviewer/vcs/_http.py`** — перенести `_RETRY_CODES`, `_FP`, `_RetryTransport` дословно из `github.py`.

```python
from __future__ import annotations
import re
import time

import httpx

# Маркер идемпотентности в теле комментария (общий для всех VCS-провайдеров).
_FP = re.compile(r"<!-- ai-review:([0-9a-f]+) -->")
_RETRY_CODES = {429, 502, 503, 504}


class _RetryTransport:
    """Обёртка над httpx-транспортом с retry по статусам 429/502/503/504."""

    def __init__(
        self,
        wrapped,
        *,
        attempts: int = 3,
        backoff_base: float = 1.0,
        max_wait: float = 8.0,
        _sleep=time.sleep,
    ):
        self._wrapped = wrapped
        self._attempts = attempts
        self._backoff_base = backoff_base
        self._max_wait = max_wait
        self._sleep = _sleep

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in range(self._attempts):
            response = self._wrapped.handle_request(request)
            if response.status_code not in _RETRY_CODES:
                return response
            if attempt < self._attempts - 1:
                retry_after = response.headers.get("retry-after")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = self._backoff_base * (2 ** attempt)
                else:
                    wait = self._backoff_base * (2 ** attempt)
                # max(0, …): невалидный/отрицательный Retry-After (напр. "-1")
                # не должен уводить sleep в минус — time.sleep(<0) бросает ValueError.
                self._sleep(max(0.0, min(wait, self._max_wait)))
        assert response is not None
        return response

    def close(self) -> None:
        self._wrapped.close()

    def __enter__(self):
        self._wrapped.__enter__()
        return self

    def __exit__(self, *args):
        return self._wrapped.__exit__(*args)
```

- [ ] **Step 2: Обновить шапку `reviewer/vcs/github.py`** — удалить локальные `_RETRY_CODES`/`_FP`/`_RetryTransport` (строки 10–62) и импортировать из `_http`. Итоговые строки 1–11:

```python
from __future__ import annotations
import base64

import httpx

from reviewer.vcs.base import PullRequest, ChangedFile, InlineComment
from reviewer.vcs._http import _RetryTransport, _FP  # noqa: F401  (реэкспорт для тестов)
```
(Убедиться, что `re`/`time` больше не нужны в `github.py`; `_FP` используется в `list_existing_fingerprints`.)

- [ ] **Step 3: Прогнать тесты GitHub-провайдера**

Run: `.venv/bin/pytest tests/vcs/test_github.py -q`
Expected: PASS (все ~17 тестов), без ImportError.

- [ ] **Step 4: Линт**

Run: `.venv/bin/ruff check reviewer/vcs/_http.py reviewer/vcs/github.py`
Expected: чисто (или только заранее известный шум — не трогать чужие файлы).

- [ ] **Step 5: Commit**

```bash
git add reviewer/vcs/_http.py reviewer/vcs/github.py
git commit -m "refactor(vcs): вынести _RetryTransport и маркер в общий reviewer/vcs/_http"
```

---

### Task 2: `derive_vcs_from_remote` — определение платформы из git remote

**Files:**
- Modify: `reviewer/services/repo_id.py` (добавить функцию + хелпер хоста)
- Test: `tests/services/test_repo_id.py` (создать, если нет; иначе дописать)

**Interfaces:**
- Produces: `derive_vcs_from_remote(remote_url: str) -> tuple[str, str] | None` — `(provider, base_url)`; `("github", "")` / `("gitlab", "https://<host>")` / `None`.

- [ ] **Step 1: Написать падающие тесты** в `tests/services/test_repo_id.py`

```python
import pytest
from reviewer.services.repo_id import derive_vcs_from_remote


@pytest.mark.parametrize("url, expected", [
    ("git@github.com:o/r.git", ("github", "")),
    ("https://github.com/o/r.git", ("github", "")),
    ("https://gitlab.com/o/r.git", ("gitlab", "https://gitlab.com")),
    ("git@gitlab.acme.com:grp/r.git", ("gitlab", "https://gitlab.acme.com")),
    ("https://gitlab.acme.com/grp/sub/r.git", ("gitlab", "https://gitlab.acme.com")),
    ("https://bitbucket.org/o/r.git", None),
    ("", None),
    ("not a url", None),
])
def test_derive_vcs_from_remote(url, expected):
    assert derive_vcs_from_remote(url) == expected
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/services/test_repo_id.py -q`
Expected: FAIL (`ImportError: cannot import name 'derive_vcs_from_remote'`).

- [ ] **Step 3: Реализовать** в `reviewer/services/repo_id.py` (добавить после `derive_repo_from_remote`)

```python
_SSH_HOST_RE = re.compile(r"^[\w.-]+@([\w.-]+):")
_HTTP_HOST_RE = re.compile(r"^https?://([^/]+)/")


def _remote_host(remote_url: str) -> str | None:
    """Хост из git remote URL (ssh `git@host:...` или https `https://host/...`)."""
    u = (remote_url or "").strip()
    m = _SSH_HOST_RE.match(u)
    if m:
        return m.group(1).lower()
    m = _HTTP_HOST_RE.match(u)
    if m:
        # отрезаем возможные userinfo@ и :port
        return m.group(1).lower().split("@")[-1].split(":")[0]
    return None


def derive_vcs_from_remote(remote_url: str) -> tuple[str, str] | None:
    """(provider, base_url) из git remote URL; None если платформа не распознана.

    github.com → ('github', '') (base_url не нужен — зашит в GitHubProvider);
    хост с 'gitlab' (gitlab.com или self-hosted) → ('gitlab', 'https://<host>').
    """
    host = _remote_host(remote_url)
    if host is None:
        return None
    if "gitlab" in host:
        return ("gitlab", f"https://{host}")
    if "github" in host:
        return ("github", "")
    return None
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/services/test_repo_id.py -q`
Expected: PASS (8 параметров).

- [ ] **Step 5: Commit**

```bash
git add reviewer/services/repo_id.py tests/services/test_repo_id.py
git commit -m "feat(index): derive_vcs_from_remote — платформа VCS из git remote"
```

---

### Task 3: Таблица `repo_vcs` + методы стора

Персистентная карта `repo → (provider, base_url)`. Методы зеркалят `get_index_meta`/`set_index_meta` (fail-soft на отсутствие таблицы). Тест — integration (нужен Postgres).

**Files:**
- Modify: `reviewer/index/schema.sql` (добавить таблицу в конец)
- Modify: `reviewer/index/store.py` (методы рядом с `set_index_meta`, ~строка 176)
- Test: `tests/index/test_repo_vcs.py` (создать)

**Interfaces:**
- Produces: `ChunkStore.get_repo_vcs(repo: str) -> tuple[str, str] | None`, `ChunkStore.set_repo_vcs(repo: str, provider: str, base_url: str = "") -> None`.

- [ ] **Step 1: Написать падающий integration-тест** `tests/index/test_repo_vcs.py`

```python
import pytest
from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore

pytestmark = pytest.mark.integration


@pytest.fixture
def store():
    s = Settings()
    st = ChunkStore(s.pg_dsn)
    st.init_schema()
    return st


def test_get_repo_vcs_absent_returns_none(store):
    assert store.get_repo_vcs("nobody/none-xyz") is None


def test_set_then_get_repo_vcs(store):
    store.set_repo_vcs("o/r-test133", "gitlab", "https://gitlab.acme.com")
    assert store.get_repo_vcs("o/r-test133") == ("gitlab", "https://gitlab.acme.com")


def test_set_repo_vcs_upserts(store):
    store.set_repo_vcs("o/r-test133", "github", "")
    assert store.get_repo_vcs("o/r-test133") == ("github", "")
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/index/test_repo_vcs.py -m integration -q`
Expected: FAIL (`AttributeError: ... 'get_repo_vcs'`) — при поднятом Postgres. (Если Postgres не поднят — поднять `docker compose up -d`.)

- [ ] **Step 3: Добавить таблицу в `reviewer/index/schema.sql`** (в конец файла)

```sql
-- Карта платформы VCS репозитория (PRI-133): auto-derive из git remote при
-- `reviewer index`. Читается _create_vcs_provider при ревью (API-only движок)
-- ДО любого API-вызова. Ключ по repo (платформа — свойство репо, не ветки).
CREATE TABLE IF NOT EXISTS repo_vcs (
    repo       text        PRIMARY KEY,
    provider   text        NOT NULL,
    base_url   text        NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 4: Добавить методы в `reviewer/index/store.py`** (после `set_index_meta`)

```python
    def get_repo_vcs(self, repo: str) -> tuple[str, str] | None:
        """Платформа VCS репо: (provider, base_url) или None.

        Отсутствие таблицы (старый индекс без init_schema) равнозначно
        отсутствию записи — резолв провайдера откатится на ENV-дефолт."""
        import psycopg.errors
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT provider, base_url FROM repo_vcs WHERE repo=%s", (repo,)
                ).fetchone()
        except psycopg.errors.UndefinedTable:
            return None
        return (row[0], row[1]) if row else None

    def set_repo_vcs(self, repo: str, provider: str, base_url: str = "") -> None:
        """Записать/обновить платформу VCS репо (UPSERT по repo)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO repo_vcs (repo, provider, base_url, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (repo) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    base_url = EXCLUDED.base_url,
                    updated_at = now()
                """,
                (repo, provider, base_url),
            )
            conn.commit()
```

- [ ] **Step 5: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/index/test_repo_vcs.py -m integration -q`
Expected: PASS (3 теста).

- [ ] **Step 6: Линт + commit**

```bash
.venv/bin/ruff check reviewer/index/store.py
git add reviewer/index/schema.sql reviewer/index/store.py tests/index/test_repo_vcs.py
git commit -m "feat(index): таблица repo_vcs + get/set_repo_vcs (карта платформы репо)"
```

---

### Task 4: `GitLabProvider` (`reviewer/vcs/gitlab.py`)

Реализация `VCSProvider` для GitLab MR через httpx (GitLab API v4). Unit-тесты на `httpx.MockTransport`, зеркалят `test_github.py`.

**Files:**
- Create: `reviewer/vcs/gitlab.py`
- Test: `tests/vcs/test_gitlab.py` (создать)

**Interfaces:**
- Consumes: `reviewer.vcs._http._RetryTransport`, `_FP` (Task 1); `PullRequest`, `ChangedFile`, `InlineComment` из `reviewer.vcs.base`.
- Produces: `GitLabProvider(owner: str, repo: str, token: str, *, base_url: str = "https://gitlab.com", client: httpx.Client | None = None, retry_attempts: int = 3, retry_backoff_base: float = 1.0)` — все 7 методов `VCSProvider`.

- [ ] **Step 1: Написать падающие unit-тесты** `tests/vcs/test_gitlab.py`

```python
import json
import pytest
import httpx
from reviewer.vcs.gitlab import GitLabProvider
from reviewer.vcs.base import InlineComment


def make_provider(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler),
                          base_url="https://gitlab.com/api/v4")
    return GitLabProvider("o", "r", token="t", client=client)


def test_get_pull_request_maps_diff_refs_and_branches():
    def handler(req):
        if req.url.path.endswith("/merge_requests/7"):
            return httpx.Response(200, json={
                "diff_refs": {"base_sha": "aaa", "head_sha": "bbb", "start_sha": "aaa"},
                "target_branch": "main",
                "source_branch": "feature/x",
                "title": "My MR",
                "description": "desc",
                "draft": True,
            })
        return httpx.Response(404)
    p = make_provider(handler)
    pr = p.get_pull_request(7)
    assert pr.base_sha == "aaa"
    assert pr.head_sha == "bbb"
    assert pr.base_ref == "main"
    assert pr.head_ref == "feature/x"
    assert pr.title == "My MR"
    assert pr.body == "desc"
    assert pr.draft is True


def test_get_changed_files_maps_status():
    def handler(req):
        if req.url.path.endswith("/merge_requests/3/changes"):
            return httpx.Response(200, json={"changes": [
                {"old_path": "a.py", "new_path": "a.py", "diff": "@@ -1 +1 @@",
                 "new_file": False, "deleted_file": False, "renamed_file": False},
                {"old_path": "b.py", "new_path": "b.py", "diff": "@@ +1 @@",
                 "new_file": True, "deleted_file": False, "renamed_file": False},
                {"old_path": "c.py", "new_path": "c.py", "diff": "",
                 "new_file": False, "deleted_file": True, "renamed_file": False},
            ]})
        return httpx.Response(404)
    p = make_provider(handler)
    files = p.get_changed_files(3)
    assert (files[0].path, files[0].status) == ("a.py", "modified")
    assert (files[1].path, files[1].status) == ("b.py", "added")
    assert (files[2].path, files[2].status) == ("c.py", "removed")


def test_get_file_at_ref_decodes_base64():
    import base64
    def handler(req):
        if "/repository/files/" in req.url.path:
            return httpx.Response(200, json={
                "content": base64.b64encode(b"hello").decode(), "encoding": "base64"})
        return httpx.Response(404)
    p = make_provider(handler)
    assert p.get_file_at_ref("dir/f.py", "main") == "hello"


def test_get_file_at_ref_404_returns_none():
    p = make_provider(lambda req: httpx.Response(404))
    assert p.get_file_at_ref("missing.py", "main") is None


def test_list_existing_fingerprints_parses_markers():
    def handler(req):
        if req.url.path.endswith("/notes"):
            return httpx.Response(200, json=[{"body": "issue\n<!-- ai-review:abc123 -->"}])
        return httpx.Response(404)
    p = make_provider(handler)
    assert p.list_existing_fingerprints(5) == {"abc123"}


def test_compare_files_maps_diffs():
    def handler(req):
        if req.url.path.endswith("/repository/compare"):
            return httpx.Response(200, json={"diffs": [
                {"old_path": "a.py", "new_path": "a.py", "diff": "@@ @@",
                 "new_file": False, "deleted_file": False, "renamed_file": False},
            ]})
        return httpx.Response(404)
    p = make_provider(handler)
    files = p.compare_files("base", "head")
    assert files[0].path == "a.py"
    assert files[0].status == "modified"


def test_publish_review_posts_summary_note_and_discussion():
    posts = []

    def handler(req):
        if req.method == "GET" and req.url.path.endswith("/merge_requests/5"):
            return httpx.Response(200, json={
                "diff_refs": {"base_sha": "b1", "head_sha": "h1", "start_sha": "s1"},
                "target_branch": "main", "source_branch": "x",
                "title": "T", "description": "", "draft": False})
        if req.method == "POST" and req.url.path.endswith("/notes"):
            posts.append(("note", json.loads(req.content)))
            return httpx.Response(201, json={"id": 1})
        if req.method == "POST" and req.url.path.endswith("/discussions"):
            posts.append(("discussion", json.loads(req.content)))
            return httpx.Response(201, json={"id": "d1"})
        return httpx.Response(404)

    p = make_provider(handler)
    p.publish_review(5, "h1", "Сводка",
                     [InlineComment("a.py", 10, "RIGHT", "body\n<!-- ai-review:fp1 -->")])
    kinds = [k for k, _ in posts]
    assert "note" in kinds and "discussion" in kinds
    note = next(b for k, b in posts if k == "note")
    assert note["body"] == "Сводка"
    disc = next(b for k, b in posts if k == "discussion")
    assert disc["body"] == "body\n<!-- ai-review:fp1 -->"
    pos = disc["position"]
    assert pos["position_type"] == "text"
    assert pos["base_sha"] == "b1" and pos["head_sha"] == "h1" and pos["start_sha"] == "s1"
    assert pos["new_path"] == "a.py" and pos["new_line"] == 10


def test_publish_review_left_side_uses_old_line():
    posts = []

    def handler(req):
        if req.method == "GET" and req.url.path.endswith("/merge_requests/6"):
            return httpx.Response(200, json={
                "diff_refs": {"base_sha": "b", "head_sha": "h", "start_sha": "s"},
                "target_branch": "main", "source_branch": "x",
                "title": "T", "description": "", "draft": False})
        if req.method == "POST" and req.url.path.endswith("/discussions"):
            posts.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "d"})
        if req.method == "POST" and req.url.path.endswith("/notes"):
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(404)

    p = make_provider(handler)
    p.publish_review(6, "h", "S", [InlineComment("a.py", 4, "LEFT", "b")])
    pos = posts[0]["position"]
    assert pos["old_line"] == 4 and "new_line" not in pos
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/vcs/test_gitlab.py -q`
Expected: FAIL (`ModuleNotFoundError: reviewer.vcs.gitlab`).

- [ ] **Step 3: Реализовать `reviewer/vcs/gitlab.py`**

```python
from __future__ import annotations
import base64
from urllib.parse import quote

import httpx

from reviewer.vcs.base import PullRequest, ChangedFile, InlineComment
from reviewer.vcs._http import _RetryTransport, _FP


def _file_status(ch: dict) -> str:
    if ch.get("new_file"):
        return "added"
    if ch.get("deleted_file"):
        return "removed"
    if ch.get("renamed_file"):
        return "renamed"
    return "modified"


def _to_changed_file(ch: dict) -> ChangedFile:
    return ChangedFile(
        path=ch.get("new_path") or ch.get("old_path"),
        status=_file_status(ch),
        patch=ch.get("diff") or None,
    )


class GitLabProvider:
    """VCSProvider для GitLab Merge Requests (API v4) поверх httpx.

    `number` — это MR `iid` (per-project счётчик), прямой аналог номера PR.
    Путь проекта `owner/name` URL-энкодится в `:id`.
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        token: str,
        *,
        base_url: str = "https://gitlab.com",
        client: httpx.Client | None = None,
        retry_attempts: int = 3,
        retry_backoff_base: float = 1.0,
    ):
        self.owner, self.repo = owner, repo
        self._proj = quote(f"{owner}/{repo}", safe="")
        if client is None:
            transport = _RetryTransport(
                httpx.HTTPTransport(),
                attempts=retry_attempts,
                backoff_base=retry_backoff_base,
            )
            client = httpx.Client(
                base_url=f"{base_url.rstrip('/')}/api/v4",
                headers={"PRIVATE-TOKEN": token},
                timeout=30,
                transport=transport,
            )
        self._c = client

    def close(self) -> None:
        self._c.close()

    def _base(self) -> str:
        return f"/projects/{self._proj}"

    def _mr(self, number: int) -> str:
        return f"{self._base()}/merge_requests/{number}"

    def get_pull_request(self, number: int) -> PullRequest:
        d = self._c.get(self._mr(number)).raise_for_status().json()
        refs = d.get("diff_refs") or {}
        return PullRequest(
            number=number,
            base_sha=refs.get("base_sha", ""),
            head_sha=refs.get("head_sha", ""),
            base_ref=d.get("target_branch", ""),
            title=d.get("title", ""),
            body=d.get("description") or "",
            draft=bool(d.get("draft", d.get("work_in_progress", False))),
            head_ref=d.get("source_branch"),
        )

    def get_changed_files(self, number: int) -> list[ChangedFile]:
        d = self._c.get(f"{self._mr(number)}/changes").raise_for_status().json()
        return [_to_changed_file(ch) for ch in d.get("changes", [])]

    def get_file_at_ref(self, path: str, ref: str) -> str | None:
        r = self._c.get(
            f"{self._base()}/repository/files/{quote(path, safe='')}",
            params={"ref": ref},
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return base64.b64decode(r.json()["content"]).decode("utf-8", "replace")

    def list_existing_fingerprints(self, number: int) -> set[str]:
        fps, page = set(), 1
        while True:
            r = self._c.get(
                f"{self._mr(number)}/notes",
                params={"per_page": 100, "page": page},
            ).raise_for_status()
            batch = r.json()
            for note in batch:
                fps.update(_FP.findall(note.get("body", "")))
            if len(batch) < 100:
                break
            page += 1
        return fps

    def compare_files(self, base_sha: str, head_sha: str) -> list[ChangedFile]:
        r = self._c.get(
            f"{self._base()}/repository/compare",
            params={"from": base_sha, "to": head_sha},
        ).raise_for_status()
        return [_to_changed_file(ch) for ch in r.json().get("diffs", [])]

    def publish_review(
        self,
        number: int,
        head_sha: str,
        summary: str,
        comments: list[InlineComment],
    ) -> None:
        # Сводка — обычный нот MR (у GitLab нет объекта «review»).
        self._c.post(
            f"{self._mr(number)}/notes", json={"body": summary}
        ).raise_for_status()
        if not comments:
            return
        # Inline-комментарии — отдельные discussions с позицией. Тройку SHA
        # берём из diff_refs MR (head_sha из аргумента может расходиться).
        d = self._c.get(self._mr(number)).raise_for_status().json()
        refs = d.get("diff_refs") or {}
        for c in comments:
            position = {
                "position_type": "text",
                "base_sha": refs.get("base_sha"),
                "start_sha": refs.get("start_sha"),
                "head_sha": refs.get("head_sha"),
                "new_path": c.path,
                "old_path": c.path,
            }
            # RIGHT → строка новой версии, LEFT → строка старой.
            # Мультистрочные комментарии деградируют в однострочный (на c.line).
            if c.side == "RIGHT":
                position["new_line"] = c.line
            else:
                position["old_line"] = c.line
            self._c.post(
                f"{self._mr(number)}/discussions",
                json={"body": c.body, "position": position},
            ).raise_for_status()
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/vcs/test_gitlab.py -q`
Expected: PASS (8 тестов).

- [ ] **Step 5: Линт + commit**

```bash
.venv/bin/ruff check reviewer/vcs/gitlab.py tests/vcs/test_gitlab.py
git add reviewer/vcs/gitlab.py tests/vcs/test_gitlab.py
git commit -m "feat(vcs): GitLabProvider — ревью Merge Request через GitLab API v4"
```

---

### Task 5: Settings + резолв провайдера в `_create_vcs_provider`

**Files:**
- Modify: `reviewer/config/settings.py:78-81` (добавить поля)
- Modify: `reviewer/services/review_service.py:116-124` (`_create_vcs_provider`)
- Test: `tests/services/test_vcs_resolution.py` (создать)

**Interfaces:**
- Consumes: `GitLabProvider` (Task 4); `ChunkStore.get_repo_vcs` (Task 3); `derive_vcs_from_remote` не нужен здесь.
- Produces: `Settings.vcs_provider: str`, `Settings.gitlab_token: str`, `Settings.gitlab_url: str`; `ReviewService._create_vcs_provider(owner, repo) -> VCSProvider` (резолвит провайдер из `repo_vcs`, иначе ENV-дефолт).

- [ ] **Step 1: Написать падающий unit-тест** `tests/services/test_vcs_resolution.py`

```python
from types import SimpleNamespace
from reviewer.config.settings import Settings
from reviewer.services.review_service import ReviewService
from reviewer.vcs.github import GitHubProvider
from reviewer.vcs.gitlab import GitLabProvider


def _service(repo_vcs_row):
    settings = Settings(github_token="gh", gitlab_token="gl", vcs_provider="github")
    store = SimpleNamespace(get_repo_vcs=lambda repo: repo_vcs_row)
    components = SimpleNamespace(store=store)
    return ReviewService(settings, components)


def test_resolves_gitlab_when_repo_vcs_says_gitlab():
    svc = _service(("gitlab", "https://gitlab.acme.com"))
    p = svc._create_vcs_provider("o", "r")
    assert isinstance(p, GitLabProvider)
    p.close()


def test_falls_back_to_env_default_github_when_absent():
    svc = _service(None)
    p = svc._create_vcs_provider("o", "r")
    assert isinstance(p, GitHubProvider)
    p.close()


def test_resolves_github_when_repo_vcs_says_github():
    svc = _service(("github", ""))
    p = svc._create_vcs_provider("o", "r")
    assert isinstance(p, GitHubProvider)
    p.close()
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/services/test_vcs_resolution.py -q`
Expected: FAIL (`TypeError`: Settings не знает `gitlab_token`/`vcs_provider`, либо провайдер всегда GitHub).

- [ ] **Step 3: Добавить поля в `reviewer/config/settings.py`** (в блок `# github`, после строки 81)

```python
    # multi-platform VCS (PRI-133): тип провайдера резолвится из repo_vcs
    # (auto-derive при reviewer index), здесь — фолбэк и токены по платформе.
    vcs_provider: str = "github"          # фолбэк, когда repo_vcs пуст / remote не распознан
    gitlab_token: str = ""                # токен платформы gitlab
    gitlab_url: str = "https://gitlab.com"  # дефолт base-url; фолбэк для self-hosted
```

- [ ] **Step 4: Переписать `_create_vcs_provider`** в `reviewer/services/review_service.py:116-124`

```python
    def _create_vcs_provider(self, owner: str, repo: str) -> VCSProvider:
        """Создать VCS-провайдер по платформе репо (repo_vcs → ENV-фолбэк).

        Тип резолвится ДО любого API-вызова: дешёвое чтение repo_vcs из стора.
        Токен берётся из ENV по платформе (секретов в .review.yml нет)."""
        from reviewer.services.repo_id import normalize_repo
        from reviewer.vcs.gitlab import GitLabProvider
        full = normalize_repo(f"{owner}/{repo}")
        row = self.components.store.get_repo_vcs(full)
        provider, base_url = row if row else (self.settings.vcs_provider, "")
        if provider == "gitlab":
            return GitLabProvider(
                owner,
                repo,
                token=self.settings.gitlab_token,
                base_url=base_url or self.settings.gitlab_url,
                retry_attempts=self.settings.github_retry_attempts,
                retry_backoff_base=self.settings.github_retry_backoff_base,
            )
        return GitHubProvider(
            owner,
            repo,
            token=self.settings.github_token,
            retry_attempts=self.settings.github_retry_attempts,
            retry_backoff_base=self.settings.github_retry_backoff_base,
        )
```
(Убедиться, что `VCSProvider` импортирован в `review_service.py` — он уже используется в сигнатуре `prepare`. `GitHubProvider` уже импортирован.)

- [ ] **Step 5: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/services/test_vcs_resolution.py -q`
Expected: PASS (3 теста).

- [ ] **Step 6: Регрессия сервисного слоя** (фейковый store в существующих тестах мог не иметь `get_repo_vcs`)

Run: `.venv/bin/pytest tests/services -q`
Expected: PASS. Если упало с `AttributeError: get_repo_vcs` — у фейкового стора в упавшем тесте добавить `get_repo_vcs` (вернуть `None`), это правка теста, не кода.

- [ ] **Step 7: Линт + commit**

```bash
.venv/bin/ruff check reviewer/config/settings.py reviewer/services/review_service.py tests/services/test_vcs_resolution.py
git add reviewer/config/settings.py reviewer/services/review_service.py tests/services/test_vcs_resolution.py
git commit -m "feat(services): резолв VCS-провайдера из repo_vcs + ENV-фолбэк (gitlab/github)"
```

---

### Task 6: Персист платформы при `reviewer index` + `.env.example` + CLAUDE.md

Замкнуть цепочку: индексация пишет платформу в `repo_vcs`. Документировать ENV.

**Files:**
- Modify: `reviewer/entrypoints/cli.py:166-167` (после `set_index_meta`)
- Modify: `.env.example` (добавить VCS-блок)
- Modify: `CLAUDE.md` (краткий факт в «Неочевидные факты»)

**Interfaces:**
- Consumes: `derive_vcs_from_remote` (Task 2), `remote_url` (gitutil), `ChunkStore.set_repo_vcs` (Task 3).

- [ ] **Step 1: Дописать персист в `reviewer/entrypoints/cli.py`** сразу после `c.store.set_index_meta(repo_id, bref, sha)` (строка 167)

```python
        # Платформа VCS репо (PRI-133): auto-derive из git remote локального
        # клона → repo_vcs. Читается при ревью (API-only) для выбора провайдера.
        from reviewer.services.repo_id import derive_vcs_from_remote
        vcs = derive_vcs_from_remote(remote_url(repo) or "")
        if vcs:
            c.store.set_repo_vcs(repo_id, vcs[0], vcs[1])
            click.echo(f"VCS: {vcs[0]}{(' @ ' + vcs[1]) if vcs[1] else ''}")
```
(`remote_url` уже импортирован в `cli.py:12`. Здесь `repo` — позиционный аргумент-путь к клону.)

- [ ] **Step 2: Прогон smoke-теста CLI на этом репо (GitHub remote)**

Run: `.venv/bin/reviewer index . --ref dev --repo mimfort/rag_for_git 2>&1 | tail -3`
Expected: в выводе строка `VCS: github`. (Требует поднятых Postgres/Neo4j; индекс уже свежий — повторный прогон дешёвый.)

- [ ] **Step 3: Проверить запись в `repo_vcs`** (диагностика)

Run: `.venv/bin/python -c "from reviewer.config.settings import Settings; from reviewer.index.store import ChunkStore; print(ChunkStore(Settings().pg_dsn).get_repo_vcs('mimfort/rag_for_git'))"`
Expected: `('github', '')`.

- [ ] **Step 4: Дополнить `.env.example`** — добавить после блока `GITHUB_*` (после строки `GITHUB_RETRY_BACKOFF_BASE`)

```bash
# multi-platform VCS (PRI-133): тип провайдера определяется автоматически из git
# remote при `reviewer index`; здесь — фолбэк-платформа и токены по платформе.
VCS_PROVIDER=github                # фолбэк, если репо не индексирован/remote не распознан
GITLAB_TOKEN=                      # PAT GitLab (api scope) — для ревью GitLab MR
GITLAB_URL=https://gitlab.com      # base-url; для self-hosted выводится из remote автоматически
```

- [ ] **Step 5: Добавить факт в `CLAUDE.md`** — в раздел «Неочевидные факты», новый буллет:

```markdown
- **Мульти-платформа VCS (GitHub + GitLab).** Тип провайдера — свойство репо, не PR. `reviewer index` определяет платформу из `git remote` (`derive_vcs_from_remote`) и пишет в таблицу `repo_vcs(repo→provider,base_url)`. При ревью (API-only движок) `_create_vcs_provider` читает `repo_vcs` ДО любого API-вызова и выбирает `GitHubProvider`/`GitLabProvider`; токен — из ENV по платформе (`GITHUB_TOKEN`/`GITLAB_TOKEN`, `GITLAB_URL` для self-hosted). Фолбэк при пустом `repo_vcs` — `VCS_PROVIDER` (дефолт github), что сохраняет обратную совместимость. Секретов в `.review.yml` нет (нет блока `vcs:`).
```

- [ ] **Step 6: Финальный прогон всего unit-набора + линт**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключены по дефолту).
Run: `.venv/bin/ruff check reviewer/ tests/`
Expected: без новых ошибок в тронутых файлах.

- [ ] **Step 7: Commit**

```bash
git add reviewer/entrypoints/cli.py .env.example CLAUDE.md
git commit -m "feat(index): персист платформы VCS в repo_vcs при index + docs/.env.example"
```

---

## Self-Review (проверено при написании плана)

**Spec coverage:**
- gitlab.py (7 методов) → Task 4 ✓
- блок vcs: в .review.yml → **сознательно НЕ реализуется** (см. спека, отклонения) → отражено в Task 5/6 (нет парсинга yml) ✓
- ENV `VCS_PROVIDER`/`GITLAB_TOKEN`/`GITLAB_URL` → Task 5 (Settings) + Task 6 (.env.example) ✓
- `_create_vcs_provider` рефактор → Task 5 ✓ (5 call-site'ов не меняются — сигнатура та же)
- auto-derive из git remote + repo_vcs → Task 2 (derive) + Task 3 (store) + Task 6 (cli persist) ✓
- DRY `_RetryTransport` → Task 1 ✓
- часть Г (configure-review не клоберит vcs:) → не требуется (нет блока vcs:), задокументировано в спеке ✓
- тесты: gitlab unit (Task 4), derive (Task 2), repo_vcs integration (Task 3), resolution (Task 5) ✓

**Type consistency:** `get_repo_vcs -> tuple[str,str]|None` (Task 3) ↔ потребляется в Task 5 как `row if row else (...)`. `GitLabProvider(... base_url=..., retry_attempts=..., retry_backoff_base=...)` (Task 4) ↔ вызывается в Task 5 с теми же kwargs. `derive_vcs_from_remote -> tuple[str,str]|None` (Task 2) ↔ `vcs[0]`/`vcs[1]` в Task 6. ✓

**Placeholders:** нет TBD/TODO; весь код приведён дословно. ✓

**Зависимости задач:** 1 → (2,3,4 независимы) → 5 (нужны 3,4) → 6 (нужны 2,3). Порядок 1→2→3→4→5→6 безопасен.
