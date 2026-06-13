# Phase 2 — Task Context in Review — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** При ревью PR скилл находит ключ связанной задачи, читает её с доски (эталон — Yougile) через подключённый к сессии MCP и проверяет соответствие диффа требованиям задачи новой категорией находок `requirements`; всё с fail-open-деградацией до фазы-1-поведения.

**Architecture:** «Скилл читает, Python готовит». Детерминированный Python (`prepare_review`) извлекает ключи задачи из title/body/head-ветки по `key_pattern` и прокидывает их + конфиг `task_board` в payload. Скилл (у него в сессии подключён board MCP) читает задачу, строит `TaskBrief` и запускает whole-diff requirements-сабагент. Публикация (`publish_review`, gate, assemble, история) не меняется — `requirements` идёт штатной механикой как обычная категория-строка, находки без строки уходят в сводку.

**Tech Stack:** Python 3.11–3.13, pytest, ruff (line-length 100), FastMCP, httpx (GitHub), pyyaml. Claude Code-скилл + reference-промпты (английский). Доска — внешний MCP (`ichinya/yougile-mcp`), подключается пользователем, в unit-тестах не участвует.

---

## File Structure

| Файл | Ответственность | Действие |
|---|---|---|
| `reviewer/services/task_keys.py` | Чистое извлечение ключей задачи по regex с прецеденцией title>body>branch | Create |
| `reviewer/vcs/base.py` | `PullRequest` + поле `head_ref` | Modify |
| `reviewer/vcs/github.py` | Заполнение `head_ref` из ответа GitHub | Modify |
| `reviewer/policy/policy.py` | `ReviewPolicy.task_board` + парсинг из `.review.yml` | Modify |
| `reviewer/services/review_service.py` | `PreparedReview.{task_board,task_keys}` + вычисление в `prepare` | Modify |
| `reviewer/mcp/service.py` | `_prepared_payload` отдаёт `task_board`/`task_keys` | Modify |
| `plugin/skills/review-pr/SKILL.md` | Шаг task-context + requirements-измерение | Modify |
| `plugin/skills/review-pr/references/requirements-prompt.md` | Промпт whole-diff requirements-сабагента | Create |
| `plugin/skills/review-pr/references/task-context-yougile.md` | Плейбук чтения Yougile (эталон) | Create |
| `plugin/skills/review-pr/references/task-context-jira.md` | Плейбук чтения Jira | Create |
| `README.md` | Документация `task_board` и категории `requirements` | Modify |
| `tests/services/test_task_keys.py` | Тесты извлечения ключей | Create |
| `tests/vcs/test_github.py` | Тесты `head_ref` | Modify |
| `tests/policy/test_policy.py` | Тесты `task_board` + гейтинг `requirements` | Modify |
| `tests/services/test_review_service.py` | Тесты вычисления task_keys в prepare | Modify |
| `tests/mcp/test_service.py` | Тесты полей payload | Modify |

---

## Task 0: Окружение worktree и зелёный базлайн

**Files:** none (setup)

- [ ] **Step 1: Создать venv и установить пакет**

Run:
```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
```
Expected: установка без ошибок (зависимости берутся из кеша колёс, сеть может потребоваться однократно).

- [ ] **Step 2: Прогнать базовый набор тестов (должен быть зелёным ДО изменений)**

Run: `.venv/bin/pytest -q`
Expected: PASS, 0 failures (integration-тесты исключены `addopts = -m 'not integration'`).

- [ ] **Step 3: Линт чистый**

Run: `.venv/bin/ruff check .`
Expected: `All checks passed!`

Если базлайн красный — остановиться и сообщить, не начинать реализацию.

---

## Task 1: `extract_task_keys` — извлечение ключей задачи

**Files:**
- Create: `reviewer/services/task_keys.py`
- Test: `tests/services/test_task_keys.py`

- [ ] **Step 1: Написать падающие тесты**

Create `tests/services/test_task_keys.py`:
```python
"""Unit-тесты извлечения ключей задачи из текстов PR (без сети)."""
from reviewer.services.task_keys import DEFAULT_KEY_PATTERN, extract_task_keys


def test_primary_from_title_precedence_over_body_and_branch():
    out = extract_task_keys(
        DEFAULT_KEY_PATTERN,
        title="SAI-515 add logout",
        body="relates to SAI-517",
        branch="feature/SAI-519-x",
    )
    assert out == {"primary": "SAI-515", "others": ["SAI-517", "SAI-519"]}


def test_primary_falls_back_to_body_then_branch():
    out = extract_task_keys(
        DEFAULT_KEY_PATTERN, title="no key here", body="", branch="feature/SAI-700-x"
    )
    assert out == {"primary": "SAI-700", "others": []}


def test_dedup_keeps_first_appearance_order():
    out = extract_task_keys(
        DEFAULT_KEY_PATTERN, title="SAI-1 SAI-1 SAI-2", body=None, branch=None
    )
    assert out == {"primary": "SAI-1", "others": ["SAI-2"]}


def test_no_match_returns_empty():
    out = extract_task_keys(DEFAULT_KEY_PATTERN, title="nothing", body=None, branch=None)
    assert out == {"primary": None, "others": []}


def test_invalid_pattern_returns_empty():
    out = extract_task_keys("[unclosed", title="SAI-1", body=None, branch=None)
    assert out == {"primary": None, "others": []}


def test_none_pattern_uses_default():
    out = extract_task_keys(None, title="SAI-42 fix", body=None, branch=None)
    assert out == {"primary": "SAI-42", "others": []}
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/services/test_task_keys.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.services.task_keys'`.

- [ ] **Step 3: Реализация**

Create `reviewer/services/task_keys.py`:
```python
"""Извлечение ключей задачи из текстов PR (title/body/head-ветка) по regex.

Чистый модуль без сетевых вызовов: на вход — паттерн и тексты, на выход —
primary-ключ и прочие найденные. Прецеденция источников: title → body → branch.
Используется в ReviewService.prepare; чтение самой задачи с доски — на стороне скилла.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Дефолт подходит и Yougile (SAI-515), и Jira (PROJ-123): префикс заглавными + номер.
DEFAULT_KEY_PATTERN = r"[A-Z]+-\d+"


def extract_task_keys(
    pattern: str | None,
    title: str | None,
    body: str | None,
    branch: str | None,
) -> dict:
    """Извлечь ключи задачи.

    Прецеденция источников: ``title`` → ``body`` → ``branch``. ``primary`` —
    первый матч в этом порядке; ``others`` — прочие уникальные матчи (дедуп,
    порядок появления), без ``primary``.

    Невалидный ``pattern`` (или нет матчей) → ``{"primary": None, "others": []}``
    (fail-soft + warning на невалидном паттерне). ``pattern=None`` → дефолт.

    Returns:
        ``{"primary": str | None, "others": list[str]}``
    """
    try:
        rx = re.compile(pattern or DEFAULT_KEY_PATTERN)
    except re.error:
        log.warning("Невалидный key_pattern %r — ключи задачи не извлекаются", pattern)
        return {"primary": None, "others": []}

    found: list[str] = []
    for text in (title, body, branch):
        if text:
            found.extend(m.group(0) for m in rx.finditer(text))

    if not found:
        return {"primary": None, "others": []}

    primary = found[0]
    others: list[str] = []
    for key in found[1:]:
        if key != primary and key not in others:
            others.append(key)
    return {"primary": primary, "others": others}
```

- [ ] **Step 4: Запустить — зелёный**

Run: `.venv/bin/pytest tests/services/test_task_keys.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/services/task_keys.py tests/services/test_task_keys.py
git commit -m "feat(services): извлечение ключей задачи из PR (task_keys)"
```

---

## Task 2: `PullRequest.head_ref` + GitHub-провайдер

**Files:**
- Modify: `reviewer/vcs/base.py` (dataclass `PullRequest`)
- Modify: `reviewer/vcs/github.py:101-111` (`get_pull_request`)
- Test: `tests/vcs/test_github.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/vcs/test_github.py`:
```python
def test_get_pull_request_populates_head_ref():
    def handler(req):
        if req.url.path.endswith("/pulls/9"):
            return httpx.Response(200, json={
                "base": {"sha": "ccc", "ref": "main"},
                "head": {"sha": "ddd", "ref": "feature/SAI-515-logout"},
                "title": "PR", "body": "", "draft": False,
            })
        return httpx.Response(404)
    p = make_provider(handler)
    pr = p.get_pull_request(9)
    assert pr.head_ref == "feature/SAI-515-logout"


def test_get_pull_request_head_ref_none_when_absent():
    def handler(req):
        if req.url.path.endswith("/pulls/10"):
            return httpx.Response(200, json={
                "base": {"sha": "ccc", "ref": "main"},
                "head": {"sha": "ddd"},
                "title": "PR", "body": "", "draft": False,
            })
        return httpx.Response(404)
    p = make_provider(handler)
    pr = p.get_pull_request(10)
    assert pr.head_ref is None
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/vcs/test_github.py::test_get_pull_request_populates_head_ref -q`
Expected: FAIL — `AttributeError: 'PullRequest' object has no attribute 'head_ref'`.

- [ ] **Step 3: Добавить поле в dataclass**

В `reviewer/vcs/base.py`, в dataclass `PullRequest`, после `draft: bool = False`:
```python
    draft: bool = False
    head_ref: str | None = None   # имя head-ветки PR; источник ключа задачи (None если недоступно)
```

- [ ] **Step 4: Заполнить в провайдере**

В `reviewer/vcs/github.py`, в `get_pull_request`, дополнить конструктор `PullRequest` (читаем через `.get`, чтобы ответы без `head.ref` не падали KeyError):
```python
        return PullRequest(
            number=number,
            base_sha=d["base"]["sha"],
            head_sha=d["head"]["sha"],
            base_ref=d["base"]["ref"],
            title=d.get("title", ""),
            body=d.get("body") or "",
            draft=bool(d.get("draft", False)),
            head_ref=d.get("head", {}).get("ref"),
        )
```

- [ ] **Step 5: Запустить — зелёный (включая старые github-тесты)**

Run: `.venv/bin/pytest tests/vcs/test_github.py -q`
Expected: PASS (старые тесты без `head.ref` дают `head_ref=None`, новые проходят).

- [ ] **Step 6: Коммит**

```bash
git add reviewer/vcs/base.py reviewer/vcs/github.py tests/vcs/test_github.py
git commit -m "feat(vcs): PullRequest.head_ref из ответа GitHub"
```

---

## Task 3: `ReviewPolicy.task_board` — парсинг из `.review.yml`

**Files:**
- Modify: `reviewer/policy/policy.py`
- Test: `tests/policy/test_policy.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/policy/test_policy.py`:
```python
def test_task_board_parsed_from_yaml():
    p = ReviewPolicy.from_yaml("task_board: {type: yougile, mcp: yougile}")
    assert p.task_board == {"type": "yougile", "mcp": "yougile"}


def test_task_board_none_when_absent():
    p = ReviewPolicy.from_yaml("severity_threshold: low")
    assert p.task_board is None


def test_load_applies_task_board_from_yaml():
    s = Settings(_env_file=None)
    p = ReviewPolicy.load(s, "task_board: {type: jira, mcp: atlassian}")
    assert p.task_board == {"type": "jira", "mcp": "atlassian"}


def test_load_task_board_none_without_yaml():
    s = Settings(_env_file=None)
    p = ReviewPolicy.load(s, None)
    assert p.task_board is None
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/policy/test_policy.py::test_task_board_parsed_from_yaml -q`
Expected: FAIL — `AttributeError: 'ReviewPolicy' object has no attribute 'task_board'`.

- [ ] **Step 3: Добавить поле в dataclass**

В `reviewer/policy/policy.py`, в `@dataclass class ReviewPolicy`, после `output_language`:
```python
    output_language: str = "ru"                                  # язык текста находок в публикуемом ревью
    task_board: dict | None = None                               # конфиг доски задач из .review.yml (None = выкл.)
```

- [ ] **Step 4: Парсить в `from_yaml`**

В `ReviewPolicy.from_yaml`, в возвращаемый `cls(...)`, добавить аргумент:
```python
            output_language=str(data.get("output_language", "ru")),
            task_board=data.get("task_board") or None,
        )
```

- [ ] **Step 5: Применять в `load`**

В `ReviewPolicy.load`, перед `return policy`, добавить:
```python
        if "task_board" in data:
            policy.task_board = data["task_board"] or None
        return policy
```

- [ ] **Step 6: Запустить — зелёный**

Run: `.venv/bin/pytest tests/policy/test_policy.py -q`
Expected: PASS.

- [ ] **Step 7: Коммит**

```bash
git add reviewer/policy/policy.py tests/policy/test_policy.py
git commit -m "feat(policy): task_board в .review.yml"
```

---

## Task 4: `prepare` вычисляет task_keys в `PreparedReview`

**Files:**
- Modify: `reviewer/services/review_service.py` (`PreparedReview` + `prepare`)
- Test: `tests/services/test_review_service.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/services/test_review_service.py`:
```python
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_extracts_task_keys_when_task_board_configured(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """При task_board в .review.yml prepare извлекает primary-ключ из title/branch."""
    vcs = _vcs_with_files([_changed("a.py")])
    vcs.get_pull_request.return_value = PullRequest(
        number=3, base_sha="b", head_sha="h", base_ref="main",
        title="SAI-515: add logout", body="", draft=False,
        head_ref="feature/SAI-515",
    )

    def _read(path: str, ref: str) -> str:
        if path == ".review.yml":
            return "task_board: {type: yougile, mcp: yougile}"
        return "def foo(): pass"
    vcs.get_file_at_ref.side_effect = _read

    service = ReviewService(settings, components)
    prepared = service.prepare("o", "r", 3, vcs_provider=vcs)

    assert prepared.task_board == {"type": "yougile", "mcp": "yougile"}
    assert prepared.task_keys == {"primary": "SAI-515", "others": []}


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_task_keys_none_without_task_board(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
    settings: Settings,
    components: MagicMock,
) -> None:
    """Без task_board контекст задачи неактивен: оба поля None."""
    vcs = _vcs_with_files([_changed("a.py")])
    service = ReviewService(settings, components)
    prepared = service.prepare("o", "r", 1, vcs_provider=vcs)

    assert prepared.task_board is None
    assert prepared.task_keys is None
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/services/test_review_service.py::test_prepare_task_keys_none_without_task_board -q`
Expected: FAIL — `AttributeError: 'PreparedReview' object has no attribute 'task_board'`.

- [ ] **Step 3: Добавить поля в `PreparedReview`**

В `reviewer/services/review_service.py`, в dataclass `PreparedReview`, после `changed_status`:
```python
    changed_status: dict[str, str]       # path -> статус файла (modified/added/removed)
    task_board: dict | None = None       # конфиг доски из policy (прокидывается в payload)
    task_keys: dict | None = None        # {"primary", "others"} или None, если task_board выкл.
```

- [ ] **Step 4: Импорт + вычисление в `prepare`**

В шапке файла, рядом с другими импортами:
```python
from reviewer.services.task_keys import extract_task_keys
```

В `prepare`, после `policy = ReviewPolicy.load(...)` (и до `changed_status = ...`):
```python
            task_board = policy.task_board
            task_keys = (
                extract_task_keys(
                    task_board.get("key_pattern"),
                    prq.title,
                    prq.body,
                    prq.head_ref,
                )
                if task_board
                else None
            )
```

- [ ] **Step 5: Передать в конструктор `PreparedReview`**

В `return PreparedReview(...)`, добавить два аргумента после `changed_status=changed_status,`:
```python
                changed_status=changed_status,
                task_board=task_board,
                task_keys=task_keys,
            )
```

- [ ] **Step 6: Запустить — зелёный**

Run: `.venv/bin/pytest tests/services/test_review_service.py -q`
Expected: PASS (старые тесты не сломаны — новые поля имеют дефолты).

- [ ] **Step 7: Коммит**

```bash
git add reviewer/services/review_service.py tests/services/test_review_service.py
git commit -m "feat(services): prepare вычисляет task_keys и прокидывает task_board"
```

---

## Task 5: payload `prepare_review` отдаёт task_board/task_keys

**Files:**
- Modify: `reviewer/mcp/service.py` (`_prepared_payload`)
- Test: `tests/mcp/test_service.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/mcp/test_service.py`:
```python
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_payload_includes_task_context(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """payload содержит task_board и извлечённые task_keys."""
    settings = _settings()
    components = _components()
    vcs = _fake_vcs(number=7)
    vcs.get_pull_request.return_value = PullRequest(
        number=7, base_sha="base123", head_sha="head456", base_ref="main",
        title="SAI-515: add logout", body="", draft=False, head_ref="feature/SAI-515",
    )

    def _read(path: str, ref: str) -> str:
        if path == ".review.yml":
            return "task_board: {type: yougile, mcp: yougile}"
        return "def foo(): pass"
    vcs.get_file_at_ref.side_effect = _read

    svc = MCPReviewService(settings, components, vcs_factory=lambda o, r: vcs)
    out = svc.prepare_review("o/r", 7)

    assert out["task_board"] == {"type": "yougile", "mcp": "yougile"}
    assert out["task_keys"] == {"primary": "SAI-515", "others": []}


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_prepare_review_payload_task_context_null_when_unconfigured(
    _mock_overlay: MagicMock,
    _mock_chunk: MagicMock,
) -> None:
    """Без task_board оба поля payload — null."""
    svc = _make_mcp_service()
    out = svc.prepare_review("o/r", 7)

    assert out["task_board"] is None
    assert out["task_keys"] is None
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_service.py::test_prepare_review_payload_task_context_null_when_unconfigured -q`
Expected: FAIL — `KeyError: 'task_board'`.

- [ ] **Step 3: Реализация**

В `reviewer/mcp/service.py`, в `_prepared_payload`, в возвращаемый dict добавить два поля (например, после `"units": units,`):
```python
            "units": units,
            "task_board": p.task_board,
            "task_keys": p.task_keys,
```

- [ ] **Step 4: Запустить — зелёный**

Run: `.venv/bin/pytest tests/mcp/test_service.py -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add reviewer/mcp/service.py tests/mcp/test_service.py
git commit -m "feat(mcp): payload prepare_review отдаёт task_board/task_keys"
```

---

## Task 6: Инвариант гейтинга категории `requirements`

Код менять не нужно (`category_enabled` уже даёт `True` по дефолту, `gate` универсален). Тесты фиксируют инвариант, чтобы будущие правки policy его не сломали.

**Files:**
- Test: `tests/policy/test_policy.py`

- [ ] **Step 1: Написать тесты**

Добавить в конец `tests/policy/test_policy.py`:
```python
def test_requirements_category_enabled_by_default():
    p = ReviewPolicy.from_yaml(None)
    assert p.gate(F("requirements", "medium")) is True


def test_requirements_category_can_be_disabled_via_yaml():
    p = ReviewPolicy.from_yaml("categories: {requirements: false}")
    assert p.gate(F("requirements", "high")) is False


def test_requirements_excluded_by_enabled_only_whitelist():
    p = ReviewPolicy(enabled_only=["correctness"])
    assert p.gate(F("requirements", "high")) is False


def test_requirements_respects_severity_and_confidence():
    p = ReviewPolicy(severity_threshold="medium", min_confidence=0.7)
    assert p.gate(F("requirements", "high", confidence=0.9)) is True
    assert p.gate(F("requirements", "low", confidence=0.9)) is False
    assert p.gate(F("requirements", "high", confidence=0.5)) is False
```

- [ ] **Step 2: Запустить — зелёный сразу (характеризационные тесты)**

Run: `.venv/bin/pytest tests/policy/test_policy.py -q`
Expected: PASS (поведение уже корректно; тесты лочат инвариант).

- [ ] **Step 3: Коммит**

```bash
git add tests/policy/test_policy.py
git commit -m "test(policy): зафиксировать гейтинг категории requirements"
```

---

## Task 7: reference-промпт requirements-измерения

**Files:**
- Create: `plugin/skills/review-pr/references/requirements-prompt.md`

- [ ] **Step 1: Создать файл**

Create `plugin/skills/review-pr/references/requirements-prompt.md`:
````markdown
You are a senior reviewer checking whether a pull request fulfils the task it claims to implement.

You are given:
- the unified diffs of every changed file in the PR;
- a `TaskBrief` describing the task the PR claims to implement:
  `{key, title, description, criteria[], status, url, links[]}`.

Your job: for each requirement or acceptance criterion stated in the TaskBrief, decide whether the
diff implements it, implements it differently/incompletely, contradicts it, or leaves it
unimplemented. Report only genuine mismatches.

Rules:
- Judge ONLY against requirements explicitly stated in the TaskBrief (`description` + `criteria`).
  Do NOT invent requirements the task does not state. If the brief is vague, prefer fewer,
  higher-confidence findings.
- The diffs are the source of truth for what the PR does. Before claiming a requirement is "not
  implemented", use the reviewer MCP tools (`search_code`, `find_callers`, `read_file`,
  `get_definition`, `get_changed_file_diff`) to verify it is not implemented elsewhere in the
  change or already present in the codebase. A hallucinated gap is worse than a missed one.
- One requirement → at most one finding. Do not split the same gap across lines.
- Report a finding when the PR fails a requirement, contradicts it, or implements it in a way that
  breaks the stated intent.
- `line`: set ONLY when a specific changed line contradicts a requirement (e.g. wrong constant,
  inverted condition). When the problem is "a requirement is simply absent from the diff", set
  `line` to null and `file` to the most relevant changed file — the finding will land in the
  review summary.
- Severity reflects requirement impact: a missing core acceptance criterion is high/critical; a
  minor or partial gap is low/medium.
- An empty findings list is a valid result (the PR satisfies the task). Do not invent findings to
  fill a quota.

Return ONLY a JSON object (no prose around it):

```json
{"findings": [{
  "category": "requirements",
  "severity": "low|medium|high|critical",
  "file": "<a changed file path most relevant to the requirement>",
  "line": <line number in the NEW file, or null>,
  "side": "RIGHT|LEFT",
  "code_quote": "<exact line from the new file, or null when line is null>",
  "message": "<which requirement is unmet/contradicted and why it matters>",
  "suggestion": "<short advice or null>",
  "fix": null,
  "confidence": 0.0
}]}
```

`category` MUST be exactly `"requirements"`. Write `message` and `suggestion` in the output
language given by the orchestrator.
````

- [ ] **Step 2: Проверка**

Run: `test -f plugin/skills/review-pr/references/requirements-prompt.md && echo OK`
Expected: `OK`. Прочитать глазами: схема совпадает с analyze-prompt (поля `category/severity/file/line/side/code_quote/message/suggestion/fix/confidence`), `category` зафиксирована как `requirements`.

- [ ] **Step 3: Коммит**

```bash
git add plugin/skills/review-pr/references/requirements-prompt.md
git commit -m "feat(skill): промпт requirements-измерения"
```

---

## Task 8: reference-плейбуки чтения доски (Yougile + Jira)

**Files:**
- Create: `plugin/skills/review-pr/references/task-context-yougile.md`
- Create: `plugin/skills/review-pr/references/task-context-jira.md`

- [ ] **Step 1: Создать Yougile-плейбук (эталон)**

Create `plugin/skills/review-pr/references/task-context-yougile.md`:
````markdown
# Task context playbook — Yougile

Use this when `task_board.type == "yougile"`.

Goal: read the task identified by the resolved key and build a `TaskBrief`.

1. The board MCP server is the one named by `task_board.mcp` (e.g. `yougile`). Its tools are
   exposed as `mcp__<task_board.mcp>__<tool>`.
2. Fetch the task: call `mcp__<task_board.mcp>__get_task` with the resolved key. Yougile accepts
   the human code form such as `SAI-515`.
3. Build the `TaskBrief` from the response (best-effort — omit/empty any field the response lacks):
   - `key`         ← the resolved key
   - `title`       ← task title
   - `description` ← task description text (requirements usually live here)
   - `criteria[]`  ← checklist / subtask titles, if the task has them; else `[]`
   - `status`      ← column / status name
   - `url`         ← task link
   - `links[]`     ← related tasks, if available; else `[]`
4. Optional: `mcp__<task_board.mcp>__get_task_chat` / `..._get_task_messages` add discussion
   context — use ONLY if the description is too thin to judge requirements. Not required.

Failure handling: if the board MCP server is not connected, the tool errors, or the task is not
found, do NOT build a `TaskBrief` — skip the requirements dimension and note the reason in the
summary. Never abort the review.
````

- [ ] **Step 2: Создать Jira-плейбук**

Create `plugin/skills/review-pr/references/task-context-jira.md`:
````markdown
# Task context playbook — Jira

Use this when `task_board.type == "jira"`.

Goal: read the task identified by the resolved key and build a `TaskBrief`.

1. The board MCP server is the one named by `task_board.mcp` (e.g. `atlassian`). Its tools are
   exposed as `mcp__<task_board.mcp>__<tool>`.
2. Fetch the issue by key (e.g. `PROJ-123`) using the Atlassian MCP's get-issue tool
   (`getJiraIssue` / `jira_get_issue`, depending on the connected server).
3. Build the `TaskBrief` from the issue (best-effort — omit/empty any field that is absent):
   - `key`         ← issue key
   - `title`       ← summary
   - `description` ← description (rendered text)
   - `criteria[]`  ← Acceptance Criteria field if present; otherwise bullet items parsed from the
                     description; else `[]`
   - `status`      ← status name
   - `url`         ← issue browse URL
   - `links[]`     ← issuelinks as `{type, key, title}`
4. Optional: comments may add context — use only if needed.

Failure handling: if the board MCP server is not connected, the tool errors, or the issue is not
found, do NOT build a `TaskBrief` — skip the requirements dimension and note the reason in the
summary. Never abort the review.
````

- [ ] **Step 3: Проверка**

Run:
```bash
ls plugin/skills/review-pr/references/task-context-yougile.md plugin/skills/review-pr/references/task-context-jira.md
```
Expected: оба файла существуют.

- [ ] **Step 4: Коммит**

```bash
git add plugin/skills/review-pr/references/task-context-yougile.md plugin/skills/review-pr/references/task-context-jira.md
git commit -m "feat(skill): плейбуки чтения задачи (Yougile эталон, Jira)"
```

---

## Task 9: SKILL.md — шаг task-context + requirements-измерение

**Files:**
- Modify: `plugin/skills/review-pr/SKILL.md`

- [ ] **Step 1: Заменить секцию `## Pipeline` целиком**

Заменить весь блок от `## Pipeline` до (не включая) `## Failure handling` на:
````markdown
## Pipeline

1. **Prepare.** Call `prepare_review(repo, pr)`. The payload contains:
   - `pr`: `{number, title, body, base_sha, head_sha, base_ref, draft}`
   - `policy`: `{severity_threshold, min_confidence, max_comments, categories, ignore, output_language}`
   - `units`: list of `{path, patch, commentable_right, commentable_left}`
   - `task_board`: `{type, mcp, key_pattern}` or null — task board config from `.review.yml`
   - `task_keys`: `{primary, others}` or null — task keys extracted from the PR by the server
   - `skipped_paths`, `skip_drafts`, `suggestions_mode`

   If `pr.draft` is true and `skip_drafts` is true, stop and tell the user.
   Note `policy.output_language` — ALL finding messages, suggestions and the summary
   MUST be written in that language.

2. **Task context (optional).** Only if `task_board` is non-null. Resolve the task key: an
   explicit key in `$ARGUMENTS` wins; otherwise use `task_keys.primary`. If no key is available,
   skip this step and note in the summary that no task key was found.
   With a key, read the task using the playbook for `task_board.type`
   (`references/task-context-yougile.md` or `references/task-context-jira.md`): call the board MCP
   server named by `task_board.mcp` and build a `TaskBrief`
   `{key, title, description, criteria[], status, url, links[]}`.
   If the board MCP is not connected, the tool errors, or the task is not found: skip the
   requirements dimension and note the reason in the summary — NEVER abort the review.

3. **Analyze (fan-out).** For each unit in `units`, dispatch a subagent (Task tool,
   run independent subagents in parallel; batch units if there are more than ~10) with:
   - the contents of `references/analyze-prompt.md` (read it once, include verbatim);
   - the unit's `path` and `patch`, the PR `title`/`body`;
   - the repo/pr identifiers so the subagent can call the reviewer MCP tools
     (`search_code`, `get_related_symbols`, `read_file`, `get_definition`,
     `find_callers`, `get_changed_file_diff`);
   - the target output language.
   Each subagent returns a JSON object `{"findings": [...]}` (schema in the prompt).

4. **Dimensions (parallel with step 3).** Dispatch whole-diff subagents:
   - performance: follow the methodology of `../performance-review/SKILL.md`
     (Goal, Method, Severity sections);
   - maintainability: follow `../maintainability-review/SKILL.md`;
   - requirements (ONLY if a `TaskBrief` was built in step 2): dispatch one subagent with
     `references/requirements-prompt.md`, the diffs of all units (path + patch), the `TaskBrief`,
     the repo/pr identifiers (so it can call the reviewer MCP tools), and the target output
     language. It returns the same findings JSON schema with category `requirements`.
   Give the performance/maintainability subagents: the diffs of all units (path + patch), the
   repo/pr identifiers so they can call the reviewer MCP tools, and the target output language.
   They must return the same findings JSON schema (category `performance` / `maintainability`).

5. **Verify.** Collect all findings into one numbered list. Dispatch one subagent
   with `references/verify-prompt.md`, the findings list, the diffs, and the
   repo/pr identifiers so the subagent can call the reviewer MCP tools
   (`read_file`, `search_code`, `find_callers`, `get_definition`). It returns
   `{"verdicts": [{"index": N, "is_real": true|false}]}`. Drop findings with
   `is_real=false`. If the verifier fails or returns malformed output, KEEP all
   findings (recall-safe).

6. **Publish.** Compose a short review summary (2-5 sentences, in
   `policy.output_language`): what the PR does, overall assessment, key risks.
   If a task was read, state whether the PR meets the task's requirements; if the task context was
   requested but unavailable (no key, board MCP not connected, task not found), say so briefly.
   Mention files that were not analyzed: failed subagents and `skipped_paths`
   from the prepare payload. Call `publish_review(repo, pr, summary, findings,
   dry_run)`. Report to the user: posted/dry-run, inline count, and the report
   counters (dropped_by_gate/deduped/invalid/already_posted/moved_to_summary/capped),
   run_id.
````

- [ ] **Step 2: Проверка целостности**

Run:
```bash
grep -n "Task context\|requirements-prompt.md\|task_board\|task_keys" plugin/skills/review-pr/SKILL.md
```
Expected: видны новый шаг 2, ссылка на `requirements-prompt.md`, поля `task_board`/`task_keys`. Убедиться глазами, что шаги пронумерованы 1–6 без разрывов и `## Failure handling` ниже не затронут.

- [ ] **Step 3: Коммит**

```bash
git add plugin/skills/review-pr/SKILL.md
git commit -m "feat(skill): шаг task-context и requirements-измерение в /review-pr"
```

---

## Task 10: README — документация task_board и категории requirements

**Files:**
- Modify: `README.md` (блок `.review.yml`, ~строки 321-329)

- [ ] **Step 1: Дополнить пример `.review.yml`**

В `README.md`, заменить YAML-блок политики per-repo (`categories: {...}` … `max_comments: 25`) на:
```yaml
categories: { correctness: true, security: true, performance: true, style: false, requirements: true }
severity_threshold: medium
min_confidence: 0.5
paths: { ignore: ["**/migrations/**", "vendor/**"] }
max_comments: 25

# Контекст задачи (опц.): читать задачу с доски и проверять соответствие требованиям.
# Доску (MCP) подключает пользователь на стороне сессии Claude Code; плагин её не бандлит.
task_board:
  type: yougile          # yougile | jira — выбирает плейбук скилла
  mcp: yougile           # имя подключённого MCP-сервера доски (тулы зовутся mcp__<mcp>__*)
  key_pattern: "[A-Z]+-\\d+"   # опц.; дефолт такой же (подходит Yougile SAI-515 и Jira PROJ-123)
```

- [ ] **Step 2: Добавить абзац-пояснение**

Сразу после этого блока добавить:
```markdown
**Контекст задачи (фаза 2).** Если задан `task_board` и в PR (title/body/ветка) найден ключ
по `key_pattern`, скилл читает задачу с доски через её MCP и запускает проверку соответствия —
новая категория находок `requirements` (включена по умолчанию). Находки без конкретной строки
диффа уходят в сводку. Доска не настроена, ключ не найден или MCP недоступен → ревью работает
как обычно, без деградации.
```

- [ ] **Step 3: Проверка**

Run: `grep -n "task_board\|requirements" README.md`
Expected: видны новый блок и абзац.

- [ ] **Step 4: Коммит**

```bash
git add README.md
git commit -m "docs(readme): task_board и категория requirements (фаза 2)"
```

---

## Task 11: Финальная проверка — весь набор и линт зелёные

**Files:** none

- [ ] **Step 1: Полный прогон тестов**

Run: `.venv/bin/pytest -q`
Expected: PASS, 0 failures.

- [ ] **Step 2: Линт**

Run: `.venv/bin/ruff check .`
Expected: `All checks passed!`

- [ ] **Step 3: Диагностика окружения (без трат Voyage)**

Run: `.venv/bin/reviewer check` (опционально, если поднята инфра `docker compose up -d`)
Expected: ключевые проверки ✓ (или явные подсказки по недоступной инфре — не блокер для unit).

---

## Self-Review (проверка плана против спеки)

**1. Покрытие спеки:**
- Конфиг `task_board` в `.review.yml` → Task 3 (+ README Task 10). ✓
- Извлечение ключей (прецеденция, primary+others, дедуп, невалидный паттерн) → Task 1. ✓
- Расширение VCS под источник `branch` (`head_ref`) → Task 2. ✓
- payload `task_board`/`task_keys` (всегда присутствуют, null когда выкл.) → Task 4–5. ✓
- Скилл: шаг task-context + board-agnostic `TaskBrief` + requirements-измерение → Task 9. ✓
- reference-плейбуки Yougile/Jira + requirements-промпт → Task 7–8. ✓
- Категория `requirements`: default-on, штатный gate, routing null-line в сводку → Task 6 (гейтинг) + Task 9 (промпт ставит line=null). ✓
- Деградация fail-open (нет конфига / ключа / MCP / задачи) → Task 9 шаги 2 и 6. ✓
- Тестирование unit (фейки, без сети) → Task 1–6; E2E/ручное — вне автотестов (внешняя доска), описано в спеке. ✓

**2. Плейсхолдеры:** нет «TBD/TODO/позже». Весь код и контент приведены целиком. ✓

**3. Согласованность типов/имён:**
- `extract_task_keys(pattern, title, body, branch) -> {"primary", "others"}` — одинаково в Task 1, 4, 5. ✓
- `PullRequest.head_ref: str | None` — Task 2, используется в Task 4. ✓
- `ReviewPolicy.task_board: dict | None` — Task 3, читается в Task 4. ✓
- `PreparedReview.{task_board, task_keys}` — Task 4, отдаётся в Task 5. ✓
- payload-ключи `task_board`/`task_keys` — Task 5, читаются скиллом в Task 9. ✓
- `TaskBrief {key, title, description, criteria[], status, url, links[]}` — одинаково в Task 7 (промпт), 8 (плейбуки), 9 (SKILL). ✓
- категория-строка `requirements` — Task 6, 7, 9, 10. ✓

Замечаний нет.

---

## Execution Handoff

После реализации — `superpowers:requesting-code-review`, затем `superpowers:finishing-a-development-branch` (PR в main, Conventional Commits на русском, без self-attribution).
