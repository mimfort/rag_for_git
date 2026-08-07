# PRI-225 Task Board Retention Filter — Design

**Status:** Approved in brainstorming on 2026-07-31
**Task:** [PRI-225](https://ru.yougile.com/team/686c049c8af8/#PRI-225)
**Brief:** `docs/superpowers/briefs/2026-07-31-PRI-225-task-board-retention-filter.md`

## Goal

Add a generic, per-repository retention policy for task-board synchronization. The policy must
exclude old or explicitly archived tasks before expensive normalization, support safe provider API
pushdown, preserve current behavior when absent, and safely reconcile cursor and purge state when
the policy changes.

## Decisions

- `max_age_days` is measured from the task's last modification time.
- The age boundary is inclusive: a task updated exactly at the cutoff remains eligible.
- Archive and terminal states are separate. `include_archived: false` excludes only archive.
- An unknown archive state remains eligible and produces one deduplicated provider warning.
- Existing `keep_with_prs` behavior applies to retention-driven purge.
- Filtering has one canonical implementation in the sync domain; provider filtering is an optional,
  semantically equivalent optimization.
- Different policies are isolated for different `task_board.project` values. Repositories pointing
  to the same project intentionally share the existing global task corpus.

## Non-Goals

- Repo-specific task-store or task-graph views for repositories sharing one board project.
- An `include_terminal` setting or exclusion of done/completed tasks.
- Mandatory API pushdown for every provider.
- Provider-specific retention keys under `task_board.options`.
- Changing the default `purge_orphaned=false` behavior.
- Changing task retrieval semantics outside the effects of synchronized and purged data.

## Configuration

The effective policy accepts a generic sibling of provider `options`:

```yaml
task_board:
  type: yougile
  project: PRI
  key_pattern: 'PRI-\d+'
  options: {}
  sync_filter:
    max_age_days: 180
    include_archived: false
```

The normalized model is:

```python
@dataclass(frozen=True)
class TaskSyncFilter:
    max_age_days: int | None = None
    include_archived: bool = True


@dataclass(frozen=True)
class TaskBoardConfig:
    # Existing fields omitted.
    sync_filter: TaskSyncFilter | None = None
```

Rules:

- An absent `sync_filter` means no retention filtering and preserves current behavior.
- An empty `sync_filter` is retained by config round-trip but has no active restrictions, uses the
  legacy integer cursor, and reports `filter_applied=false`.
- `max_age_days` must be an integer greater than or equal to 1; booleans are rejected as integers.
- `include_archived` must be a boolean.
- `TaskBoardConfig.as_dict()` emits `sync_filter` only when the block was present.
- Recursive credential detection covers `sync_filter` as part of the complete `task_board` mapping.
- `sync_filter` is never copied into provider `options`.
- Existing effective-policy precedence is unchanged: global home, committed `.review.yml`, then home
  per-repo, with top-level replacement of the complete `task_board` value.

Because policy layers replace `task_board` as one top-level value, `configure-review` must preserve
all sibling board fields when editing only `sync_filter`.

## Domain Model

`RawTask` exposes lifecycle metadata independently from normalized task content:

```python
@dataclass
class RawTask:
    # Existing identity/content fields omitted.
    timestamp: int | None
    archived: bool | None = None
    terminal: bool | None = None
```

`timestamp` remains epoch milliseconds and means last modification time; `None` means the provider
cannot determine it. Providers map only native,
documented lifecycle signals:

- `archived=True/False` only when the provider can identify archive exactly.
- `archived=None` when the API cannot identify archive or the field is absent.
- `terminal` captures native done/completed/terminal state but is not consulted by this filter.
- Existing provider-specific payload remains available through `provider_data` where needed.

The current internal `completed` field is replaced by `terminal`; `RawTask` is not persisted, so no
serialized-data migration is required. All providers and tests move together to the canonical field.

## Filter Semantics

A pure classifier receives `TaskSyncFilter`, `RawTask`, and one provider-run `now_ms` value. It
returns one of `eligible`, `filtered_by_age`, or `filtered_archived`.

Classification order is deterministic:

1. If `max_age_days` is set and `raw.timestamp` is known and is less than
   `now_ms - max_age_days * 86_400_000`, return
   `filtered_by_age`.
2. If `include_archived` is false and `raw.archived is True`, return `filtered_archived`.
3. Otherwise return `eligible`.

Therefore a task matching both restrictions is counted only as `filtered_by_age`. A task at the
exact age cutoff remains eligible. When an age restriction is active, `timestamp=None` is fail-open:
the row continues to archive classification, increments `age_unknown`, warns once per provider, and
uses full normalization because watermark ordering cannot classify it as unchanged. `archived=None`
does not itself filter the row and increments a separate `archive_unknown` diagnostic count when the
row was not already filtered by age. The clock is injected so boundary tests are deterministic.

## Provider Contract And Pushdown

Providers receive the generic policy as a listing hint but do not own its semantics:

```python
@dataclass
class TaskListingStats:
    filtered_by_age: int = 0
    filtered_archived: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class TaskListing:
    rows: Iterable[RawTask]
    stats: TaskListingStats


class TaskBoardProvider(Protocol):
    def iter_raw(
        self,
        board: str | None,
        limit: int | None,
        *,
        sync_filter: TaskSyncFilter | None = None,
        now_ms: int | None = None,
    ) -> TaskListing: ...
```

`TaskListing.stats` is populated as the stream is consumed. A provider may omit rows through API or
client-side listing pushdown only when it can preserve the canonical classification order and
return exact, mutually exclusive reason counts. Otherwise it returns all listed rows and zero
provider-side filter counts; `SyncService` performs local filtering.

Provider retention pushdown is disabled whenever `limit` is set. Partial sync therefore preserves
the existing meaning of `limit` as a raw-enumeration cap instead of changing it to an eligible-row
cap on providers with server-side filtering.

`SyncService` reclassifies every yielded row even after pushdown. This catches adapter mistakes and
keeps provider optimization from becoming policy. Provider-side and local counts are added after
the stream completes.

For YouGile, the reference adapter maps an exact native archive flag when the listing payload
provides one. If the API does not expose archive exactly, it returns `archived=None`; local filtering
keeps the task and emits one warning for the YouGile run. Age/archive pushdown is added only where
the actual listing endpoint can satisfy the exact-count contract.

## MCP And Skill Configuration Flow

The current client-side `sync-tasks` skill cannot see home per-repo policy. `sync_board` therefore
gains a server-resolved repository mode rather than asking the client to reconstruct effective
policy:

```python
sync_board(
    repo: str | None = None,
    branch: str | None = None,
    board: str | None = None,
    board_type: str | None = None,
    provider_options: Mapping[str, JsonValue] | None = None,
    sync_filter: Mapping[str, JsonValue] | None = None,
    limit: int | None = None,
    purge_orphaned: bool = False,
    keep_with_prs: bool = True,
    force_renormalize: bool = False,
) -> dict
```

There are two mutually exclusive modes:

- Repo mode: `repo` is set. The server resolves branch through the tracked-branch allowlist, calls
  existing layered `_resolve_policy`, and takes type, project, options, and filter from the effective
  `task_board`. Supplying `board`, `board_type`, `provider_options`, or `sync_filter` at the same time
  is a configuration error.
- Explicit mode: `repo` is absent. Existing arguments and deploy-wide fallback keep their current
  behavior. An explicit generic `sync_filter` is accepted separately from `provider_options`.

In repo mode, effective-policy failure returns an error before board I/O. It never falls back to an
unfiltered deploy config because that could enumerate or purge the wrong corpus. An absent or
disabled effective `task_board` returns the existing board-not-configured style error.

The `sync-tasks` skill resolves canonical repo and branch, then calls repo mode with only operation
flags. It no longer treats local `.review.yml` plus deploy fallback as the complete effective policy.
Other skills can retain their existing config flow unless their operation also needs home-layer
retention semantics.

## Sync Flow

For each selected provider, `SyncService` performs these steps:

1. Fix one `now_ms` for filtering and old/new-policy comparison.
2. Read and parse cursor state.
3. Ask the provider for `TaskListing` using the new filter as an optional hint.
4. For every yielded `RawTask`, classify it with the new filter before adding active keys or calling
   `normalize_meta`/`normalize`.
5. Count filtered rows and skip all normalization for them.
6. Add eligible keys to the purge active set before normalization, so a normalization failure does
   not delete an existing task.
7. Choose full normalize, metadata refresh, or unchanged handling from timestamp, force mode, and
   filter-transition backfill rules.
8. Index changed tasks and refresh unchanged metadata through existing services.
9. After a successful unlimited enumeration, optionally run project-scoped purge and persist cursor
   state.

Filtered tasks do not reach `normalize`, `normalize_meta`, indexing, or graph upsert. Without purge,
previously indexed filtered tasks remain until an explicit full purge; this preserves the existing
opt-in deletion model. With full scoped purge, the eligible active set removes previously stored
filtered tasks only inside the selected project.

## Cursor State And Filter Changes

The cursor ref remains `tasks:{board_type}:{project-or-*}`. This preserves the existing board/project
scope and supports independent policy for repositories using different projects.

Legacy state is an integer string:

```text
1722345678000
```

Filtered state is versioned JSON:

```json
{
  "version": 1,
  "watermark": 1722345678000,
  "sync_filter": {"include_archived": false, "max_age_days": 180},
  "filter_fingerprint": "sha256:..."
}
```

The fingerprint is SHA-256 over canonical sorted JSON containing the filter and filter-semantics
version. A future semantic change increments that version and intentionally triggers re-evaluation.

Compatibility and transition rules:

- Legacy integer reads as its watermark with old filter `None`.
- A successful no-filter run with no prior filtered state continues writing the same integer format.
- A successful run with at least one active restriction writes JSON.
- Removing a filter reads the old JSON for transition backfill, then writes integer state after the
  successful full run.
- `limit` prevents both watermark advancement and filter-state/fingerprint changes.
- Invalid JSON produces a warning and uses watermark `0` plus unknown old filter, causing a safe full
  backfill of all eligible rows.
- Cursor write failure leaves completed indexing intact and returns a warning; the next run safely
  repeats transition work.

On each row, old and new filters are evaluated using the same `now_ms`:

- Old excluded, new eligible: full `normalize`, even when `timestamp <= watermark`.
- New excluded: no normalization.
- Eligible under both and below watermark: existing `normalize_meta` path.
- Above watermark or `force_renormalize=true`: existing full normalize path.
- Unknown timestamp with an active age restriction: full normalize because unchanged status cannot
  be established safely.
- Unknown/corrupt old filter: full normalize every row eligible under the current filter.

This handles larger `max_age_days`, `include_archived: false → true`, and filter removal without
requiring a blanket re-embed. Existing content-hash dedup keeps full normalization from causing
unnecessary embeddings.

## Purge Semantics

Purge runs only after a successful unlimited listing, as today.

- The active set contains only eligible task keys.
- The existing `project=board` scope is preserved for repo-scoped synchronization.
- `keep_with_prs=true` protects PR-linked tasks, including tasks excluded by retention.
- `keep_with_prs=false` allows those tasks to be removed.
- `limit` always disables purge and cursor advancement.
- Unknown archive rows remain eligible, so uncertainty cannot cause deletion.
- Provider/listing failure skips cursor persistence and purge for the incomplete scope.

Repositories sharing the same `task_board.project` operate on the same task corpus. Different
retention views for the same project require a future repo dimension in store and graph and are not
part of PRI-225.

## Summary And Observability

Each provider and the aggregate response include:

```json
{
  "enumerated": 120,
  "eligible": 75,
  "filtered_by_age": 40,
  "filtered_archived": 5,
  "age_unknown": 1,
  "archive_unknown": 3,
  "filter_applied": true,
  "filter_fingerprint": "sha256:...",
  "filter_source": "home:repos/mimfort/rag_for_git.yml"
}
```

For a completed exact listing:

```text
enumerated = eligible + filtered_by_age + filtered_archived
```

`age_unknown` is a diagnostic count for rows whose age could not be evaluated; such a row may still
be filtered by archive. `archive_unknown` counts rows whose archive state could not be evaluated and
that were not already filtered by age. Existing changed/embedded/refreshed/unchanged/failed, cursor,
warning, purge, and `by_board` fields remain. No-filter runs add zero/default retention fields but
preserve prior indexing and cursor behavior.

`filter_applied` is true only when `max_age_days` is set or `include_archived` is false. In repo mode,
`filter_source` comes from layered policy provenance; in explicit mode it is `explicit`.

Provider warnings, normalization failures, policy errors, and cursor errors continue through the
existing secret-sanitization path. Archive uncertainty is deduplicated to one warning per provider
run rather than one warning per row.

## Failure Handling

- Invalid `sync_filter`: return a configuration error before board I/O.
- Effective repo policy unavailable: return an error; do not use an unfiltered fallback.
- Provider cannot push down: enumerate and filter locally without failing the run.
- Provider cannot identify archive: keep unknown rows and warn once.
- Provider cannot identify update time while age filtering is active: do not age-filter the row,
  force full normalization, and warn once.
- Normalize/metadata-refresh failure: retain the eligible key in the active set and report the
  sanitized failure.
- Listing failure or incomplete enumeration: do not purge or persist cursor/filter state.
- Cursor parse failure: warn and full-backfill eligible rows from watermark zero.
- Cursor write failure: keep indexed results, warn, and allow idempotent retry.
- Neo4j unavailable: retain existing fail-soft store behavior and graph warnings.

## Configure-Review, Sync-Tasks And Documentation

`configure-review` adds `sync_filter` to its generic board shape and asks separately for:

- Maximum age in days, or no age limit.
- Whether archived tasks are included.

It keeps home per-repo as the recommended target, preserves unrelated `task_board` fields and
comments, never writes credentials, and explains that shared project values share one corpus.

`sync-tasks` calls server repo mode and reports effective source, fingerprint, aggregate counters,
`by_board`, and warnings in Russian. It remains a thin trigger and never enumerates task text.

Board-provider documentation records for each provider whether archive is exact and whether
age/archive pushdown with exact counts is supported. README/config examples describe defaults,
purge interaction, shared-project scope, and filter-change backfill.

Changes under `plugin/skills/` require regeneration and verification of the Codex plugin manifest
using the repository's existing manifest script.

## Test Strategy

### Config And Layers

- Normalize absent, empty, age-only, archive-only, and full filters.
- Reject zero/negative/non-integer age and non-boolean archive values.
- Reject nested registered credential names without echoing values.
- Assert immutable normalization and `as_dict()` round-trip.
- Resolve two home per-repo files with different projects/filters and verify independent sources.
- Verify committed/home precedence and preservation by configure-review edits.

### Pure Filter And Cursor

- Test one millisecond below, exactly at, and one millisecond above the age cutoff.
- Test age-before-archive single-reason classification.
- Test archive true/false/unknown and the unknown diagnostic count.
- Test unknown update time, fail-open classification, and forced full normalization.
- Test stable canonical fingerprints and semantics-version changes.
- Parse/write legacy integer and filtered JSON states.
- Test add, tighten, loosen, remove, corrupt, and partial filter-state transitions.

### SyncService

- No-filter run keeps integer cursor and existing indexing behavior.
- Filtered rows never call normalize or normalize_meta.
- Provider and local exact counts merge into the aggregate invariant.
- Below-watermark rows newly admitted by a looser filter use full normalize.
- Rows eligible under old and new filters use metadata refresh below watermark.
- Natural aging excludes rows without a fingerprint change.
- `force_renormalize` still controls eligible rows.
- `limit` disables purge and all cursor/filter-state writes.
- `limit` also disables provider pushdown and retains the raw-enumeration cap.
- Normalization failure keeps the eligible key active.
- Scoped purge receives eligible keys, project, and both `keep_with_prs` values.

### Provider Contract And YouGile

- Contract adapters provide stable update timestamps and canonical lifecycle fields.
- Pushdown is accepted only with exact reason counts and canonical ordering.
- Adapters without exact pushdown return all rows for local filtering.
- YouGile maps exact native archive metadata when available.
- Missing YouGile archive metadata produces `None`, eligibility, and one warning.
- YouGile old/archive rows do not reach expensive normalization.

### MCP, Skills And Docs

- Repo mode resolves effective home/committed policy and threads a separate typed filter.
- Repo mode rejects mixed explicit board arguments.
- Policy resolution failure never falls back to unfiltered sync.
- Legacy explicit calls remain compatible and may pass a generic filter separately.
- Summary and `by_board` expose all retention counters/source/fingerprint.
- Skill text uses repo/branch mode and configure-review emits the documented shape.
- Provider docs and root examples remain synchronized with the registry contract.
- Ruff, focused pytest suites, full unit suite, and Codex manifest check pass before completion.

## Acceptance Mapping

1. No filter: legacy integer cursor, indexing, limit, and purge behavior remain unchanged.
2. Old/archive rows: classified before normalization and absent from new indexing work.
3. Per-repo policy: server resolves effective layers; different projects have independent cursors
   and scoped corpora.
4. Full purge: eligible keys remove previously stored excluded rows only in the selected project,
   subject to explicit `keep_with_prs` behavior.
5. Partial sync: no purge and no cursor/filter-state advancement.
6. Looser filter: old/new policy comparison backfills newly eligible rows below watermark.
7. Summary: exact eligible/age/archive counts appear at aggregate and per-board levels.
8. Reference coverage: generic provider contract and YouGile tests cover age, archive uncertainty,
   filter transitions, and partial sync.
9. User-facing configuration: configure-review, sync-tasks, README, provider docs, and plugin
   manifests reflect the feature.
