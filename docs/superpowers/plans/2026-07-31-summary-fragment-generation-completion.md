# Summary Fragment Generation Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make capped bootstrap and depth rebuild converge without regenerating
clusters whose current-depth fragments were already committed.

**Architecture:** Pure helpers own the reserved provenance stamp and exact
same-cluster completion predicate. `MCPReviewService` derives bootstrap and
full-rebuild flags per cluster from those helpers while retaining branch-level
`completed_depth` as the final prune marker.

**Tech Stack:** Python 3.11–3.13, Pydantic v2, pytest, FastMCP.

## Global Constraints

- Completion evidence is server-owned and overwrites client `_reviewer`.
- Completion requires every current path/fingerprint in the same cluster at the
  current effective depth and generation `summary-fragment-v1`.
- Incomplete bootstrap/full-rebuild regenerates the entire cluster.
- Completed clusters become reusable before global `completed_depth` advances.
- No database schema migration.
- Do not update global MCP configuration until tests and review pass.

---

### Task 1: Cluster-specific generation completion

**Files:**

- Modify: `reviewer/services/summary_fragments.py`
- Modify: `reviewer/mcp/service.py`
- Modify: `tests/services/test_summary_fragments.py`
- Modify: `tests/mcp/test_subsystem_summaries.py`

**Interfaces:**

- Produces:
  `with_server_generation_provenance(provenance, depth) -> dict`
- Produces:
  `has_complete_fragment_generation(cluster_key, current, stored, depth) -> bool`
- `MCPReviewService` uses per-cluster `(bootstrap, full_rebuild)` flags for delta,
  stale, cap eligibility, list output, and work output.

- [ ] **Step 1: Write failing pure-helper and service regressions**

Add literal tests proving:

```python
assert with_server_generation_provenance(
    {"model": "cheap", "_reviewer": {"depth": 999}},
    2,
) == {
    "model": "cheap",
    "_reviewer": {"generation": "summary-fragment-v1", "depth": 2},
}
```

Add `cap=1` multi-pass service cases for:

```text
first-upgrade: completed_depth=None, fresh legacy hashes, no stamped fragments
depth-change: completed_depth=1, effective depth=2, depth-1 stamped fragments
```

In each case, persist the first selected cluster, feed the server-stamped stored
fragment back into the next service snapshot, and assert:

```text
completed cluster: bootstrap/full_rebuild false, stale false, pending paths empty
remaining cluster: selected, deferred reaches zero
```

Add a persistence test whose client provenance contains a spoofed `_reviewer`
value and assert the store receives current server depth/generation.

- [ ] **Step 2: Run RED tests**

```bash
../../.venv/bin/pytest \
  tests/services/test_summary_fragments.py \
  tests/mcp/test_subsystem_summaries.py -q
```

Expected: failures because generation helpers and cluster-local completion do not
exist.

- [ ] **Step 3: Implement the pure provenance/completion helpers**

In `reviewer/services/summary_fragments.py`, add:

```python
_SERVER_PROVENANCE_KEY = "_reviewer"
_GENERATION = "summary-fragment-v1"

def with_server_generation_provenance(provenance, depth):
    return {
        **dict(provenance),
        _SERVER_PROVENANCE_KEY: {
            "generation": _GENERATION,
            "depth": depth,
        },
    }
```

`has_complete_fragment_generation` must build same-cluster stored fragments by
path and return true only when every current path has exact fingerprint plus the
exact reserved stamp.

- [ ] **Step 4: Make MCP summary state cluster-specific**

Add a service helper that returns:

```python
complete = has_complete_fragment_generation(
    cluster.key,
    current_fingerprints,
    state.fragments,
    state.depth,
)
bootstrap = state.bootstrap and not complete
full_rebuild = state.full_rebuild and not complete
```

Use these flags in `_summary_delta`, stale calculation, cap eligibility,
serialized list fields, and `get_subsystem_summary_work`. Before
`commit_summary_bundle`, replace every incoming fragment provenance through
`with_server_generation_provenance(..., state.depth)`.

- [ ] **Step 5: Run focused GREEN tests and ruff**

```bash
../../.venv/bin/pytest \
  tests/services/test_summary_fragments.py \
  tests/mcp/test_subsystem_summaries.py -q
../../.venv/bin/ruff check \
  reviewer/services/summary_fragments.py reviewer/mcp/service.py \
  tests/services/test_summary_fragments.py tests/mcp/test_subsystem_summaries.py
git diff --check
```

- [ ] **Step 6: Review and commit**

Request a read-only review of the implementation and regressions. Fix every
Critical/Important issue, rerun focused checks, then commit:

```bash
git add reviewer/services/summary_fragments.py reviewer/mcp/service.py \
  tests/services/test_summary_fragments.py tests/mcp/test_subsystem_summaries.py
git commit -m "fix(mcp): завершать capped generation по кластерам (PRI-226)"
```

### Task 2: Full verification, report, and local MCP deployment

**Files:**

- Modify:
  `.superpowers/sdd/2026-07-31-pri-226-incremental-file-summary-fragments/task-4-report.md`
- Global config only after code review and tests.

- [ ] **Step 1: Run full verification**

```bash
../../.venv/bin/pytest -q
../../.venv/bin/ruff check reviewer tests
git diff --check
```

- [ ] **Step 2: Reconfigure only the reviewer MCP**

Using public `codex mcp` CLI, replace the single `reviewer` entry with:

```text
uvx --from /Users/aleksejzadoroznyj/PycharmProjects/rag_for_git/.worktrees/pri-226-incremental-file-summaries reviewer-mcp
```

Verify exactly one enabled `reviewer`; preserve `openaiDeveloperDocs` and all
unrelated entries. Keep the already installed local-worktree plugin unchanged.

- [ ] **Step 3: Append report**

Record RED/GREEN evidence, review, commits, test outputs, final plugin/MCP state,
and the requirement to preserve the feature worktree until remote release and
reinstall.
