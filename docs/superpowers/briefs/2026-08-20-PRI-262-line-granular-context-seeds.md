# PRI-262 — Seed the context core from changed lines, not from the whole symbol

Собран на: 2026-08-20, indexed_sha=308b86bcbcb39c7e463c8d41ac218e2becf6484d, branch=dev (drift=35)
Источник задачи: reviewer store (после warm-up sync), ключ доски ID-316 / alias PRI-262, статус «Бэклог».

## Task

PRI-261 shipped `context-recall` as a second denominator for brief quality and closed with a
**negative result**: manual eye-check of 8 tasks judged 41/98 context-core paths genuinely worth
reading = **41.8 %** against a pre-registered gate of **≥ 50 %** (`eval/pri261_eye_check.md`,
`eval/replay_report.md` § «PRI-261 — отрицательный результат»).

PRI-262 is the **post-hoc hypothesis** for why: the traversal is seeded by the *whole enclosing
symbol* containing a diff hunk, not by the changed lines. It is a hypothesis requiring its own
acceptance, **not** an amendment to PRI-261's verdict.

## Acceptance criteria (from the board, verbatim in intent)

1. Meaningful-path share on manual eye-check **above 41.8 %** and not below a pre-registered
   threshold (proposed: keep ≥ 50 %). Threshold named in the journal **before** looking at data;
   never moved retroactively.
2. Eye-check sample must include **PRI-227, PRI-236, PRI-215, PRI-221** (old mechanism scored
   0/6, 0/5, 18/34, 6/30). Improvement must be visible on exactly those.
3. Single `indexed_sha`, commands with explicit `--branch dev`. (Without the flag the run silently
   uses the `main` index — 2139 chunks vs 7551. This defect already zeroed one PRI-261 run.)
4. Per-task deltas read against the measured harness noise floor: ±1 file pairwise, 6 of 62 tasks
   unstable between identical runs. Below that is not a signal.
5. **Additivity**: no existing number changes (core_recall, precision, aggregates). Verified by a
   property test on random inputs, as in PRI-261.
6. **Purity**: `reviewer/metrics/brief_quality/**` stays I/O-free, traversal injected;
   `eval/solve_task_metrics/` stays a re-export, not a second copy of the formula
   (guard: `tests/metrics/test_reexport_guard.py`).

## Where the defect lives

`eval/solve_task_metrics/context_seeds.py::_symbols_at` (85 lines total, read it whole):

```python
for chunk in chunks:
    for start, end in ranges:
        if chunk.start_line <= end and chunk.end_line >= start:
            hits.add(f"{path}#{chunk.symbol_fqn}")
```

Any chunk whose *range* intersects a hunk becomes a seed. Two consequences, both verified in this
session:

- **Comment/docstring/help-text-only hunks seed anyway.** Nothing inspects hunk content.
- **Nested chunks both fire.** `reviewer/index/chunker.py::chunk_python` emits the class chunk
  *and* each method chunk (`visit` recurses into the body and appends at every level). A hunk
  inside one method of `MCPReviewService` seeds `service.py#MCPReviewService.foo` **and**
  `service.py#MCPReviewService`.

## Correction to the task's framing (important — verify in brainstorming, do not take on faith)

The board text presents both failure scenarios as fixable by seed granularity. **They are not the
same defect**, and `eval/pri261_eye_check.md` already says so more precisely than the ticket:

- **Scenario 1 (comment-only edits — PRI-227, PRI-236 half).** Genuinely a seeding defect.
  Filtering hunks that change no executable code removes the seed entirely. Item 1 of «Что сделать»
  fixes this.
- **Scenario 2 (god-modules — PRI-221, PRI-236 other half, PRI-215 drain).** **Narrowing the seed
  to changed lines does not fix this**, because the changed lines *are* real code. The eye-check
  pins it exactly: in PRI-236 the `--path` help-text edit sits inside `config_show()`, whose
  **untouched pre-existing body** calls `CommittedLayerFetcher`/`resolve_policy_data`; `check()`
  gained one relevant call but its **untouched** neighbouring `GraphStore()` check also seeded
  `graph/store.py`. The junk arrives through **edges originating on lines that were never changed**.

  Fixing scenario 2 therefore needs **edge-level filtering by call-site line**, not seed-symbol
  narrowing. Blocking constraint: `reviewer/graph/store.py::outgoing_neighbors` returns
  `{n.id}` only — the Neo4j `CALLS`/`IMPLEMENTS` edges carry **no line information**, and adding it
  is a graph-schema change touching `graph/builder.py` + `graph/scip.py` + `graph/store.py`.
  A cheaper route that preserves purity: `context_seeds` already parses the merge-commit source
  with tree-sitter, so it can extract *callee names appearing on changed lines* and intersect the
  graph neighbourhood by name/path locally. Weigh both in brainstorming.

  The board's item 3 already anticipates this ("they are fixed by different mechanisms and one may
  work without the other") — but items 1–2 only describe the mechanism for scenario 1.

## Reuse, do not rewrite

- `reviewer/metrics/brief_quality/context_core.py` — pure derivation, `Traversal` injected. Untouched.
- `reviewer/graph/store.py::outgoing_neighbors` — directed one-hop CALLS/IMPLEMENTS.
- `eval/solve_task_metrics/context_seeds.py` — seed extraction. **The change lives here.**
- `eval/solve_task_metrics/replay.py:168-170` — the call site (`collect_seeds` → `derive_context_core`).
- Columns `ctx`/`ctx_hit`/`ctx_recall`/`u_prec` already exist in both reports.
- Tests: `tests/eval/test_context_seeds.py`, `tests/metrics/test_context_core.py`,
  `tests/metrics/test_reexport_guard.py`, `tests/eval/test_replay.py`.

## Constraints / open questions

- **Preflight drift = 35.** The base index sits at `308b86b` — the exact sha PRI-261 measured at.
  Re-indexing would move it and break comparability with the 41.8 % baseline. Decide explicitly:
  measure at `308b86b` (comparable, stale) or re-index and re-establish the "before" side. Do not
  let this happen by accident.
- **`--branch dev` is mandatory** on every replay command (criterion 3).
- **Non-Python blind spot is out of scope** (stated in the ticket): `classify.is_core_production_path`
  counts `plugin/*` non-`.md` (incl. JSON) as core, while `context_seeds._symbols_at` only sees
  Python via `chunk_python`. For a task whose core is entirely non-Python the denominator is
  **undefined, not empty** — 3 of 62 tasks (PRI-177, PRI-237, PRI-243).
- **Known debt noticed there, also out of scope unless brainstorming pulls it in**: the metric has
  no `context_retrieval_failed` status mirroring `STATUS_RETRIEVAL_FAILED`, so a systemic traversal
  failure reads as "empty context core". PRI-261 checked this by hand once; nobody will remember next time.
- **Pre-register the threshold before running anything.** Criterion 1 is worthless if the number is
  written after the data is seen.
- **Two negatives = stop.** Item 6 of «Что сделать»: if the gate fails again, close with a second
  negative result and do not return to the idea without a new mechanism.

## Related work

PRI-261 (parent, negative, PR #217) · PRI-227 / PRI-236 / PRI-215 / PRI-221 (the failure cases) ·
PRI-177 / PRI-237 / PRI-243 (non-Python blind spot) · PRI-257/258 (precedent: a zero delta may be
budget mechanics, not the lever — check the candidate reaches the output before declaring a null result).
