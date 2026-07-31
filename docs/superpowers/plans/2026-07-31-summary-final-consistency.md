# Summary Final Consistency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close all final-review consistency races in subsystem summaries.

**Architecture:** A canonical layout token flows from effective policy through list,
fragment provenance, verified prune, and completed state. Store performs final
coverage verification under the existing branch advisory lock. Backfill and move
reuse use exact snapshot identity.

**Tech Stack:** Python 3.11+, FastMCP, psycopg/PostgreSQL, pytest, Ruff.

## Global Constraints

- Preserve existing skeleton-hash freshness semantics.
- Legacy prune calls and legacy state rows must fail closed without deletion.
- No external-service access in unit tests; use only isolated test ParadeDB for integration.
- Keep the feature worktree and branch; do not push or merge.

---

### Task 1: Layout generation identity

**Files:** `reviewer/graph/summaries.py`, `reviewer/services/summary_fragments.py`,
`reviewer/index/schema.sql`, `reviewer/index/summary_store.py`,
`reviewer/mcp/service.py`, focused graph/service/MCP/store tests.

- [ ] Add failing tests for canonical sorted overrides, legacy incomplete state,
  fixed-depth override rebuild, server stamp, and capped convergence.
- [ ] Run focused tests and record expected failures.
- [ ] Implement `compute_layout_token`, nullable `completed_layout`, exact stamp,
  and layout-based state flags.
- [ ] Run focused tests and commit the green slice.

### Task 2: Verified prune protocol

**Files:** `reviewer/index/summary_store.py`, `reviewer/mcp/service.py`,
`reviewer/entrypoints/mcp_server.py`, `plugin/skills/summarize-subsystems/SKILL.md`,
MCP/store/skill tests.

- [ ] Add failing tests for legacy call, policy race, premature bootstrap,
  incomplete fragments, and successful atomic prune.
- [ ] Run focused tests and record expected failures.
- [ ] Implement expected token/hash API, service re-derivation, and locked store
  verification before deletion/state advance.
- [ ] Update FastMCP and skill failure semantics; run focused tests and commit.

### Task 3: Exact embedding CAS and ambiguous move self-heal

**Files:** `reviewer/index/summary_store.py`, `reviewer/mcp/service.py`,
`reviewer/services/summary_fragments.py`, focused unit/integration tests.

- [ ] Add failing backfill snapshot/concurrent text rewrite tests.
- [ ] Add failing two-candidate delta and atomic self-heal integration tests.
- [ ] Implement exact CAS/counting and ambiguity-as-pending behavior.
- [ ] Run focused tests and commit.

### Task 4: Documentation, payload, verification, deployment

**Files:** `README.md`, `CLAUDE.md`, design docs, both plugin manifests, report.

- [ ] Update layout/prune docs and MCP tool-count docstring.
- [ ] Resync plugin metadata/cachebuster and run installer/payload guards.
- [ ] Run focused suites, isolated SummaryStore integration, full pytest, full Ruff,
  and `git diff --check`.
- [ ] Request independent whole-wave review and fix all Critical/Important findings.
- [ ] Reinstall exact local plugin, verify one plugin/one reviewer MCP, and write
  `final-fix-report.md` with RED/GREEN and restore/dependency notes.
