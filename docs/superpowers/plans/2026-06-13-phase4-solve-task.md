# Phase 4 — /solve-task Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `/solve-task` skill that gathers disciplined context for a task (board task + related/similar tasks + relevant code), distills a structured brief, and hands off to `superpowers:brainstorming`; plus one new session-less MCP tool `search_codebase` for code search without a PR session.

**Architecture:** `search_codebase` is a session-less code search over the base index (`Retriever.search_base`) that mirrors `Retriever.retrieve`'s pipeline — hybrid BM25+ANN → graph-expansion seeded from the top hits → rerank — but base-only (`changed_paths=[]` + a non-matching overlay ref) and with graph/reranker fail-soft. The skill reuses phase-2 board playbooks and phase-3 task tools (`index_task`/`search_tasks`/`get_task_context`), and chains into the superpowers dev cycle rather than reimplementing it.

**Tech Stack:** Python 3.11–3.13, Postgres/ParadeDB (pgvector + pg_search BM25, RRF), Voyage embeddings, FastMCP, Claude Code skills (English).

**Spec:** `docs/superpowers/specs/2026-06-13-phase4-solve-task-design.md`

---

## Conventions

- Code/docstrings/comments — Russian; skills & LLM prompts — English.
- Commits: Conventional Commits in Russian, **no self-attribution** (no `Co-Authored-By` / Claude / AI).
- Lint: `main` is NOT ruff-clean (pre-existing debt). Scope `.venv/bin/ruff check` to changed files.
- Tests: `pytest` excludes `integration` by default. Unit tests fake/mock; real Postgres only under `@pytest.mark.integration`.
- The venv is already created at `.venv` (`.venv/bin/pytest`, `.venv/bin/ruff`).

## File Structure

**New files:**
- `plugin/skills/solve-task/SKILL.md` — the skill.
- `tests/retrieval/__init__.py`, `tests/retrieval/test_search_base.py` — unit test for `Retriever.search_base`.
- `tests/tasks/test_search_codebase_integration.py` — integration test (non-destructive base search).

**Modified files:**
- `reviewer/retrieval/retriever.py` — `Retriever.search_base`.
- `reviewer/mcp/service.py` — `MCPReviewService.search_codebase`.
- `reviewer/entrypoints/mcp_server.py` — register `search_codebase` tool; bump tool-count docstring.
- `tests/mcp/test_service.py` — `search_codebase` delegate test.
- `tests/mcp/test_server_tools.py` — `search_codebase` registration test.
- `tests/mcp/test_server.py` — tool-count set (11 → 12).
- `README.md` — `/solve-task` note.

---

## Task 0: Baseline

**Files:** none.

- [ ] **Step 1: Confirm clean baseline**

Run: `.venv/bin/pytest -q`
Expected: PASS (the merged phase-3 suite — ~262 passed, 1 skipped). If failures, report before proceeding.

- [ ] **Step 2: Confirm infra (for the integration task)**

Run: `docker compose up -d && .venv/bin/reviewer check`
Expected: Postgres :5433 reachable.

---

## Task 1: Retriever.search_base (session-less base search)

**Files:**
- Modify: `reviewer/retrieval/retriever.py`
- Test: `tests/retrieval/__init__.py` (empty), `tests/retrieval/test_search_base.py`

- [ ] **Step 1: Write the failing test**

`tests/retrieval/__init__.py`: empty file.

`tests/retrieval/test_search_base.py`:
```python
from reviewer.retrieval.retriever import ContextPack, Retriever


class _Hit:
    def __init__(self, node_id, score=1.0):
        self.node_id = node_id
        self.path, self.symbol_fqn = node_id.split("#", 1)
        self.kind = "function"
        self.start_line = 1
        self.end_line = 2
        self.text = "body"
        self.score = score


class _FakeStore:
    def __init__(self, hits, related=None):
        self._hits = hits
        self._related = related or []
        self.search_calls = []
        self.fetch_calls = []

    def hybrid_search(self, *, query_text, query_embedding, overlay_ref,
                      changed_paths, top_k, candidates):
        self.search_calls.append({
            "overlay_ref": overlay_ref, "changed_paths": changed_paths,
            "top_k": top_k, "candidates": candidates,
        })
        return self._hits

    def fetch_nodes(self, node_ids, overlay_ref, changed_paths):
        self.fetch_calls.append({
            "node_ids": list(node_ids), "overlay_ref": overlay_ref,
            "changed_paths": changed_paths,
        })
        return self._related


class _FakeEmbedder:
    def __init__(self):
        self.queries = []

    def embed_query(self, text):
        self.queries.append(text)
        return [0.1] * 8


class _FakeGraph:
    def __init__(self, related_ids=(), raise_=False):
        self._ids = set(related_ids)
        self.expand_calls = []
        self._raise = raise_

    def expand(self, node_ids, hops=2):
        self.expand_calls.append({"seeds": list(node_ids), "hops": hops})
        if self._raise:
            raise RuntimeError("neo4j down")
        return set(self._ids)


class _FakeReranker:
    def __init__(self):
        self.calls = []

    def rerank(self, query, items, top_k):
        self.calls.append({"n": len(items), "top_k": top_k})
        return list(items)[:top_k]


def test_search_base_is_base_only_and_seeds_graph_from_hits():
    hits = [_Hit("a.py#f1"), _Hit("b.py#f2"), _Hit("c.py#f3"), _Hit("d.py#f4")]
    related = [_Hit("e.py#neighbor")]
    store, graph, reranker, emb = _FakeStore(hits, related), _FakeGraph({"e.py#neighbor"}), _FakeReranker(), _FakeEmbedder()
    r = Retriever(store, graph, emb, reranker, max_context_chars=8000)
    pack = r.search_base("logout", top_k=3)
    assert isinstance(pack, ContextPack)
    # base-only hybrid search
    assert store.search_calls[0]["changed_paths"] == []
    assert store.search_calls[0]["overlay_ref"] != "base"
    assert emb.queries == ["logout"]
    # graph-expansion seeded from TOP hits (not changed files), neighbors fetched base-only
    assert graph.expand_calls and graph.expand_calls[0]["seeds"][0] == "a.py#f1"
    assert store.fetch_calls[0]["changed_paths"] == []
    # rerank applied (candidates > top_k and graph added a new node)
    assert reranker.calls and reranker.calls[0]["top_k"] == 3
    assert "a.py#f1" in pack.as_context()


def test_search_base_graph_down_falls_back_to_hybrid():
    store = _FakeStore([_Hit("a.py#f1")])
    r = Retriever(store, _FakeGraph(raise_=True), _FakeEmbedder(), _FakeReranker(), max_context_chars=8000)
    assert "a.py#f1" in r.search_base("x").as_context()  # graph error swallowed


def test_search_base_no_reranker_returns_rrf_order():
    store = _FakeStore([_Hit("a.py#f1"), _Hit("b.py#f2")])
    r = Retriever(store, graph=None, embedder=_FakeEmbedder(), reranker=None)
    pack = r.search_base("x", top_k=5)
    assert [it.node_id for it in pack.items] == ["a.py#f1", "b.py#f2"]


def test_search_base_empty_returns_empty_pack():
    r = Retriever(_FakeStore([]), graph=None, embedder=_FakeEmbedder(), reranker=None)
    assert r.search_base("nothing").as_context() == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/retrieval/test_search_base.py -q`
Expected: FAIL — `Retriever` has no attribute `search_base`.

- [ ] **Step 3: Add `search_base` to `reviewer/retrieval/retriever.py`**

First add a module logger at the top of the file (the file currently has no logging):
```python
import logging

log = logging.getLogger(__name__)
```
(place after the existing `from __future__ import annotations` / `from dataclasses import dataclass` imports.)

Then add this method to the `Retriever` class (after `retrieve`). It mirrors `retrieve`'s pipeline — hybrid → graph-expand → rerank — but is **base-only** and seeds graph-expansion from the **top hits** (no PR/changed files). Graph and reranker are **fail-soft**:
```python
    def search_base(self, query, top_k=10, candidates=50) -> ContextPack:
        """Гибрид-поиск по base-индексу без PR-сессии — для /solve-task.

        Зеркало :meth:`retrieve`, но base-only и сидинг графа от хитов:
        ``changed_paths=[]`` + несуществующий ``overlay_ref="__none__"`` → WHERE отбирает
        только base-строки. graph-expansion идёт от топ-хитов (а не от changed-файлов),
        затем rerank. Граф и реранкер fail-soft: недоступны/ошибка → деградация до
        чистого гибрида / RRF-порядка.
        """
        qvec = self.embedder.embed_query(query)
        hits = self.store.hybrid_search(
            query_text=query, query_embedding=qvec,
            overlay_ref="__none__", changed_paths=[],
            top_k=candidates, candidates=candidates)
        merged: dict[str, object] = {}
        for h in hits:
            merged.setdefault(h.node_id, h)
        graph_new = False
        if self.graph is not None and hits:
            try:
                seeds = [h.node_id for h in hits[:top_k]]
                related_ids = self.graph.expand(seeds, hops=1)
                related = self.store.fetch_nodes(list(related_ids), "__none__", [])
                for it in related:
                    if it.node_id not in merged:
                        merged[it.node_id] = it
                        graph_new = True
            except Exception:
                log.warning("search_base: graph-expansion недоступен", exc_info=True)
        items = list(merged.values())
        if self.reranker is None or len(items) <= 3 or (len(items) <= top_k and not graph_new):
            return ContextPack(items=items[:top_k], max_chars=self.max_context_chars)
        try:
            items = self.reranker.rerank(query, items, top_k=top_k)
        except Exception:
            log.warning("search_base: rerank недоступен — RRF-порядок", exc_info=True)
            items = items[:top_k]
        return ContextPack(items=items, max_chars=self.max_context_chars)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/retrieval/test_search_base.py -q`
Expected: PASS (4 tests: base-only+graph-seed+rerank; graph-down fallback; no-reranker RRF order; empty).

- [ ] **Step 5: Lint & commit**

```bash
.venv/bin/ruff check reviewer/retrieval/retriever.py tests/retrieval/test_search_base.py
git add reviewer/retrieval/retriever.py tests/retrieval/__init__.py tests/retrieval/test_search_base.py
git commit -m "feat(retrieval): Retriever.search_base — session-less поиск по base-индексу"
```

---

## Task 2: MCP search_codebase — delegate + tool registration

**Files:**
- Modify: `reviewer/mcp/service.py`
- Modify: `reviewer/entrypoints/mcp_server.py`
- Test: `tests/mcp/test_service.py`, `tests/mcp/test_server_tools.py`, `tests/mcp/test_server.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/mcp/test_service.py` (the file already has `_make_mcp_service`/`_components` helpers; `_components()` returns a MagicMock, so `components.retriever.search_base` is an auto-MagicMock):
```python
def test_search_codebase_delegates_to_retriever() -> None:
    """search_codebase зовёт retriever.search_base и форматирует ContextPack."""
    svc = _make_mcp_service()
    svc.components.retriever.search_base.return_value.as_context.return_value = "auth.py#logout\nbody"
    out = svc.search_codebase("logout", top_k=5)
    assert "auth.py#logout" in out
    svc.components.retriever.search_base.assert_called_once_with("logout", top_k=5)


def test_search_codebase_empty_or_error_returns_note() -> None:
    """Пустой результат или сбой → '(ничего не найдено)'."""
    svc = _make_mcp_service()
    svc.components.retriever.search_base.return_value.as_context.return_value = ""
    assert svc.search_codebase("x") == "(ничего не найдено)"
    svc.components.retriever.search_base.side_effect = RuntimeError("pg down")
    assert svc.search_codebase("x") == "(ничего не найдено)"
```

Append to `tests/mcp/test_server_tools.py`:
```python
def test_search_codebase_tool_registered():
    import asyncio

    svc = _service()
    svc.search_codebase.return_value = "code"
    server = create_server(svc)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "search_codebase" in names
```

In `tests/mcp/test_server.py` — find the test that asserts the exact set of tool names (currently 11 names incl. `index_task`/`search_tasks`/`get_task_context`) and add `"search_codebase"` to the expected set (now 12).

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/mcp/test_service.py -k search_codebase tests/mcp/test_server_tools.py -k search_codebase -q`
Expected: FAIL — `MCPReviewService` has no `search_codebase`; tool not registered.

- [ ] **Step 3: Add the delegate to `reviewer/mcp/service.py`**

Add a method next to the other task delegates (after `get_task_context`):
```python
    def search_codebase(self, query: str, top_k: int = 10) -> str:
        """Гибрид-поиск по base-индексу репозитория (без PR-сессии) — для /solve-task."""
        try:
            pack = self.components.retriever.search_base(query, top_k=top_k)
        except Exception:
            log.warning("search_codebase: сбой поиска", exc_info=True)
            return "(ничего не найдено)"
        return pack.as_context() or "(ничего не найдено)"
```

- [ ] **Step 4: Register the tool in `reviewer/entrypoints/mcp_server.py`**

Add inside `create_server`, next to the other repo-global task tools (after `get_task_context`):
```python
    @mcp.tool()
    def search_codebase(query: str, top_k: int = 10) -> str:
        """Hybrid semantic+lexical search over the repo's base code index (no PR session).
        Use it (e.g. from /solve-task) to find relevant existing code by a free-text
        formulation, when there is no PR to scope a session to."""
        return service.search_codebase(query, top_k)
```
Update the `create_server` docstring tool count: "11 тулами" → "12 тулами".

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/pytest tests/mcp/ -q`
Expected: PASS (existing + new; `test_server.py` count test green at 12).

- [ ] **Step 6: Lint & commit**

```bash
.venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_service.py tests/mcp/test_server_tools.py tests/mcp/test_server.py
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_service.py tests/mcp/test_server_tools.py tests/mcp/test_server.py
git commit -m "feat(mcp): тул search_codebase — session-less поиск кода по base-индексу"
```

---

## Task 3: Integration test for search_codebase (non-destructive)

**Files:**
- Test: `tests/tasks/test_search_codebase_integration.py`

This adds a base chunk at a **test-only path** (so it never collides with a real index), searches for a nonsense marker word (BM25 lexical hit — robust with a fake embedder), asserts it's found, then deletes only that path. It does NOT truncate the real base index.

- [ ] **Step 1: Write the failing integration test**

`tests/tasks/test_search_codebase_integration.py`:
```python
"""Integration: search_codebase (Retriever.search_base) на живом Postgres, без Voyage.

Добавляет base-чанк на ТЕСТОВОМ пути с маркер-словом, ищет его (BM25-хит), затем удаляет
только этот путь — не трогает реальный base-индекс. Маркер integration.
"""
from __future__ import annotations

import hashlib

import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkRow, ChunkStore
from reviewer.retrieval.retriever import Retriever

pytestmark = pytest.mark.integration

_TEST_PATH = "__solve_task_fixture__.py"
_MARKER = "zzsolvetaskmarker"


def _vec(seed: str) -> list[float]:
    h = hashlib.sha256(seed.encode()).digest()
    return [((h[i % len(h)] + i) % 17) / 17.0 for i in range(1024)]


class _FakeEmbedder:
    def embed_documents(self, texts):
        return [_vec(t) for t in texts]

    def embed_query(self, text):
        return _vec(text)


def test_search_codebase_finds_base_chunk():
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    emb = _FakeEmbedder()
    text = f"def logout(session):\n    session.clear()  # {_MARKER}"
    try:
        store.delete_paths("base", [_TEST_PATH])  # гигиена от прошлых прогонов
        store.upsert([ChunkRow(
            ref="base", content_hash="h_solve", path=_TEST_PATH, lang="python",
            symbol_fqn="logout", kind="function", start_line=1, end_line=2,
            text=text, embedding=emb.embed_documents([text])[0])])

        r = Retriever(store, graph=None, embedder=emb, reranker=None, max_context_chars=8000)
        ctx = r.search_base(_MARKER, top_k=5).as_context()
        assert f"{_TEST_PATH}#logout" in ctx
    finally:
        store.delete_paths("base", [_TEST_PATH])
        store.close()
```

- [ ] **Step 2: Run the integration test (infra up)**

Run: `.venv/bin/pytest tests/tasks/test_search_codebase_integration.py -m integration -q`
Expected: PASS. (BM25 matches the marker word; the fake embedder only needs to provide a valid 1024-vector.)

- [ ] **Step 3: Confirm default run excludes it**

Run: `.venv/bin/pytest tests/tasks/ -q`
Expected: unit green, this file deselected.

- [ ] **Step 4: Lint & commit**

```bash
.venv/bin/ruff check tests/tasks/test_search_codebase_integration.py
git add tests/tasks/test_search_codebase_integration.py
git commit -m "test(retrieval): integration search_codebase на живом Postgres (фейк-эмбеддер)"
```

---

## Task 4: /solve-task skill

**Files:**
- Create: `plugin/skills/solve-task/SKILL.md`

Prompt-markdown (no pytest). Verification = the checklist in Step 2. Match the frontmatter format of `plugin/skills/review-pr/SKILL.md` (a `---` block with `description:`). Confirm the relative path `../review-pr/references/task-context-yougile.md` resolves from `plugin/skills/solve-task/` (it points to `plugin/skills/review-pr/references/task-context-yougile.md`).

- [ ] **Step 1: Create `plugin/skills/solve-task/SKILL.md`**

```markdown
---
description: Gather disciplined context for solving a task, then hand off to development. Use when the user asks to solve/implement a task ("solve PRI-4", "/solve-task <key or description>", "реши задачу X"). Reads the task from a connected board (if a key + board), pulls related/similar tasks and relevant code, distills a brief, and enters brainstorming. Requires the reviewer MCP server (and optionally a board MCP).
---

# Solve Task

Gather the right context for a task, distill it into a brief, then enter the normal development
workflow. This skill does NOT plan or implement — it disciplines context-gathering and hands the
brief to `superpowers:brainstorming` (which leads to writing-plans → subagent-driven-development).

## Inputs

`$ARGUMENTS` is either:
- a task key (e.g. `PRI-4`, matching the board's `key_pattern`), or
- a free-text description (e.g. "add a logout endpoint").

## Pipeline

1. **Config.** Read `.review.yml` from the repo. If it has a `task_board` block (`type`, `mcp`,
   `key_pattern`), a board is configured and its tools are `mcp__<task_board.mcp>__*`. No block, or
   the board MCP is not connected → board-less mode (continue without it).

2. **Identify the task.**
   - If `$ARGUMENTS` matches the board's `key_pattern` AND a board is configured/connected: read the
     task via the playbook `../review-pr/references/task-context-<task_board.type>.md` and build a
     `TaskBrief` `{key, aliases[], title, description, criteria[], status, url, links[]}`. Then call
     `index_task(TaskBrief)` to persist it (idempotent — safe to repeat).
   - Otherwise: treat `$ARGUMENTS` as the task description; do not read the board.

3. **Gather context (best-effort, fail-open).** Any tool returning a "(… unavailable)" / "(ничего не
   найдено)" note or an error is non-fatal — continue.
   - If you have a task key: `get_task_context(key)` → linked tasks, their PRs, and the code those PRs
     touched.
   - `search_tasks("<title>. <first lines of description>")` → semantically similar tasks. If a board
     is connected, you may read the most relevant similar tasks from the board for fuller detail.
   - `search_codebase("<task description>")` → relevant existing code (files/symbols to touch or
     mimic).

4. **Distill the solution brief.** Write a structured markdown brief. Apply a strict relevance
   filter: include an item ONLY if it directly informs the implementation; drop the rest and note how
   many were dropped. Sections:
   - **Task** — key/title/requirements/criteria (or the user's formulation in board-less mode).
   - **Related work** — only the relevant linked/similar tasks and their PRs (what to reuse / follow).
   - **Relevant code** — files/symbols to touch or mimic, each with a one-line "why".
   - **Constraints / open questions** — limits, unknowns, and context gaps (e.g. "board unavailable",
     "task corpus empty").

5. **Hand off to development.** Show the brief, then invoke `superpowers:brainstorming` with the brief
   as the seed/context. From there the normal cycle takes over (brainstorming → writing-plans →
   subagent-driven-development/TDD). Your job ends at the handoff — do NOT plan or implement here.

## Failure handling (fail-open)

- No `task_board` / board MCP not connected / task not found → board-less: build the brief from
  `search_tasks` (if the corpus is warm) + `search_codebase` + the user's formulation; note the gap.
- Neo4j down → `get_task_context` / `index_task` graph parts degrade (empty + warning); build the
  brief from `search_tasks` + `search_codebase`.
- Empty task corpus (no prior `/sync-tasks` or reviews) → `search_tasks` is empty; rely on the board
  (if a key) + `search_codebase`.
- Never abort: with any gap, distill what you have, note the deficit in the brief, and still hand off
  to brainstorming.
- Read-only on the board; this skill never writes to it.
```

- [ ] **Step 2: Verify (manual checklist — no pytest)**

Confirm:
- frontmatter is valid YAML (`---` + `description:`), matching `review-pr/SKILL.md`;
- the relative playbook path resolves: `ls plugin/skills/review-pr/references/task-context-yougile.md` exists, and `../review-pr/references/...` from `plugin/skills/solve-task/` points to it;
- the flow is key-OR-free-text, fail-open, ends by entering `superpowers:brainstorming` with the brief;
- tool names used (`index_task`, `search_tasks`, `get_task_context`, `search_codebase`) match what the MCP server registers (grep `reviewer/entrypoints/mcp_server.py`);
- English throughout; never writes to the board.

- [ ] **Step 3: Commit**

```bash
git add plugin/skills/solve-task/
git commit -m "feat(skill): /solve-task — сбор контекста задачи и передача в разработку"
```

---

## Task 5: README note

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a short `/solve-task` note (Russian)**

Near the phase-3 "Граф и RAG по задачам" paragraph (or the skills/MCP-tools section), add:
```markdown
**Скилл `/solve-task` (фаза 4).** `/solve-task <ключ | свободный текст>` собирает контекст под
задачу — читает задачу с доски (если есть ключ и подключена доска), тянет связанные и похожие
задачи с их PR и кодом (`get_task_context`/`search_tasks`), ищет релевантный код по формулировке
(`search_codebase` — session-less гибрид-поиск по base-индексу), сводит **только релевантное** в
структурированный бриф и передаёт его в штатный цикл разработки (`brainstorming` → план →
реализация). Скилл дисциплинирует сбор контекста, не заменяя разработку; fail-open — без доски/графа
работает по формулировке и коду.
```
If README enumerates MCP tools in a table, add `search_codebase` with a one-line gloss.

- [ ] **Step 2: Verify (manual)** — consistent with the implemented behaviour; Russian; no contradiction with phase-3 text.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): скилл /solve-task и тул search_codebase (фаза 4)"
```

---

## Task 6: Final verification

**Files:** none.

- [ ] **Step 1: Full unit suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (phase-3 baseline + new phase-4 tests).

- [ ] **Step 2: Integration suite (infra up)**

Run: `docker compose up -d && .venv/bin/pytest -m integration -q`
Expected: the new `search_codebase` integration test passes; note any PRE-EXISTING failure unrelated to phase 4 (e.g. `tests/integration/test_pipeline.py` needs `VOYAGE_API_KEY`).

- [ ] **Step 3: Lint changed files only**

Run:
```bash
.venv/bin/ruff check reviewer/retrieval/retriever.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/retrieval/ tests/tasks/test_search_codebase_integration.py
```
Expected: clean.

- [ ] **Step 4: Skill sanity**

Run: `grep -c "^description:" plugin/skills/solve-task/SKILL.md` → 1 (valid frontmatter). Eyeball the diff.

---

## Self-Review (plan vs spec) — completed by author

- **session-less `search_codebase`** → Task 1 (`Retriever.search_base`) + Task 2 (MCP delegate + tool). ✓
- **base-only mechanism** (`changed_paths=[]` + non-matching overlay) → Task 1, asserted in unit + integration. ✓
- **skill: key OR free-text, board read via phase-2 playbook, index_task, get_task_context/search_tasks/search_codebase, structured brief with relevance filter, enter brainstorming** → Task 4. ✓
- **fail-open degradation** (no board / Neo4j down / empty corpus / Postgres down) → Task 4 skill + Task 2 delegate (`(ничего не найдено)` on error). ✓
- **reuse** (board playbooks, phase-3 task tools, superpowers cycle) → Task 4. ✓
- **docs** → Task 5. ✓
- **tests**: unit (search_base params/skip-graph/empty; delegate; registration), integration (base search non-destructive) → Tasks 1–3. ✓
- **Open spec questions resolved**: base-search lives on `Retriever.search_base` (testable, reuses query-embed); neutral overlay ref = `"__none__"` (asserted `!= "base"` in unit, exercised in integration). ✓

No placeholders; types/signatures consistent (`Retriever.search_base(query, top_k=10, candidates=50) -> ContextPack`; `MCPReviewService.search_codebase(query, top_k=10) -> str`; tool `search_codebase(query, top_k=10)`).
