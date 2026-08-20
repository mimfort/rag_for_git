# PRI-262 pre-registration

**Written and committed BEFORE any measurement run, before the implementation exists, and
before any data from the new mechanism has been seen.** Criterion 1 of the task requires the
threshold to be named up front and never moved retroactively. This file is that record.

## Hypothesis under test

PRI-261 closed with a negative result: 41/98 context-core paths judged genuinely worth reading
= 41.8 %, against a pre-registered bar of ≥ 50 % (`eval/pri261_eye_check.md`).

PRI-262 tests the post-hoc hypothesis that the cause is **seeding granularity**: the traversal is
seeded by the whole symbol enclosing a diff hunk rather than by the changed lines, so

1. hunks that change no executable code (comments, docstrings, help text) still seed, and
2. edges originating on *untouched* lines of a touched function still contribute.

This is a hypothesis with its own acceptance, **not** an amendment to PRI-261's verdict.

## Pre-registered gate

**Bar: the share of context-core paths judged genuinely worth reading must be ≥ 50 %,
and must exceed 41.8 %.**

The bar is deliberately held at PRI-261's value rather than adjusted for the harder sample
below. Note for the record, so it cannot later be read as goalpost-moving in either direction:
the sample is enriched with the tasks where the old mechanism performed *worst*, so ≥ 50 % on
this sample is a strictly harder test than ≥ 50 % was on PRI-261's sample. We accept that.

## Pre-registered sample

Six tasks. Four are mandated by the task's criterion 2 (the cases where the old mechanism
failed); two are carried over from PRI-261's sample as middle-of-range real-signal controls.

| Task | Old mechanism | Role |
|---|---|---|
| PRI-227 | 0/6 | mandated — docstring-only diff |
| PRI-236 | 0/5 | mandated — help-text-only edit + untouched sibling calls |
| PRI-215 | 18/34 | mandated — plumbing drain |
| PRI-221 | 6/30 | mandated — god-module contamination |
| PRI-251 | 6/8 | control — real feature edges |
| PRI-249 | 9/13 | control — real publish-pipeline edges |

PRI-172 (1/1) and PRI-218 (1/1) are deliberately **dropped**: single-path unanimous tasks carry
almost no information and inflate both sides of the ratio equally.

Both the "before" and the "after" side are eye-checked, at the same `indexed_sha`, so the
comparison is a true A/B of the seeding code and nothing else.

## Pre-registered falsifiable prediction (non-gating)

**PRI-227 must yield ZERO seeds.** Its diff is a docstring rename with no logic changed, so a
mechanism that correctly filters non-executable hunks cannot produce a seed for it. This is not
part of the gate; it is a direct test of whether scenario 1 was actually fixed, independent of
what the aggregate does. If PRI-227 still produces seeds, scenario 1 is not fixed regardless of
the headline number.

## Pre-registered measurement conditions

- One `indexed_sha` for both sides. The index is re-built on `dev` at HEAD for this task; the
  resulting sha is recorded in the report, and **both** runs use it.
- Every replay command carries an explicit `--branch dev`. Without the flag the run silently
  uses the `main` index (2139 chunks vs 7551) — a defect that already zeroed one PRI-261 run.
- Per-task path deltas below the measured harness noise floor (±1 file pairwise; 6 of 62 tasks
  unstable between identical runs) are **not** treated as signal.

## Pre-registered stopping rule

If the gate fails, PRI-262 closes as a **second negative result** with the number recorded, and
the idea is not revisited without a genuinely new mechanism. Two consecutive negative measurements
mean the problem is not seeding granularity.
