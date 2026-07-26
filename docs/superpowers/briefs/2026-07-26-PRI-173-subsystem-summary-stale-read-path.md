# Brief — PRI-173 Сводки подсистем: поле stale в get_subsystem_summaries (read path)

https://ru.yougile.com/team/686c049c8af8/#PRI-173

## Task

- Store-first task data from reviewer store after successful board sync: ID-173 (alias PRI-173), status «Движок (reviewer CLI/MCP)».
- Add `stale: true | false | null` to every `get_subsystem_summaries` result so consumers do not treat an outdated architectural prior as current.
- Expose `source_hash` through the plural store queries; derive current cluster hashes on the read path and compare them with stored hashes.
- When the summary count exceeds `SUMMARY_TOPK_THRESHOLD`, return `stale: null` and do no extra cluster work; retain the single-`cluster_key` path.
- Teach ask and solve-task to downweight `stale: true`, avoid structural claims, and mark it `[stale]` in the latter's Subsystems section.

## Related work

- PRI-165 (done, PR #53) — reuse its skeleton-based `source_hash` and cluster/read-path conventions; do not regress its capped builder behavior.
- PRI-184 (backlog) — overlaps in freshness-warning intent, but is a plugin-only preflight guard; decide whether to close/retarget it after this MCP-level signal exists.

(dropped 15: PRI-175 is only a linked Constraints-tag task; remaining semantic matches concern retrieval, indexing, or prior rollout rather than this read-path contract.)

## Subsystems

- reviewer/index — summary persistence, hybrid store and freshness inputs; relevant to `source_hash` projection.
- reviewer/graph — clustering owns the structural source hash that read-path freshness must compare.
- reviewer/entrypoints — MCP tool contract is consumed by plugin skills.
- tests/index — store-layer and summary-store test conventions.
- tests/graph — clustering/source-hash behavior test coverage.

## Relevant code

- reviewer/mcp/service.py:944 — `list_subsystem_clusters` already builds current clusters and compares persisted hashes; extract/reuse this calculation without applying its rebuild cap semantics to consumer results.
- reviewer/mcp/service.py:1044 — `get_subsystem_summaries` has three response paths (single, ANN/top-k, all) and currently returns store values unchanged.
- reviewer/index/summary_store.py:105 — `get_summaries()` selects no `source_hash`; extend both SELECT and returned mapping.
- reviewer/index/summary_store.py:114 — `get_summary()` already returns `source_hash`, so single-summary behavior needs stale annotation, not a missing-column fix.
- reviewer/index/summary_store.py:129 — `search_summaries()` likewise omits `source_hash` in the ANN path.

(dropped 0: all retrieved current-code locations directly determine the implementation.)

## Test exemplars

- tests/mcp/test_subsystem_summaries.py:66 — existing MagicMock service/store round-trip is the fixture pattern for all/read-path stale assertions.
- tests/mcp/test_subsystem_summaries.py:91 — use its deliberately mismatched hash setup as the stale-true scenario.

(dropped 0: both surfaced test locations directly model this service contract.)

## Constraints / open questions

- [reviewer_store_after_sync] PRI-173 was read from the refreshed reviewer store; `criteria=[]`, and its description has no acceptance/criteria heading, so explicit acceptance criteria are a task-data gap.
- The current `main` implementation does **not** expose `stale`: plural store queries omit `source_hash`, and the consumer service returns their records unchanged; this task is not obsolete or already implemented.
- Preserve latency: query + count above the resolved top-k threshold must set `stale=null` without `list_base_members`/cluster derivation; verify all/no-query and `cluster_key` paths separately.
- The small-repo read calculation scans base members and derives clusters; reuse the same resolved depth/overrides as `list_subsystem_clusters`, fail soft if index/store access is unavailable, and do not introduce automatic rebuilds.
- Update both consumer skills consistently; stale summaries remain a prior only, never evidence for `path:line` claims.
- Resolve PRI-184 ownership after implementation to avoid two competing stale-warning mechanisms.

Собран на: mid / gpt-5.6-terra, режим: subagent
