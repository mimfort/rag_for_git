# PRI-262 eye-check: does line-granular seeding make the context core meaningful?

**Verdict: PASSES the pre-registered ≥ 50 % threshold. 16/25 paths judged genuinely worth
reading = 64.0 %, against 41.8 % from PRI-261 and a bar of ≥ 50 %.**

Gate, sample and stopping rule were fixed in `eval/pri262_preregistration.md`, committed
(909e974) before the implementation existed and before any run.

## Method

Both sides measured at one `indexed_sha=a07405c7f11b029f30ae0be969fa36a009074ffb`, branch `dev`,
7657 chunks, with explicit `--branch dev`. The "before" side is the pre-implementation commit
(909e974) run from a git worktree; the "after" side is the same corpus after the seeding change.
Nothing else differs between the runs.

**Both sides were judged in a single pass, by one judge, against one written criterion** — this
matters, see the honesty note below.

Criterion for "meaningful": *would a developer doing this real task have needed to read this file
— and could they have?* The second clause is not decoration. A file created after the task merged
was unreadable at the time, so counting it as context is counting a file that did not exist.

## Result

| Task | before | after | before share | after share |
|---|---|---|---|---|
| PRI-227 | 0/6 | 0/0 | 0 % | undefined (no seeds) |
| PRI-236 | 0/5 | 0/0 | 0 % | undefined (empty denominator) |
| PRI-215 | 6/35 | 5/11 | 17 % | 45 % |
| PRI-221 | 9/32 | 8/10 | 28 % | 80 % |
| PRI-251 | 5/8 | 2/3 | 63 % | 67 % |
| PRI-249 | 3/13 | 1/1 | 23 % | 100 % |
| **Total** | **23/99** | **16/25** | **23.2 %** | **64.0 %** |

## The pre-registered falsifiable prediction held

**PRI-227 yields zero seeds.** Its diff is a docstring rename; every hunk masks to an identical
left and right block, so no hunk is significant and no seed is produced. All 6 of its previously
manufactured context paths are gone. Scenario 1 is fixed, independently of the aggregate.

PRI-236 is the sharper case. It still produces 7 seeds — the `check()` change is real code — but
its whole context core is now empty, because every name sounding on a changed line
(`_vector_to_list`, `check_vector_roundtrip`, `find_embeddings_by_hashes`) lives in
`index/store.py`, which is itself a changed file and is subtracted. That is precisely the outcome
PRI-261's eye-check argued for in prose: "the real dependency lives in the *changed* file and is
correctly excluded". The help-text edit inside `config_show()` no longer drags
`config/committed.py` and `config/layers.py` in, because those calls sit on untouched lines.

## Honesty note: why the "before" column reads 23.2 %, not 41.8 %

PRI-261 judged the same before-side paths at 41.8 %. This pass judges them lower, and the
difference is **the existence clause, not a change in the data**. PRI-261 counted the eight sibling
board adapters (`asana`, `clickup`, `github`, `kaiten`, `linear`, `trello`, `weeek`,
`yandex_tracker`) as "genuinely useful" context for PRI-215. Checked here:
`reviewer/tasks/boards/asana.py` was added **2026-07-25**, and PRI-215 merged **2026-07-24** — the
adapters did not exist when the task was done. The same check removes `config/deepmerge.py`
(added 2026-08-18) and `config/fetch_errors.py` (2026-08-10) from PRI-221's before-side credit,
both of which post-date its 2026-07-31 merge.

This is disclosed rather than smoothed over, because it cuts against a clean story: **the gate is
cleared on the after-side number (64.0 % ≥ 50 %, > 41.8 %), which is what criterion 1 asks for,
but the before/after *delta* in this table is larger than a like-for-like comparison with
PRI-261's published 41.8 % would give.** Both columns here are internally consistent with each
other, which is the comparison criterion 3 actually governs.

## The cost, stated plainly: 7 meaningful paths were lost

Precision is not free. Meaningful paths fell 23 → 16 while junk fell 76 → 9. The losses are real
and named:

- **PRI-251** lost `index/chunker.py`, `index/models.py` and `gitutil.py`. All three are genuine:
  `inherit.py` builds inheritance edges out of `chunk_python` output, and `build_with_scip` runs
  the indexer through a temporary worktree. They were dropped because the calls that reach them
  sit on lines the diff did not touch.
- **PRI-215** lost `tasks/graph.py`; **PRI-221** lost `services/branch.py`; **PRI-249** lost
  `services/cost_sidecar.py` and `vcs/base.py`.

The mechanism cannot see a dependency that the changed lines never name. That is the price of the
name filter, and it is the honest reading of criterion 4's noise floor: these are not ±1 wobbles,
they are systematic.

## Corpus-level effect (all 63 tasks, same two runs)

| | before | after |
|---|---|---|
| total context-core paths | 403 | 85 |
| median context-core size | 4 | 0 |
| tasks with a measured denominator | 42 | 31 |
| tasks with no context measurement | 5 | 16 |
| `core_recall_median` | 0.25 | 0.25 |
| `bulk_core_recall_median` | 0.1429 | 0.1429 |
| `precision_median` | 1.0 | 1.0 |

**Additivity (criterion 5) is confirmed empirically, not only by property test**: every pre-existing
number is byte-identical across the two runs.

The coverage cost is the number to watch: 11 more tasks now have an undefined denominator, and the
median context core is 0. The metric became much more precise and considerably narrower. Whether
that trade is acceptable is a decision about what the denominator is *for*, and it is not settled
by this eye-check.
