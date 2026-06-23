# PRI-119 — PR walkthrough (гид по чтению) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать ревьюеру-человеку markdown-гид по PR (откуда начать читать, что меняет файл, на что влияет) — отдельно от находок-багов.

**Architecture:** Новый LLM-скилл `/reviewer_pr-walkthrough` поверх существующей PR-сессии: `prepare_review` → `get_impact`/`get_changed_file_diff`/`find_callers` → markdown в терминал. Опциональный приор — `get_subsystem_summaries` (PRI-159). Опциональный постинг в PR — тонкий метод `post_pr_walkthrough` (переиспользует `GitHubProvider.publish_review` с пустыми inline-комментариями).

**Tech Stack:** Python 3.11–3.13, FastMCP, httpx (GitHubProvider), pytest. Реализуется **после** PRI-159. Спек: `docs/superpowers/specs/2026-06-23-pri-119-pr-walkthrough-design.md`.

## Global Constraints

- Язык проекта — **русский**: докстринги, комментарии, гид и вывод скилла. Тело SKILL.md — на английском (токены), но скилл инструктирует отвечать по-русски.
- Коммиты — **Conventional Commits на русском, без self-attribution**.
- Ветка работы: `feat/graphrag-summaries-walkthrough` (та же программа, что PRI-159).
- PR-сессионные тулы уже существуют: `prepare_review`, `get_impact`, `get_changed_file_diff`, `find_callers`, `get_related_symbols`, `read_file`.
- `GitHubProvider.publish_review(number, head_sha, summary, comments)` — постинг review (см. `reviewer/vcs/github.py:163`).
- Постинг в PR — outward-facing: только по явной просьбе пользователя + подтверждение.
- Unit-тесты не трогают сеть (фейки). Линт: `.venv/bin/ruff check .`.

---

### Task 1: Метод постинга `post_pr_walkthrough` + MCP-тул

**Files:**
- Modify: `reviewer/mcp/service.py` (метод + константа-маркер)
- Modify: `reviewer/entrypoints/mcp_server.py` (`@mcp.tool()`-обёртка)
- Test: `tests/mcp/test_pr_walkthrough.py`

**Interfaces:**
- Consumes: PR-сессия (`self._session(repo, pr).prepared.vcs` + `.prepared.prq.head_sha`), `GitHubProvider.publish_review`.
- Produces:
  - `WALKTHROUGH_MARKER = "<!-- ai-walkthrough -->"` (module-level в `service.py`).
  - `MCPReviewService.post_pr_walkthrough(repo, pr, markdown) -> dict` → `{"posted": True, "pr": pr}` или `{"posted": False, "reason": ...}`.

- [ ] **Step 1: Написать падающий unit-тест**

```python
# tests/mcp/test_pr_walkthrough.py
"""Unit-тест постинга walkthrough-гида (PRI-119). Сессия и VCS — фейки."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService


def _settings() -> Settings:
    s = Settings()
    s.voyage_api_key = "test"
    s.github_token = "test"
    s.default_repo = ""
    return s


def test_post_pr_walkthrough_posts_body_with_marker_and_no_comments():
    svc = MCPReviewService(_settings(), MagicMock())
    vcs = MagicMock()
    prepared = SimpleNamespace(vcs=vcs, prq=SimpleNamespace(head_sha="head456"))
    svc._session = lambda repo, pr: SimpleNamespace(prepared=prepared)   # изолируем сессию

    out = svc.post_pr_walkthrough("o/n", 7, "## Начни отсюда\n- a.py")

    assert out == {"posted": True, "pr": 7}
    vcs.publish_review.assert_called_once()
    number, head_sha, body, comments = vcs.publish_review.call_args.args
    assert number == 7 and head_sha == "head456"
    assert body.startswith("<!-- ai-walkthrough -->")
    assert "Начни отсюда" in body
    assert comments == []      # гид — без inline-находок


def test_post_pr_walkthrough_fail_soft_on_network_error():
    svc = MCPReviewService(_settings(), MagicMock())
    vcs = MagicMock()
    vcs.publish_review.side_effect = RuntimeError("boom")
    prepared = SimpleNamespace(vcs=vcs, prq=SimpleNamespace(head_sha="h"))
    svc._session = lambda repo, pr: SimpleNamespace(prepared=prepared)

    out = svc.post_pr_walkthrough("o/n", 7, "guide")
    assert out["posted"] is False
    assert "RuntimeError" in out["reason"]
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/pytest tests/mcp/test_pr_walkthrough.py -q`
Expected: FAIL (`AttributeError: ... 'post_pr_walkthrough'`).

- [ ] **Step 3: Реализовать метод в `reviewer/mcp/service.py`**

Module-level (рядом с прочими константами модуля):

```python
WALKTHROUGH_MARKER = "<!-- ai-walkthrough -->"
```

Метод (рядом с `publish_review` в `MCPReviewService`):

```python
    def post_pr_walkthrough(self, repo: str, pr: int, markdown: str) -> dict:
        """Опубликовать walkthrough-гид в PR как review-комментарий (без inline-находок).

        Маркер ``<!-- ai-walkthrough -->`` в body отделяет гид от ревью-находок
        (``<!-- ai-review:* -->``). Outward-facing — вызывается только по явной
        просьбе пользователя. Fail-soft при сетевой ошибке."""
        from reviewer.services.repo_id import normalize_repo
        sess = self._session(normalize_repo(repo), pr)
        prepared = sess.prepared
        body = f"{WALKTHROUGH_MARKER}\n\n{markdown}"
        try:
            prepared.vcs.publish_review(pr, prepared.prq.head_sha, body, [])
        except Exception as e:
            log.warning("post_pr_walkthrough: сбой постинга", exc_info=True)
            return {"posted": False, "reason": f"{type(e).__name__}: {e}"}
        return {"posted": True, "pr": pr}
```

- [ ] **Step 4: Прогнать — проходит**

Run: `.venv/bin/pytest tests/mcp/test_pr_walkthrough.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Зарегистрировать тул в `reviewer/entrypoints/mcp_server.py`** (после `get_candidate_findings`)

```python
    @mcp.tool()
    def post_pr_walkthrough(repo: str, pr: int, markdown: str) -> dict:
        """Post a human-facing PR reading guide (walkthrough) as a PR review comment,
        separate from bug findings (carries a <!-- ai-walkthrough --> marker, empty
        inline comments). Outward-facing: the /reviewer_pr-walkthrough skill calls this
        only on explicit user request. Requires an active prepare_review session."""
        return service.post_pr_walkthrough(repo, pr, markdown)
```

- [ ] **Step 6: Smoke сервера + линт + коммит**

```bash
.venv/bin/pytest tests/mcp/test_server.py tests/mcp/test_server_tools.py -q
.venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_pr_walkthrough.py
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_pr_walkthrough.py
git commit -m "feat(mcp): post_pr_walkthrough — постинг гида ревьюеру-человеку (PRI-119)"
```

(Если `test_server_tools.py` проверяет точный список имён тулов — добавить туда `post_pr_walkthrough`.)

---

### Task 2: Скилл `/reviewer_pr-walkthrough` + guard-тест

**Files:**
- Create: `plugin/skills/pr-walkthrough/SKILL.md`
- Test: `tests/skills/test_pr_walkthrough_skill.py`

**Interfaces:**
- Consumes тулы: `prepare_review`, `get_impact`, `get_changed_file_diff`, `find_callers`, `get_related_symbols` (есть); `get_subsystem_summaries` (PRI-159, опц.); `post_pr_walkthrough` (Task 1, опц.).

- [ ] **Step 1: Написать падающий guard-тест**

```python
# tests/skills/test_pr_walkthrough_skill.py
from pathlib import Path
import re

SKILL = (Path(__file__).resolve().parents[2]
         / "plugin" / "skills" / "pr-walkthrough" / "SKILL.md")


def test_skill_exists_and_uses_session_tools():
    text = SKILL.read_text(encoding="utf-8")
    assert "prepare_review" in text
    assert "get_impact" in text
    assert "find_callers" in text


def test_skill_includes_resolve_to_existing_common_files():
    text = SKILL.read_text(encoding="utf-8")
    includes = re.findall(r"<!-- include: (_common/[\w\-./]+) -->", text)
    assert includes, "нет include-маркеров _common"
    base = SKILL.resolve().parents[1]
    for inc in includes:
        assert (base / inc).is_file(), f"include не найден: {inc}"


def test_skill_posting_is_opt_in_and_russian():
    text = SKILL.read_text(encoding="utf-8")
    assert "post_pr_walkthrough" in text
    low = text.lower()
    assert "russian" in low or "русск" in low
    # постинг — только по явной просьбе (outward-facing)
    assert "explicit" in low or "only on explicit" in low or "явн" in low
```

- [ ] **Step 2: Прогнать — падает**

Run: `.venv/bin/pytest tests/skills/test_pr_walkthrough_skill.py -q`
Expected: FAIL (`FileNotFoundError`).

- [ ] **Step 3: Создать `plugin/skills/pr-walkthrough/SKILL.md`**

```markdown
---
name: reviewer_pr-walkthrough
description: Build a human-facing reading guide for a GitHub pull request (where to start, what each file changes, what it impacts) using the reviewer PR session + code graph. Use when the user asks to walk a human reviewer through a PR ("PR walkthrough", "гид по PR", "как читать этот PR", "проведи по PR"). NOT a bug review (see review-pr). Requires the reviewer MCP server + base index.
---

# PR walkthrough (reading guide for a human reviewer)

Help a human reviewer orient in a PR — not find bugs. Produce a markdown guide: where to start
(by centrality), what each file changes, and what it impacts ("careful, affects X"). Separate from
bug findings (`review-pr`).

**Always answer the user in Russian** (the project language). Tool calls, identifiers and `path:line`
stay verbatim.

## Tools

<!-- include: _common/tool-usage.md -->
Plus the PR-session tools (reviewer MCP): `prepare_review`, `get_impact`, `get_changed_file_diff`,
`find_callers`, `get_related_symbols`, `read_file`; optional `get_subsystem_summaries` (PRI-159);
optional `post_pr_walkthrough` (only on explicit user request).

## Pipeline

1. **Resolve repo & prepare the session.** Resolve `repo` (git remote). Call `prepare_review(repo, pr)`.
   If it returns `{"status": "skipped"}` (branch not in REVIEW_BRANCHES), tell the user (in Russian)
   and stop.

<!-- include: _common/branch-selection.md -->

2. **Reading order (centrality).** Call `get_impact(repo, pr)` → changed symbols and their callers.
   Order "start here" by how much depends on each changed symbol (most central first). Graph down →
   fall back to ordering by file (fail-open).

3. **What each file changes.** For each changed file, `get_changed_file_diff(repo, pr, path)` → one
   line describing what it changes.

4. **Impact ("careful, affects X").** For the central changed symbols, `find_callers` /
   `get_related_symbols` → who depends on them. Every "affects X" must be backed by a real caller.

5. **Subsystem prior (optional).** `get_subsystem_summaries(repo, branch)` → name the touched
   subsystem(s) in one line. Empty / unavailable → skip (fail-open).

6. **Assemble the guide (Russian markdown):**
   - **Начни отсюда** — ordered list (most central first).
   - **По файлам** — one line per changed file.
   - **Осторожно, влияет на** — impacted symbols + their callers.
   - (optional) **Подсистемы** — 1–2 lines from summaries.

7. **Output.** Print the guide to the user by default. Post to the PR ONLY on explicit user request:
   confirm first, then call `post_pr_walkthrough(repo, pr, markdown)` (posts a PR review comment with
   a `<!-- ai-walkthrough -->` marker, separate from bug findings).

## Grounding (hard rule)

<!-- include: _common/anti-hallucination.md -->

Every "affects X" is backed by a real `find_callers` result. Never invent callers or impact.

## Notes

- This is a reading guide, NOT a bug review — no findings, no inline severity comments.
- Posting to the PR is outward-facing and never happens without explicit user request + confirmation.
- Works without PRI-159 summaries and degrades gracefully when the graph is unavailable.
```

- [ ] **Step 4: Прогнать guard-тесты (и общий skills-набор) — проходят**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS (новый тест зелёный; `test_common_blocks`/`test_assembled_prompts` не сломаны).

- [ ] **Step 5: Полный прогон + коммит**

```bash
.venv/bin/pytest -q
git add plugin/skills/pr-walkthrough/SKILL.md tests/skills/test_pr_walkthrough_skill.py
git commit -m "feat(skills): скилл pr-walkthrough — гид по PR для ревьюера-человека (PRI-119)"
```

---

## Self-Review

**Spec coverage:**
- Гид по units PR (порядок чтения по центральности, что меняет файл, карта вызывающих) → Task 2 (шаги 2–4, тулы `get_impact`/`get_changed_file_diff`/`find_callers`).
- Вывод markdown в терминал, отдельно от находок-багов → Task 2 (шаг 6–7), маркер `<!-- ai-walkthrough -->` (Task 1).
- Опц. постинг в PR через `mcp/service.py` → Task 1 (`post_pr_walkthrough`), с подтверждением (Task 2 шаг 7).
- Опц. приор PRI-159 (fail-open) → Task 2 (шаг 5).
- Fail-open (граф down → порядок по файлам; ветка не отслеживается → skipped) → Task 2 (шаги 1–2).
- Тесты: unit постинга (фейк VCS), guard скилла → Tasks 1–2.

**Placeholder scan:** нет TBD/TODO; весь код и текст скилла приведены, команды и ожидаемый вывод указаны.

**Type consistency:** `post_pr_walkthrough(repo, pr, markdown)` одинаков в service-методе, MCP-туле, тесте и шаге 7 скилла; `WALKTHROUGH_MARKER` используется в методе и проверяется тестом; `publish_review(number, head_sha, body, comments)` соответствует `GitHubProvider.publish_review`.

**Вне объёма (из спека):** Python-хелпер упорядочивания (строит LLM), автопостинг без подтверждения, связка с PRI-112/PRI-115.
