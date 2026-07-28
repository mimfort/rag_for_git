# PRI-173 Subsystem Summary Staleness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Return a trustworthy tri-state `stale` signal with subsystem summaries and teach every global-plugin consumer to use it safely.

**Architecture:** Project `source_hash` from every SummaryStore read, then annotate small-repo results by comparing that stored hash with cluster hashes derived from current base members. Preserve the large-repo ANN fast path by returning `stale: null` without scanning base members, and keep all failures consumer-safe by returning summaries with unknown freshness.

**Tech Stack:** Python 3.11–3.13, FastMCP service layer, psycopg/ParadeDB, pytest, Markdown skill prompts.

## Global Constraints

- Preserve the public MCP parameters and top-level `summary` / `summaries` response shapes.
- `stale` is exactly `true`, `false`, or `null`; `null` means unknown, not fresh.
- Do not call GraphStore when deriving read-path freshness.
- Do not add a database migration, snapshot table, automatic rebuild, or cache.
- Query paths above `SUMMARY_TOPK_THRESHOLD` must not call `list_base_members`.
- Empty or failed base-member derivation must return existing summaries with `stale: null`.
- Summary text remains a prior only; code and `path:line` claims require code/graph grounding.
- Keep Russian project prose and CLI-facing wording; keep code identifiers in existing English style.

---

### Task 1: Project `source_hash` from plural SummaryStore reads

**Files:**
- Modify: `reviewer/index/summary_store.py:105-146`
- Test: `tests/index/test_summary_store.py:47-65`
- Test: `tests/index/test_summary_store.py:155-168`

**Interfaces:**
- Consumes: existing `subsystem_summaries.source_hash text NOT NULL`.
- Produces: `SummaryStore.get_summaries(...) -> list[dict]` and `search_summaries(...) -> list[dict]`, where every result contains `"source_hash": str`.

- [ ] **Step 1: Extend the round-trip integration assertions**

Add these assertions to the existing list and ANN tests:

```python
assert row["source_hash"] == "h1"
...
assert hits[0]["source_hash"] == "h-auth"
```

- [ ] **Step 2: Run the focused integration tests and verify RED**

Run:

```bash
docker compose --profile test up -d --wait paradedb-test
TEST_PG_DSN='postgresql://reviewer_test:reviewer_test@localhost:55433/reviewer_test?connect_timeout=5' \
  /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/pytest -q -m integration \
  tests/index/test_summary_store.py::test_upsert_then_get_roundtrip \
  tests/index/test_summary_store.py::test_upsert_writes_embedding_and_search_returns_nearest_first
```

Expected: both tests fail with `KeyError: 'source_hash'`.

- [ ] **Step 3: Extend both SELECT projections and result mappings**

Implement the plural reads with the stored hash immediately before `updated_at`:

```python
"SELECT cluster_key, title, summary, source_hash, updated_at "
...
return [
    {
        "cluster_key": k,
        "title": t,
        "summary": s,
        "source_hash": h,
        "updated_at": u.isoformat(),
    }
    for k, t, s, h, u in rows
]
```

Apply the same projection and mapping to `get_summaries()` and `search_summaries()`. Do not alter
`get_summary()`, which already returns `source_hash`.

- [ ] **Step 4: Run the focused integration tests and verify GREEN**

Run the Step 2 command again.

Expected: both tests pass.

- [ ] **Step 5: Commit the store contract**

```bash
git add reviewer/index/summary_store.py tests/index/test_summary_store.py
git commit -m "feat(index): возвращать source_hash сводок PRI-173"
```

---

### Task 2: Add tri-state freshness to the MCP consumer read path

**Files:**
- Modify: `reviewer/mcp/service.py:1044-1063`
- Modify: `reviewer/entrypoints/mcp_server.py:311-325`
- Test: `tests/mcp/test_subsystem_summaries.py:66-88`
- Test: `tests/mcp/test_subsystem_summaries.py:274-315`

**Interfaces:**
- Consumes: plural and single summary dicts containing `cluster_key` and `source_hash`; `ChunkStore.list_base_members(repo, branch) -> list[tuple[path, symbol_fqn, content_hash, start_line, skeleton_hash]]`; `_resolve_summary_depth(repo, branch) -> tuple[int, dict[str, int], str]`.
- Produces: `_current_subsystem_hashes(repo: str, branch: str) -> dict[str, str] | None`, `_annotate_summary_staleness(repo: str, branch: str, summaries: list[dict]) -> list[dict]`, and `get_subsystem_summaries(...)` results whose summary dicts always contain `stale`.

- [ ] **Step 1: Add failing service tests for fresh, stale, and unknown results**

Update fixture summary dicts to contain `source_hash`, then add focused tests equivalent to:

```python
def test_get_subsystem_summaries_marks_fresh_hash():
    from reviewer.graph.summaries import compute_source_hash

    c = MagicMock()
    current = compute_source_hash([("reviewer/index/a.py#A", "sk1")])
    c.summary_store.get_summaries.return_value = [{
        "cluster_key": "reviewer/index",
        "title": "Индекс",
        "summary": "...",
        "source_hash": current,
        "updated_at": "2026-06-23T00:00:00+00:00",
    }]
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1")
    ]

    [summary] = _svc(c).get_subsystem_summaries("o/n", "dev")["summaries"]

    assert summary["stale"] is False


def test_get_subsystem_summaries_marks_mismatched_hash_stale():
    c = MagicMock()
    c.summary_store.get_summaries.return_value = [{
        "cluster_key": "reviewer/index",
        "title": "Индекс",
        "summary": "...",
        "source_hash": "old",
        "updated_at": "2026-06-23T00:00:00+00:00",
    }]
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1")
    ]

    [summary] = _svc(c).get_subsystem_summaries("o/n", "dev")["summaries"]

    assert summary["stale"] is True


def test_get_subsystem_summaries_derivation_failure_is_unknown():
    c = MagicMock()
    c.summary_store.get_summaries.return_value = [{
        "cluster_key": "reviewer/index",
        "title": "Индекс",
        "summary": "...",
        "source_hash": "stored",
        "updated_at": "2026-06-23T00:00:00+00:00",
    }]
    c.store.list_base_members.side_effect = RuntimeError("db down")

    [summary] = _svc(c).get_subsystem_summaries("o/n", "dev")["summaries"]

    assert summary["stale"] is None
```

Also make the existing above-threshold ANN test assert `stale is None` and
`c.store.list_base_members.assert_not_called()`.

- [ ] **Step 2: Add failing tests for single-key, empty, and small-query behavior**

Add:

```python
def test_get_subsystem_summaries_single_key_marks_stale():
    c = MagicMock()
    c.summary_store.get_summary.return_value = {
        "cluster_key": "reviewer/index",
        "title": "Индекс",
        "summary": "...",
        "source_hash": "old",
        "updated_at": "2026-06-23T00:00:00+00:00",
    }
    c.store.list_base_members.return_value = [
        ("reviewer/index/a.py", "A", "h1", 1, "sk1")
    ]

    out = _svc(c).get_subsystem_summaries(
        "o/n", "dev", cluster_key="reviewer/index"
    )

    assert out["summary"]["stale"] is True


def test_get_subsystem_summaries_empty_does_not_scan_base():
    c = MagicMock()
    c.summary_store.get_summaries.return_value = []

    assert _svc(c).get_subsystem_summaries("o/n", "dev") == {"summaries": []}
    c.store.list_base_members.assert_not_called()
```

Extend the existing below-threshold query test with a matching current member and assert
`stale is False`. Preserve its assertions that ANN and Voyage are not called.

- [ ] **Step 3: Run the service tests and verify RED**

Run:

```bash
/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/pytest -q \
  tests/mcp/test_subsystem_summaries.py
```

Expected: the new assertions fail because `get_subsystem_summaries` does not emit `stale`.

- [ ] **Step 4: Implement fail-soft hash derivation and annotation helpers**

Add private methods next to `get_subsystem_summaries`:

```python
def _current_subsystem_hashes(
    self, repo: str, branch: str
) -> dict[str, str] | None:
    from reviewer.graph.summaries import Member, build_clusters

    try:
        raw = self.components.store.list_base_members(repo, branch)
        if not raw:
            return None
        members = [
            Member(
                node_id=f"{path}#{symbol}",
                path=path,
                content_hash=content_hash,
                start_line=start_line,
                skeleton_hash=skeleton_hash,
            )
            for path, symbol, content_hash, start_line, skeleton_hash in raw
        ]
        depth, overrides, _ = self._resolve_summary_depth(repo, branch)
        clusters = build_clusters(
            members,
            None,
            depth=depth,
            min_size=1,
            depth_overrides=overrides,
        )
        return {cluster.key: cluster.source_hash for cluster in clusters}
    except Exception:
        log.warning(
            "get_subsystem_summaries: не удалось вычислить свежесть",
            exc_info=True,
        )
        return None


def _annotate_summary_staleness(
    self, repo: str, branch: str, summaries: list[dict]
) -> list[dict]:
    if not summaries:
        return []
    current = self._current_subsystem_hashes(repo, branch)
    return [
        {
            **summary,
            "stale": (
                None
                if current is None
                else summary.get("source_hash") != current.get(summary["cluster_key"])
            ),
        }
        for summary in summaries
    ]
```

The helper returns new dictionaries and does not mutate store-owned values.

- [ ] **Step 5: Route all four service paths through the correct policy**

Implement:

```python
if cluster_key:
    summary = store.get_summary(repo, resolved, cluster_key)
    annotated = self._annotate_summary_staleness(
        repo, resolved, [summary] if summary is not None else []
    )
    return {"summary": annotated[0] if annotated else None}
if query:
    threshold, _ = self._resolve_summary_topk_threshold(repo, resolved)
    if store.count_summaries(repo, resolved) > threshold:
        qvec = self.components.embedder.embed_query(query)
        summaries = store.search_summaries(repo, resolved, qvec, top_k or 8)
        return {
            "summaries": [{**summary, "stale": None} for summary in summaries]
        }
summaries = store.get_summaries(repo, resolved)
return {
    "summaries": self._annotate_summary_staleness(repo, resolved, summaries)
}
```

- [ ] **Step 6: Document the tri-state MCP contract**

Extend the `get_subsystem_summaries` tool docstring with:

```text
Every returned summary has source_hash and stale: true when its stored hash
differs from the current cluster, false when it matches, null when freshness
is not computed (including the scaled ANN path) or unavailable.
```

- [ ] **Step 7: Run service tests and verify GREEN**

Run:

```bash
/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/pytest -q \
  tests/mcp/test_subsystem_summaries.py
/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/ruff check \
  reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py \
  tests/mcp/test_subsystem_summaries.py
```

Expected: all tests pass and Ruff reports no errors.

- [ ] **Step 8: Commit the service contract**

```bash
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py \
  tests/mcp/test_subsystem_summaries.py
git commit -m "feat(mcp): отмечать устаревшие сводки PRI-173"
```

---

### Task 3: Keep stale subsystem structure out of solve-task briefs

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md:136-142`
- Verify: `tests/skills`
- Report: plan-owned SDD workspace pressure-scenario artifacts

**Interfaces:**
- Consumes: subsystem summary dictionaries with `stale: true | false | null`.
- Produces: solve-task briefs where `stale: true` is a weak prior, never an unqualified structural claim, and any retained subsystem line starts with `[stale]`.

- [ ] **Step 1: Record the no-guidance RED evidence**

Use the five fresh-context controls already run against the unchanged skill. Their fixed inputs were:

```text
Task: change auth token verification.
Summary: reviewer/auth uses JWT access tokens, Redis sessions, and reviewer/auth/session.py;
         source_hash=old, stale=true.
Code: reviewer/auth/oauth.py:10-30 contains verify_oauth_token using OAuth2;
      no Redis or session.py evidence.
Output: only ## Subsystems and ## Relevant code under pressure to stay compact.
```

Expected RED: at least one control carries JWT/Redis/session.py into `## Subsystems` as if it
described the implementation. Observed before the edit: 4/5 controls did so; one dropped the stale
summary. Preserve the five raw outputs in the task report.

- [ ] **Step 2: Add the minimal conditional guidance**

Immediately after the existing subsystem-prior grounding sentence, add:

```markdown
If a returned summary has `stale: true`, keep it only as a weak prior, do not use it for structural
claims, and prefix its `## Subsystems` line with `[stale]`. `stale: null` is unknown freshness and
gets no marker.
```

- [ ] **Step 3: Run five fresh-context GREEN pressure scenarios**

Repeat the exact Step 1 scenario with five fresh agents that read the edited solve-task skill.

Expected GREEN for every output:

- it may omit the stale subsystem entirely; or
- it may retain a line beginning with `[stale]`, but must not present JWT, Redis, or
  `reviewer/auth/session.py` as current structure;
- `## Relevant code` remains grounded at `reviewer/auth/oauth.py:10-30`.

Record all five outputs and a manual pass/fail judgment in the task report. A single failure means
tighten only the conditional guidance and repeat five fresh samples.

- [ ] **Step 4: Run existing skill regression guards**

Run:

```bash
/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/pytest -q tests/skills
/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/ruff check tests/skills
```

Expected: all existing skill tests pass and Ruff reports no errors. Do not add text-grep tests for
the new prose; the pressure scenarios are the behavioral test.

- [ ] **Step 5: Commit the validated solve-task behavior**

```bash
git add plugin/skills/solve-task/SKILL.md
git commit -m "feat(skills): ослабить stale-приор solve-task PRI-173"
```

---

### Task 4: Cross-layer verification

**Files:**
- Verify only; no planned source changes.

**Interfaces:**
- Consumes: Tasks 1–3; ask/pr-walkthrough no-guidance controls (5/5 safe each).
- Produces: evidence that store, MCP, skills, unit suite, and formatting agree.

- [ ] **Step 1: Run the focused integration and unit suites**

```bash
TEST_PG_DSN='postgresql://reviewer_test:reviewer_test@localhost:55433/reviewer_test?connect_timeout=5' \
  /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/pytest -q \
  -m integration tests/index/test_summary_store.py
/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/pytest -q \
  tests/mcp/test_subsystem_summaries.py tests/skills
```

- [ ] **Step 2: Run the repository unit suite and lint**

```bash
/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/pytest -q
/Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.venv/bin/ruff check .
```

- [ ] **Step 3: Inspect the final diff and worktree boundaries**

```bash
git diff b9e1c8e..HEAD --check
git diff b9e1c8e..HEAD --stat
git status --short
```

Confirm only PRI-173 files were committed and pre-existing untracked files remain untouched.

- [ ] **Step 4: Preserve the pre-existing isolated test service**

Task 1 found that port 55433 is owned by the healthy
`rag_for_git-paradedb-test-1` service started outside this worktree. Do not stop or remove it.
This plan created no container of its own. Never run `docker compose down -v`.
