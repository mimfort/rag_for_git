# PRI-261 eye-check: is the context core meaningful, or plausible-looking junk?

**Verdict: FAILS the pre-registered ≥50% threshold. 41/98 paths judged genuinely
worth reading = 41.8%.**

Measured run: `eval/replay_history.jsonl` entry `taken_at=2026-08-19T11:16:25.198940+00:00`
(matches `eval/replay_report.md`'s current header), `indexed_sha=308b86bcbcb39c7e463c8d41ac218e2becf6484d`,
branch `dev`, `context_recall_median=0.25`, 41 tasks measured.

## Method

Selected 8 tasks from that run's `tasks[]` for spread in context-core size
(1 → 34 paths) and `core_recall` (0.64 → 1.0, both low and high represented):
**PRI-172, PRI-227, PRI-251, PRI-218, PRI-236, PRI-249, PRI-215, PRI-221**.
Selection rule: sort by `len(context_core_paths)`, take the two smallest, two
largest, and four spread across the middle; among those, prefer a mix of
`core_recall` extremes over prior familiarity.

For each task: read the real brief (`docs/superpowers/briefs/`), found its real
merge commit(s) via `git log --merges --all --grep=<KEY> -i`, ran
`git diff <merge>^1 <merge> -- <path>` on the actually-touched production files to
see exactly which functions/lines changed, then traced each `context_core_paths`
entry to the specific CALLS/IMPLEMENTS edge that put it there and judged whether a
developer doing that real task would genuinely have needed to read it.

## Per-task counts

| Task | meaningful / total | core_recall | notes |
|---|---|---|---|
| PRI-172 | 1/1 | — | real import edge, real sibling hook |
| PRI-227 | 0/6 | — | docstring-only diff hunk, zero logic changed |
| PRI-251 | 6/8 | 0.64 | real feature edges good; 2 incidental |
| PRI-218 | 1/1 | — | real single import edge |
| PRI-236 | 0/5 | 1.0 | help-text-only edit + unrelated existing calls |
| PRI-249 | 9/13 | 1.0 | real publish-pipeline edges mostly good |
| PRI-215 | 18/34 | — | sibling adapters genuinely useful, plumbing junk |
| PRI-221 | 6/30 | — | god-module (mcp/service.py, cli.py) contamination |
| **Total** | **41/98 = 41.8%** | | **below the 50% bar** |

## The dominant failure mode: chunk-granularity blind spot on large multi-purpose functions

The traversal seeds from whichever *whole function* the diff hunk falls inside,
not from the changed lines themselves. Two ways this manufactures junk:

1. **A docstring/help-text-only edit still pulls in the touched function's entire
   unrelated call graph.** PRI-227 (0/6): the "diff" is renaming a skill invocation
   name inside comments in `mcp_server.py` — confirmed via
   `git diff b254bb6..43fdb65 -- reviewer/entrypoints/mcp_server.py reviewer/mcp/service.py`,
   zero logic changed. PRI-236 (0/5) is the same shape twice over: the `--path`
   help-text edit sits inside `config_show()`, whose *existing* body (unrelated to
   the edit) calls `CommittedLayerFetcher`/`resolve_policy_data` → seeds
   `config/committed.py` and `config/layers.py`; separately, `check()` gained a
   genuinely-relevant `store.check_vector_roundtrip()` call, but that function
   *also* has a pre-existing, untouched `GraphStore()` connectivity check a few
   lines up → seeds `graph/store.py`. None of the 5 context-core paths for PRI-236
   were anything the implementer needed to read; the real dependency
   (`_vector_to_list` reused inside `find_embeddings_by_hashes`) lives in the
   *changed* file (`index/store.py`) and is correctly excluded by the
   changed-core subtraction — but that subtraction only protects files that were
   themselves edited, not files reached through an untouched sibling branch of
   the same function.

2. **God-module functions pull in everything the class imports, not just what the
   edit's logic touches.** `reviewer/mcp/service.py` (`MCPReviewService`, ~1500
   lines) and `reviewer/entrypoints/cli.py` are both single files serving dozens
   of unrelated tools/commands. PRI-221 (6/30): the real work is
   `_resolve_policy`/`_resolve_repo_branch` picking up the new home-config layer
   (genuinely pulls in `policy/policy.py`, `config/committed.py`,
   `config/branches.py`, `config/fetch_errors.py`, `config/task_board.py`,
   `gitutil.py` — judged meaningful) — but the same merge's diff hunks also touch
   four unrelated tool methods in the same file
   (`search_codebase`/`related_symbols`/`list_subsystem_clusters`/`get_subsystem_summaries`,
   confirmed via the hunk headers at lines 832/947/1100/1534) with what look like
   incidental one-line context changes, and those methods' *own* unrelated call
   graphs (`retrieval/retriever.py`, `graph/summaries.py`, `index/summary_store.py`,
   `index/store.py`, `index/embeddings.py`, `index/freshness.py`,
   `services/graph_sync.py`, `agent/state.py`, `services/risk_paths.py`, etc.) get
   pulled into the same task's context core. None of that plumbing has anything to
   do with a home-config layer.

## Cross-task recurrence — the "popular callee" check

No single path recurred in 4+ of the 8 sampled tasks (the report's own trigger
threshold), but several sit right below it at exactly 3/8, and the pattern is
worse than the raw count suggests because **every one of them was judged junk
every single time it appeared**:

- `reviewer/app.py` — PRI-251, PRI-249, PRI-221, all 3 junk. Always the same
  generic `from reviewer.app import Components` dependency-injection import,
  never topically related to any of the three tasks.
- `reviewer/config/settings.py` — PRI-236, PRI-249, PRI-221, all 3 junk. Same
  shape: `Settings()` construction, incidental to whatever the touched function
  actually does.
- `reviewer/policy/context_limits.py` — PRI-251, PRI-215, PRI-221, all 3 junk.
  Pulled in only via `_resolve_context_limits`, a helper unrelated to any of the
  three tasks' subjects.
- `reviewer/graph/store.py` — PRI-236, PRI-215, PRI-221, all 3 junk. Same
  `GraphStore()` connectivity/import pattern each time.
- `reviewer/vcs/base.py` — PRI-249, PRI-215, PRI-221, all 3 junk. Type-only
  imports (`ChangedFile`, `PullRequest`), never load-bearing for the task.

Two paths recurred at 3/8 but split — `reviewer/gitutil.py` (meaningful twice,
junk once) and `reviewer/retrieval/retriever.py` / `reviewer/services/review_service.py`
(meaningful once each, junk twice) — these are genuinely task-dependent rather
than pure noise, so they don't indict the metric the way the five above do. Still,
five paths that are junk 100% of the times they appear, sitting one occurrence
below the report's own alarm threshold, is exactly the failure mode the threshold
was designed to catch, and the 4+ trigger would have missed it in this sample by
one task.

## Concrete examples

**Genuinely meaningful (would have to read it):**
- `reviewer/graph/scip.py` (PRI-251) — the brief cites `scip.py:42-51` directly;
  the task's own subject is the `local N`-scope resolution this file implements.
- `plugin/hooks/brief_cost.py` (PRI-172) — the new guard hook's own diff imports
  and calls this module by name (`import brief_cost`); it's the pattern the new
  code was modeled on and wired to.
- `reviewer/update_lifecycle.py` (PRI-218) — the one touched CLI command imports
  it directly (`from reviewer.update_lifecycle import (...)`) to do the actual
  update work the task adds a launcher entry for.
- `reviewer/tasks/boards/linear.py` (PRI-215) — implementing a shared
  `TaskBoardProvider` Protocol for a new registry genuinely requires reading at
  least one existing sibling adapter as a worked example.

**Junk (plausible-looking, not actually needed):**
- `reviewer/app.py` (PRI-251/249/221) — generic `Components` DI import, unrelated
  to any of the three tasks' actual subject matter, present purely because it's
  imported near whatever function the diff hunk happened to land in.
- `reviewer/graph/store.py` (PRI-236) — `check()`'s pre-existing, untouched Neo4j
  ping sits a few lines above the genuinely new pgvector check in the same
  function; nothing about `GraphStore` needed reading for this task.
- `reviewer/config/committed.py` / `reviewer/config/layers.py` (PRI-236) — reached
  only because a cosmetic `--path` help-text edit shares a chunk with
  `config_show()`'s unrelated, untouched body.
- `reviewer/policy/context_limits.py` (PRI-251/215/221) — an unrelated helper
  incidental to three different god-module functions in three different tasks,
  never once the actual subject of any of them.

## Bottom line

At the pre-registered threshold, this fails: 41.8% of sampled context-core paths
are genuinely worth reading, not ≥50%. The failure is not random noise — it has an
identifiable, structural cause (function-level chunk granularity conflating "this
symbol's diff hunk" with "everything this symbol's containing function happens to
call," which is especially punishing on large multi-purpose files like
`mcp/service.py` and `cli.py`, and on comment/help-text-only edits). That's a
useful, actionable negative result, not just noise: a coarser seed (only symbols
whose *body* actually changed, not the whole enclosing chunk) or a smaller
neighbourhood on god-module files would likely move the number, but as measured,
today, the metric does not clear its own bar.
