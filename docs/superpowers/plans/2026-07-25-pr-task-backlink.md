# Обратный линк задачи в тело PR — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `finish_task` после записи в доску fail-soft дописывает в начало тела PR строку с кликабельной markdown-ссылкой на задачу (GitHub и GitLab).

**Architecture:** Чистый юнит `reviewer/tasks/pr_backlink.py` (парсинг PR-ссылки + сборка тела, без I/O) + новый метод `update_pull_request_body` в обеих реализациях `VCSProvider` + оркестрация в `MCPReviewService.finish_task`. Источник URL задачи — нормализованный бриф из уже выполняемого write-through, поэтому контракт `TaskBoardProvider` и его фикстура не меняются.

**Tech Stack:** Python 3.11+, httpx (VCS-провайдеры), pytest, ruff (line-length 100).

Спека: `docs/superpowers/specs/2026-07-25-pr-task-backlink-design.md`.

## Global Constraints

- Язык проекта — русский: комментарии, докстринги, сообщения. Скиллы под `plugin/` — на английском.
- Коммиты — Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By` / упоминаний Claude).
- `ruff check .` — line-length 100, target py311.
- Все новые тесты — unit: без сети, Postgres и Neo4j. Внешние сервисы мокаются.
- Fail-soft: ни одна ошибка бэклинка не превращает успешный `finish_task` в `status: "error"`.
- Внешний контракт тула не ломается: `reindexed` в payload остаётся `bool`.
- Любая правка контента под `plugin/` требует прогона `update_codex_plugin_manifest.py` (иначе краснеют install-тесты).

---

## File Structure

| Файл | Ответственность |
|---|---|
| `reviewer/tasks/pr_backlink.py` (создать) | Чистые функции: `parse_pr_url`, `apply_backlink`, константа `MARKER`, dataclass `PRTarget`. Ноль I/O |
| `reviewer/vcs/base.py` (правка) | Объявление `update_pull_request_body` в `Protocol` |
| `reviewer/vcs/github.py` (правка) | `PATCH /repos/{o}/{r}/pulls/{n}` c `{"body": …}` |
| `reviewer/vcs/gitlab.py` (правка) | `PUT /projects/{id}/merge_requests/{iid}` c `{"description": …}` |
| `reviewer/services/review_service.py` (правка) | `_create_vcs_provider` принимает явные `platform` / `base_url` |
| `reviewer/mcp/service.py` (правка) | `_write_through` возвращает бриф; новый `_backlink_pr`; вызов из `finish_task` |
| `reviewer/entrypoints/mcp_server.py` (правка) | Докстринг тула упоминает бэклинк |
| `plugin/skills/finish-task/SKILL.md` (правка) | Offer предупреждает о правке тела PR; отчёт озвучивает результат |
| `tests/tasks/test_pr_backlink.py` (создать) | Юнит-тесты чистых функций |
| `tests/vcs/test_github.py`, `tests/vcs/test_gitlab.py` (правка) | Тесты нового метода на `httpx.MockTransport` |
| `tests/mcp/test_finish_task.py` (правка) | Тесты оркестрации на фейках |
| `tests/skills/test_finish_task_skill.py` (правка) | Guard на текст скилла |

---

### Task 1: Чистый юнит `pr_backlink`

**Files:**
- Create: `reviewer/tasks/pr_backlink.py`
- Test: `tests/tasks/test_pr_backlink.py`

**Interfaces:**
- Consumes: ничего (первый таск, зависимостей нет).
- Produces:
  - `MARKER: str` — константа `"<!-- reviewer:task-link -->"`
  - `PRTarget` — frozen dataclass с полями `platform: str`, `owner: str`, `repo: str`, `number: int`, `base_url: str`
  - `parse_pr_url(url: str) -> PRTarget | None`
  - `apply_backlink(body: str, key: str, task_url: str) -> str | None`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/tasks/test_pr_backlink.py`:

```python
"""Юниты обратного линка задачи в тело PR: разбор ссылки + идемпотентная вставка."""
from reviewer.tasks.pr_backlink import MARKER, apply_backlink, parse_pr_url

URL = "https://ru.yougile.com/team/686c049c8af8/#PRI-216"


def test_parse_github_pr_url():
    t = parse_pr_url("https://github.com/mimfort/rag_for_git/pull/128")
    assert (t.platform, t.owner, t.repo, t.number) == (
        "github", "mimfort", "rag_for_git", 128)
    # GitHubProvider ходит в api.github.com — хост из ссылки не нужен
    assert t.base_url == ""


def test_parse_gitlab_mr_url_with_nested_groups():
    t = parse_pr_url("https://gitlab.example.ru/team/sub/svc/-/merge_requests/42")
    assert (t.platform, t.owner, t.repo, t.number) == ("gitlab", "team/sub", "svc", 42)
    # self-hosted: базовый URL API берётся из самой ссылки
    assert t.base_url == "https://gitlab.example.ru"


def test_parse_gitlab_mr_url_flat_namespace():
    t = parse_pr_url("https://gitlab.com/group/proj/-/merge_requests/3")
    assert (t.owner, t.repo, t.number) == ("group", "proj", 3)


def test_parse_ignores_url_tails():
    for tail in ("/files", "?tab=files", "#note_1", "/"):
        t = parse_pr_url(f"https://github.com/o/r/pull/7{tail}")
        assert t is not None and t.number == 7, tail


def test_parse_rejects_unrecognized():
    for bad in ("", "url", "https://github.com/o/r/pulls",
                "https://github.com/o/r/issues/7", "not a url at all"):
        assert parse_pr_url(bad) is None, bad


def test_apply_backlink_prepends_line_and_marker():
    out = apply_backlink("## Задача\n\nтекст", "PRI-216", URL)
    assert out == f"Задача: [PRI-216]({URL})\n{MARKER}\n\n## Задача\n\nтекст"


def test_apply_backlink_on_empty_body():
    assert apply_backlink("", "PRI-216", URL) == f"Задача: [PRI-216]({URL})\n{MARKER}"


def test_apply_backlink_noop_when_marker_present():
    body = f"Задача: [PRI-216]({URL})\n{MARKER}\n\nтекст"
    assert apply_backlink(body, "PRI-216", URL) is None


def test_apply_backlink_noop_when_url_already_in_body():
    # ручная ссылка без маркера уважается — дубля не будет
    assert apply_backlink(f"## Задача\n\n[PRI-216]({URL}) — описание", "PRI-216", URL) is None
```

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/test_pr_backlink.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.tasks.pr_backlink'`

- [ ] **Step 3: Написать реализацию**

Создать `reviewer/tasks/pr_backlink.py`:

```python
"""Обратный линк: кликабельная ссылка на задачу в теле PR.

`finish_task` пишет PR-ссылку в задачу; здесь — обратная сторона связи.
Чистые функции без I/O: разбор ссылки на PR/MR и идемпотентная сборка тела.
Сам HTTP-вызов делает VCS-провайдер, оркестрация — MCPReviewService.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Скрытый маркер идемпотентности — по образцу <!-- ai-review:hash --> в комментариях
# ревью: HTML-комментарий не рендерится ни на GitHub, ни на GitLab.
MARKER = "<!-- reviewer:task-link -->"

# Платформу определяет форма пути, а не хост: так self-hosted GitLab работает
# без предварительной индексации репо (repo_vcs может быть пуст).
_GITHUB_RE = re.compile(
    r"^https?://[^/\s]+/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)"
)
_GITLAB_RE = re.compile(
    r"^(?P<root>https?://[^/\s]+)/(?P<path>[^\s?#]+?)/-/merge_requests/(?P<number>\d+)"
)


@dataclass(frozen=True)
class PRTarget:
    """Разобранная ссылка на PR/MR."""
    platform: str    # "github" | "gitlab"
    owner: str       # для GitLab — путь группы любой глубины (team/sub)
    repo: str
    number: int
    base_url: str    # схема+хост; "" для github (провайдер ходит в api.github.com)


def parse_pr_url(url: str) -> PRTarget | None:
    """PRTarget из ссылки на PR/MR; None — если ссылка не распознана.

    Хвосты (/files, ?tab=…, #note_1, завершающий /) игнорируются: регулярки
    не якорят конец строки."""
    if not url:
        return None
    m = _GITHUB_RE.match(url)
    if m:
        return PRTarget("github", m.group("owner"), m.group("repo"),
                        int(m.group("number")), "")
    m = _GITLAB_RE.match(url)
    if m:
        parts = [p for p in m.group("path").split("/") if p]
        if len(parts) < 2:
            return None
        return PRTarget("gitlab", "/".join(parts[:-1]), parts[-1],
                        int(m.group("number")), m.group("root"))
    return None


def apply_backlink(body: str, key: str, task_url: str) -> str | None:
    """Новое тело PR со строкой-ссылкой в начале; None — если писать не надо.

    Идемпотентность двойная: маркер (наша прошлая правка) и сам URL задачи
    (ручная ссылка автора PR — её не дублируем)."""
    body = body or ""
    if MARKER in body or task_url in body:
        return None
    line = f"Задача: [{key}]({task_url})\n{MARKER}"
    return line if not body.strip() else f"{line}\n\n{body}"
```

- [ ] **Step 4: Прогнать тесты — убедиться, что зелёные**

Run: `.venv/bin/pytest tests/tasks/test_pr_backlink.py -q && .venv/bin/ruff check reviewer/tasks/pr_backlink.py tests/tasks/test_pr_backlink.py`
Expected: 9 passed, ruff — `All checks passed!`

- [ ] **Step 5: Коммит**

```bash
git add reviewer/tasks/pr_backlink.py tests/tasks/test_pr_backlink.py
git commit -m "feat(tasks): юнит обратного линка задачи в тело PR"
```

---

### Task 2: `update_pull_request_body` в обоих VCS-провайдерах

**Files:**
- Modify: `reviewer/vcs/base.py` (Protocol `VCSProvider`, ~строка 89-97)
- Modify: `reviewer/vcs/github.py`
- Modify: `reviewer/vcs/gitlab.py`
- Test: `tests/vcs/test_github.py`, `tests/vcs/test_gitlab.py`

**Interfaces:**
- Consumes: ничего из Task 1.
- Produces: `VCSProvider.update_pull_request_body(number: int, body: str) -> None` — реализован в `GitHubProvider` и `GitLabProvider`. Ошибки HTTP пробрасываются наружу (`raise_for_status`); fail-soft делает вызывающая сторона в Task 3.

- [ ] **Step 1: Написать падающие тесты**

Дописать в конец `tests/vcs/test_github.py`:

```python
def test_update_pull_request_body_patches_body():
    seen = {}

    def handler(req):
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["json"] = json.loads(req.content)
        return httpx.Response(200, json={})

    make_provider(handler).update_pull_request_body(7, "новое тело")
    assert seen["method"] == "PATCH"
    assert seen["path"] == "/repos/o/r/pulls/7"
    assert seen["json"] == {"body": "новое тело"}


def test_update_pull_request_body_raises_on_forbidden():
    p = make_provider(lambda req: httpx.Response(403, json={"message": "no"}))
    with pytest.raises(httpx.HTTPStatusError):
        p.update_pull_request_body(7, "тело")
```

Дописать в конец `tests/vcs/test_gitlab.py`:

```python
def test_update_pull_request_body_puts_description():
    seen = {}

    def handler(req):
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["json"] = json.loads(req.content)
        return httpx.Response(200, json={})

    make_provider(handler).update_pull_request_body(7, "новое тело")
    assert seen["method"] == "PUT"
    # путь проекта URL-энкодится в :id
    assert seen["path"] == "/api/v4/projects/o%2Fr/merge_requests/7"
    assert seen["json"] == {"description": "новое тело"}
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/vcs/test_github.py -q -k update_pull_request_body && .venv/bin/pytest tests/vcs/test_gitlab.py -q -k update_pull_request_body`
Expected: FAIL — `AttributeError: 'GitHubProvider' object has no attribute 'update_pull_request_body'` (и то же для GitLab)

- [ ] **Step 3: Написать реализации**

В `reviewer/vcs/base.py`, в `Protocol` `VCSProvider`, сразу после строки `def get_pull_request(self, number: int) -> PullRequest: ...`:

```python
    def update_pull_request_body(self, number: int, body: str) -> None: ...
```

В `reviewer/vcs/github.py`, сразу после метода `get_pull_request`:

```python
    def update_pull_request_body(self, number: int, body: str) -> None:
        """Заменить тело PR (обратный линк задачи из finish_task)."""
        self._c.patch(
            f"{self._base()}/pulls/{number}", json={"body": body}
        ).raise_for_status()
```

В `reviewer/vcs/gitlab.py`, сразу после метода `get_pull_request`:

```python
    def update_pull_request_body(self, number: int, body: str) -> None:
        """Заменить описание MR (обратный линк задачи из finish_task)."""
        self._c.put(self._mr(number), json={"description": body}).raise_for_status()
```

- [ ] **Step 4: Прогнать тесты — убедиться, что зелёные**

Run: `.venv/bin/pytest tests/vcs -q && .venv/bin/ruff check reviewer/vcs tests/vcs`
Expected: все тесты `tests/vcs` passed, ruff — `All checks passed!`

- [ ] **Step 5: Коммит**

```bash
git add reviewer/vcs/base.py reviewer/vcs/github.py reviewer/vcs/gitlab.py \
        tests/vcs/test_github.py tests/vcs/test_gitlab.py
git commit -m "feat(vcs): правка тела PR/MR в провайдерах GitHub и GitLab"
```

---

### Task 3: Оркестрация в `finish_task`

**Files:**
- Modify: `reviewer/services/review_service.py:116-141` (`_create_vcs_provider`)
- Modify: `reviewer/mcp/service.py:454-467` (`_write_through`), `:566-585` (`finish_task`), `:631` (`create_task`)
- Modify: `reviewer/entrypoints/mcp_server.py:135-140` (докстринг тула)
- Test: `tests/mcp/test_finish_task.py`

**Interfaces:**
- Consumes: `MARKER`, `parse_pr_url`, `apply_backlink`, `PRTarget` из Task 1; `update_pull_request_body(number, body)` из Task 2.
- Produces: `finish_task` возвращает дополнительное поле `task_link_added: bool`; причины пропуска — строками в существующем списке `warnings`.

- [ ] **Step 1: Написать падающие тесты**

В `tests/mcp/test_finish_task.py` дополнить фейки и добавить тесты.

Заменить `normalize` в классе `_Provider` (строки 59-61) так, чтобы бриф отдавал url:

```python
    def normalize(self, raw):
        return {"key": raw.key, "title": raw.title, "status": "done",
                "project": "PRI", "description": raw.description,
                "url": "https://board.example/#PRI-10"}
```

Добавить фейковый VCS и параметр `vcs` в `_Svc` (после класса `_Provider`):

```python
class _FakeVCS:
    def __init__(self, body="## Что сделано\n\nтекст", fail=False):
        self.body = body
        self.fail = fail
        self.updated = []
        self.closed = False

    def get_pull_request(self, number):
        if self.fail:
            raise RuntimeError("403 нет прав")
        return type("PR", (), {"number": number, "body": self.body})()

    def update_pull_request_body(self, number, body):
        self.updated.append((number, body))

    def close(self):
        self.closed = True
```

В `_Svc.__init__` — добавить параметр `vcs=None` в сигнатуру и две строки в тело (после `self.components = …`):

```python
        self._vcs_factory = (lambda owner, name: vcs) if vcs is not None else None
        self._review_service = None   # реальный ReviewService не нужен: VCS даёт фабрика
```

Добавить тесты в конец файла:

```python
PR_URL = "https://github.com/o/r/pull/7"


def test_finish_task_backlinks_task_into_pr_body():
    # Связь двусторонняя: PR-ссылка ушла в задачу, ссылка на задачу — в тело PR.
    vcs = _FakeVCS()
    out = _Svc(["fake"], vcs=vcs).finish_task("PRI-10", PR_URL)
    assert out["status"] == "ok"
    assert out["task_link_added"] is True
    assert len(vcs.updated) == 1
    number, body = vcs.updated[0]
    assert number == 7
    assert body.startswith("Задача: [PRI-10](https://board.example/#PRI-10)")
    assert "<!-- reviewer:task-link -->" in body
    assert body.endswith("## Что сделано\n\nтекст")


def test_finish_task_backlink_idempotent_on_second_run():
    vcs = _FakeVCS(body="Задача: [PRI-10](https://board.example/#PRI-10)\n"
                        "<!-- reviewer:task-link -->\n\nтекст")
    out = _Svc(["fake"], vcs=vcs).finish_task("PRI-10", PR_URL)
    assert out["status"] == "ok"
    assert out["task_link_added"] is False
    assert vcs.updated == []
    assert out["warnings"] == []


def test_finish_task_backlink_failsoft_on_vcs_error():
    # Доска уже закрыта — сбой правки PR не откатывает успех finish_task.
    vcs = _FakeVCS(fail=True)
    out = _Svc(["fake"], vcs=vcs).finish_task("PRI-10", PR_URL)
    assert out["status"] == "ok"
    assert out["done_set"] is True
    assert out["task_link_added"] is False
    assert any("403" in w for w in out["warnings"])


def test_finish_task_backlink_skipped_without_task_url():
    class _NoUrl(_Provider):
        def normalize(self, raw):
            return {"key": raw.key, "title": raw.title, "status": "done",
                    "project": "PRI", "description": raw.description, "url": None}

    vcs = _FakeVCS()
    out = _Svc(["fake"], _NoUrl(), vcs=vcs).finish_task("PRI-10", PR_URL)
    assert out["status"] == "ok"
    assert out["task_link_added"] is False
    assert vcs.updated == []
    assert any("url_template" in w for w in out["warnings"])


def test_finish_task_backlink_skipped_on_unparsable_pr_url():
    vcs = _FakeVCS()
    out = _Svc(["fake"], vcs=vcs).finish_task("PRI-10", "не ссылка")
    assert out["status"] == "ok"
    assert out["task_link_added"] is False
    assert vcs.updated == []


def test_finish_task_backlink_skipped_when_writethrough_failed():
    # Без брифа неоткуда взять url задачи — но finish всё равно ok.
    class _NoRaw(_Provider):
        def fetch_one(self, key):
            return None

    vcs = _FakeVCS()
    out = _Svc(["fake"], _NoRaw(), vcs=vcs).finish_task("PRI-10", PR_URL)
    assert out["status"] == "ok"
    assert out["reindexed"] is False
    assert out["task_link_added"] is False
    assert vcs.updated == []
```

Существующий тест `test_finish_task_migrates_legacy_status_field_and_done_column` (строка ~138) ассертит точное число warnings, а теперь к двум migration-warning добавится warning про нераспознанный `pr_url` (`"url"`). Заменить его ассерт:

```python
    assert len(out["warnings"]) == 3   # 2 migration + пропуск бэклинка (pr_url не распознан)
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/mcp/test_finish_task.py -q`
Expected: FAIL — новые тесты падают на `KeyError: 'task_link_added'`

- [ ] **Step 3: Расширить `_create_vcs_provider` явной платформой**

В `reviewer/services/review_service.py` заменить тело `_create_vcs_provider` (строки 116-141) на:

```python
    def _create_vcs_provider(
        self,
        owner: str,
        repo: str,
        platform: str | None = None,
        base_url: str | None = None,
    ) -> VCSProvider:
        """Создать VCS-провайдер по платформе репо (repo_vcs → ENV-фолбэк).

        Тип резолвится ДО любого API-вызова: дешёвое чтение repo_vcs из стора.
        Токен берётся из ENV по платформе (секретов в .review.yml нет).
        Явные platform/base_url побеждают repo_vcs: ссылка на PR — более прямое
        свидетельство платформы, чем таблица, где репо может отсутствовать
        (иначе GitLab-MR в непроиндексированном репо ушёл бы в GitHub-фолбэк)."""
        from reviewer.services.repo_id import normalize_repo
        from reviewer.vcs.gitlab import GitLabProvider
        full = normalize_repo(f"{owner}/{repo}")
        row = self.components.store.get_repo_vcs(full)
        stored, stored_url = row if row else (self.settings.vcs_provider, "")
        provider = platform or stored
        resolved_url = base_url or stored_url
        if provider == "gitlab":
            return GitLabProvider(
                owner,
                repo,
                token=self.settings.gitlab_token,
                base_url=resolved_url or self.settings.gitlab_url,
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

- [ ] **Step 4: Вернуть бриф из `_write_through`**

В `reviewer/mcp/service.py` заменить `_write_through` (строки 454-467) на:

```python
    def _write_through(self, provider: TaskBoardProvider, key: str | None) -> dict | None:
        """Best-effort fetch → normalize → index после успешной board write.

        Возвращает нормализованный бриф (нужен вызывающему как источник url
        задачи для обратного линка в PR) или None, если реиндекс не удался."""
        if not key:
            log.warning("board write-through пропущен: ключ задачи не определён")
            return None
        try:
            raw = provider.fetch_one(key)
            if raw is None:
                return None
            brief = provider.normalize(raw)
            self.components.task_service.index_task(brief)
            return brief
        except Exception:
            log.warning("board write-through реиндекс не удался")
            return None
```

В `create_task` (строка ~631) привести к bool — заменить строку

```python
                reindexed = self._write_through(resolved.provider, result.get("key"))
```

на

```python
                reindexed = self._write_through(resolved.provider, result.get("key")) is not None
```

- [ ] **Step 5: Добавить `_backlink_pr` и вызвать его из `finish_task`**

В `reviewer/mcp/service.py` добавить метод сразу после `_write_through`:

```python
    def _backlink_pr(self, pr_url: str, key: str, task_url: str) -> tuple[bool, list[str]]:
        """Дописать ссылку на задачу в начало тела PR. Возвращает (added, warnings).

        Обратная сторона связи: finish пишет PR-ссылку в задачу, здесь — ссылку
        на задачу в PR. Полностью fail-soft: доска к этому моменту уже записана,
        поэтому ни одна ошибка правки PR не отменяет успех finish_task."""
        from reviewer.tasks.pr_backlink import apply_backlink, parse_pr_url
        if not task_url:
            return False, ["ссылка на задачу не добавлена в PR: url задачи не резолвится "
                           "(task_board.url_template не задан)"]
        target = parse_pr_url(pr_url)
        if target is None:
            return False, ["ссылка на задачу не добавлена в PR: "
                           f"{pr_url!r} не распознан как ссылка на PR/MR"]
        vcs = None
        try:
            vcs = (self._vcs_factory(target.owner, target.repo) if self._vcs_factory
                   else self._review_service._create_vcs_provider(
                       target.owner, target.repo,
                       platform=target.platform, base_url=target.base_url))
            body = apply_backlink(vcs.get_pull_request(target.number).body, key, task_url)
            if body is None:
                return False, []       # ссылка уже на месте — идемпотентный no-op
            vcs.update_pull_request_body(target.number, body)
            return True, []
        except Exception as exc:
            log.warning("бэклинк задачи в PR не удался", exc_info=True)
            return False, [f"ссылка на задачу не добавлена в PR: {exc}"]
        finally:
            if vcs is not None and self._vcs_factory is None:
                try:
                    vcs.close()
                except Exception:
                    log.warning("не удалось закрыть VCS после бэклинка", exc_info=True)
```

В `finish_task` заменить блок (строки 573-583) на:

```python
                brief = self._write_through(resolved.provider, key)
                link_added, link_warnings = self._backlink_pr(
                    pr_url, key, (brief or {}).get("url") or "")
                result.setdefault("warnings", []).extend(migration_warnings)
                result["warnings"].extend(link_warnings)
                return self._safe_board_payload(
                    {
                        "status": "ok",
                        "board_type": resolved.board_type,
                        "reindexed": brief is not None,
                        "task_link_added": link_added,
                        **result,
                    },
                    resolved.secrets,
                )
```

- [ ] **Step 6: Прогнать тесты — убедиться, что зелёные**

Run: `.venv/bin/pytest tests/mcp -q && .venv/bin/pytest -q`
Expected: `tests/mcp` — все passed; полный unit-прогон — passed (регрессий в `create_task` и остальных нет)

- [ ] **Step 7: Обновить докстринг тула**

В `reviewer/entrypoints/mcp_server.py` заменить последнее предложение докстринга `finish_task` (строки 139-140)

```python
        target, and provider_options is a non-secret JSON object. Credentials remain
        server-side; failures are returned safely."""
```

на

```python
        target, and provider_options is a non-secret JSON object. It also appends a
        clickable task link to the PR body (task_link_added; fail-soft, reasons land in
        warnings). Credentials remain server-side; failures are returned safely."""
```

- [ ] **Step 8: Линт и коммит**

Run: `.venv/bin/ruff check reviewer tests`
Expected: `All checks passed!`

```bash
git add reviewer/mcp/service.py reviewer/services/review_service.py \
        reviewer/entrypoints/mcp_server.py tests/mcp/test_finish_task.py
git commit -m "feat(tasks): finish_task дописывает ссылку на задачу в тело PR"
```

---

### Task 4: Скилл, манифест и документация

**Files:**
- Modify: `plugin/skills/finish-task/SKILL.md`
- Modify: `tests/skills/test_finish_task_skill.py`
- Modify: `README.md:863`, `README.ru.md:791`, `CLAUDE.md`, `docs/board-providers.md:36`
- Run: `update_codex_plugin_manifest.py`

**Interfaces:**
- Consumes: поле `task_link_added` из Task 3.
- Produces: ничего для последующих тасков (финальный).

- [ ] **Step 1: Написать падающий guard-тест**

Дописать в конец `tests/skills/test_finish_task_skill.py`:

```python
def test_finish_task_mentions_pr_backlink():
    t = SKILL.read_text(encoding="utf-8")
    assert "task_link_added" in t     # отчёт озвучивает результат обратного линка
    assert "PR body" in t             # offer предупреждает о правке тела PR
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_finish_task_skill.py -q`
Expected: FAIL — `assert 'task_link_added' in t`

- [ ] **Step 3: Обновить скилл**

В `plugin/skills/finish-task/SKILL.md` заменить первый абзац после заголовка:

```markdown
Reply in Russian. The server appends the PR link idempotently and completes the selected generic
target; clients never send credentials.
```

на

```markdown
Reply in Russian. The server appends the PR link idempotently, completes the selected generic
target, and adds a clickable task link to the PR body; clients never send credentials.
```

В шаге 4 (`**Offer + confirm.**`) заменить первое предложение

```markdown
4. **Offer + confirm.** Show the PR link and the **resolved done target** by its label plus every
   selected option.
```

на

```markdown
4. **Offer + confirm.** Show the PR link and the **resolved done target** by its label plus every
   selected option, and state that a task link will be prepended to the PR body.
```

В шаге 6 заменить последнее предложение

```markdown
   `already_closed` is true, state that no duplicate PR link was added.
```

на

```markdown
   `already_closed` is true, state that no duplicate PR link was added. Report `task_link_added`:
   when false, relay the warning verbatim — the board write still succeeded.
```

- [ ] **Step 4: Прогнать guard-тесты**

Run: `.venv/bin/pytest tests/skills -q`
Expected: passed (включая новый тест; запрет на упоминание конкретных досок не нарушен)

- [ ] **Step 5: Пересобрать codex-манифест**

Run: `.venv/bin/python update_codex_plugin_manifest.py && .venv/bin/pytest tests/ -q -k codex`
Expected: манифест обновлён, install-тесты passed

- [ ] **Step 6: Обновить документацию**

В `README.md` строка 863 — заменить описание в таблице тулов на:

```markdown
| `finish_task` | `(key, pr_url, note=None, mark_done=True, board_type=None, target=None, provider_options=None)` | Idempotently link a PR, optionally set a generic done target, prepend a clickable task link to the PR body, and write through. |
```

В `README.ru.md` строка 791 — аналогично:

```markdown
| `finish_task` | `(key, pr_url, note=None, mark_done=True, board_type=None, target=None, provider_options=None)` | Идемпотентно дописать PR-link, опционально выставить generic done target, добавить кликабельную ссылку на задачу в тело PR и сделать write-through. |
```

В `docs/board-providers.md` строка 36 — расширить комментарий:

```yaml
  url_template: "https://tasks.example/{code}"  # optional non-secret link metadata; also feeds the PR backlink
```

В `CLAUDE.md`, в пункте «**Закрытие задачи после PR (`finish_task`)**», после предложения
«Провайдер возвращает отдельные `pr_link_added`, `done_set`, `already_closed` и warnings; общий
слой затем делает write-through `fetch_one → normalize → index_task`.» вставить:

```markdown
  Связь двусторонняя: тот же слой fail-soft дописывает кликабельную ссылку на задачу
  (markdown, из `url_template`) в начало тела PR — маркер `<!-- reviewer:task-link -->`
  даёт идемпотентность, платформа резолвится по форме ссылки (`/pull/N` → GitHub,
  `/-/merge_requests/N` → GitLab), результат — в поле `task_link_added`.
```

- [ ] **Step 7: Финальная верификация**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: полный unit-прогон passed; ruff по изменённым файлам чист (repo-wide чистота ruff на dev не гарантирована — сверять только свои файлы)

- [ ] **Step 8: Коммит**

```bash
git add plugin/ tests/skills/test_finish_task_skill.py README.md README.ru.md \
        CLAUDE.md docs/board-providers.md
git commit -m "docs(tasks): описать обратный линк задачи в PR в скилле и доках"
```

---

## Self-Review

**Покрытие спеки:**

| Раздел спеки | Таск |
|---|---|
| Поток данных (`_write_through` → бриф → `_backlink_pr`) | Task 3, шаги 4-5 |
| `pr_backlink.py` (`parse_pr_url`, `apply_backlink`, `PRTarget`) | Task 1 |
| `update_pull_request_body` на обеих платформах | Task 2 |
| Формат строки и 4 правила идемпотентности | Task 1 (тесты + реализация) |
| Резолвинг платформы из URL, self-hosted GitLab, вложенные группы | Task 1 (парсер) + Task 3, шаг 3 (`platform`/`base_url`) |
| Обработка ошибок (5 строк таблицы, включая `brief is None`) | Task 3, шаг 5 + тесты шага 1 |
| `reindexed` остаётся `bool` | Task 3, шаги 4-5 + существующий тест `test_finish_task_writes_through_to_store` |
| Тесты (4 файла) | Task 1, 2, 3, 4 |
| Документация (скилл, манифест, README EN/RU, CLAUDE.md, board-providers) | Task 4 |
| Вне скоупа (переключатель, заголовок PR, GHE, autolinks) | не реализуется нигде — верно |

**Типы и имена сверены:** `PRTarget(platform, owner, repo, number, base_url)` — поля совпадают в Task 1 (определение), Task 3 (`target.owner`, `target.repo`, `target.number`, `target.platform`, `target.base_url`). `apply_backlink(body, key, task_url) -> str | None` и `parse_pr_url(url) -> PRTarget | None` вызываются в Task 3 ровно с этими сигнатурами. `update_pull_request_body(number, body)` — Task 2 определяет, Task 3 и фейк `_FakeVCS` вызывают одинаково. `MARKER` используется в Task 1 и ассертится в Task 3.

**Плейсхолдеров нет:** каждый шаг содержит конкретный код или конкретную команду с ожидаемым результатом.
