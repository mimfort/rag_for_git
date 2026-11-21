# Brief — PRI-155 blast-radius: точность impact.py
https://ru.yougile.com/team/686c049c8af8/#PRI-155

## Task

- Store-first after sync: ID-155 (alias PRI-155); criteria are absent as a separate field, requirements are in the description.
- Keep the problem: a signature change must retain a same-file caller when that caller's own contract was not updated; path-level exclusion currently loses it.
- Keep the problem: a graph caller absent from the chunk store must never render as the misleading `path:0 | `.
- Rewrite the `extract_signature` portion: decorators are already supported in `reviewer/index/struct_diff.py`; any new diagnostic needs an explicit consumer/API rather than changing a silent `str | None` helper gratuitously.
- Acceptance tests: same-file changed vs unchanged caller, missing caller metadata, decorator regression; retain added/removed-symbol fail-soft behavior unless a separately specified impact policy requires them.

## Related work

- PRI-126 — reuse the base-vs-overlay signature gate and the three-layer `get_impact` wiring; refine its path-level caller suppression rather than add another impact path.
- PRI-158 — `extract_signature` was moved to `reviewer/index/struct_diff.py`; modify/test it there only if a concrete diagnostic contract is approved.

(dropped 6: PRI-145 concerns confidence wording, PRI-148 generic graph formatting, and the remaining semantic matches do not directly determine this engine change.)

## Subsystems

- reviewer/tools — PR-review tools own `get_impact`; this is the primary implementation surface.
- tests/tools — isolated fakes are the established unit-test pattern for impact and tool wiring.

## Relevant code

- reviewer/tools/impact.py:30 — `compute_impact` has only `changed_paths`; it must distinguish a caller node changed-with-contract-update from an unchanged caller in the same path.
- reviewer/tools/impact.py:54 — callers are currently filtered solely by path at line 55, which directly causes the same-file false negative.
- reviewer/tools/impact.py:58 — enrichment uses the existing overlay/base-aware store fetch and is the right seam for a typed/explicit unavailable-caller representation.
- reviewer/tools/impact.py:64 — the absent-node fallback creates `line=0, snippet=""`; either omit it with a counted reason or render a clear “metadata unavailable/out of index” marker.
- reviewer/tools/impact.py:73 — `format_impact` must preserve a truthful distinction between actionable caller data and incomplete graph/index data.
- reviewer/index/struct_diff.py:17 — canonical `extract_signature` already skips decorators/docstrings and is re-exported from `impact.py:7`; do not duplicate it.
- reviewer/tools/code_tools.py:149 — public tool contract delegates directly to `compute_impact`; avoid changing MCP wiring unless output semantics require it.

(dropped 1: no other retrieved code snippet directly informs this narrow engine correction; reviewer search returned no indexed snippets despite main drift=0, so paths were verified against the working tree.)

## Test exemplars

- tests/tools/test_impact.py:52 — decorator + async signature regression already exists; retain it, do not add a duplicate decorator-only feature test.
- tests/tools/test_impact.py:65 — model two cross-file callers through `_Graph`/`_Store`; extend this fixture for unchanged same-file vs genuinely updated same-file callers.
- tests/tools/test_impact.py:108 — existing test encodes the now-overbroad same-file exclusion; replace it with the revised node/contract-aware expectation.
- tests/tools/test_impact.py:120 — formatter test is the right place to require an explicit unavailable-node marker or intentional omission, never `:0 | `.
- tests/tools/test_impact.py:131 — keep end-to-end `StructuredTool` coverage after output semantics change.

(dropped 0: every surfaced impact test directly constrains the revised behavior.)

## Constraints / open questions

- Verdict: **rewrite**, not delete. The two engine defects are still present in current code and are useful for the global `rag-reviewer` plugin; the stated decorator gap is obsolete.
- Define “contract updated” before implementation: available inputs are `changed_node_ids`, base/overlay chunks, and signatures. A caller merely sharing a changed file is insufficient evidence; decide whether an unchanged caller body alone is enough to retain it, and how to handle a changed caller with an unchanged signature.
- Do not treat removed target symbols as the same issue: `compute_impact` deliberately skips missing base/overlay pairs at `impact.py:49`; expanding that policy needs a separate acceptance case because an old graph edge may be stale.
- `extract_signature` cannot explain `None` without an API change (e.g. result/reason or logging). Add it only if a downstream user-visible decision consumes the reason; otherwise preserve fail-soft behavior.
- Task-context has no linked-task detail. Similar-task list was deduplicated by canonical key and evaluated with rank only; it contained PRI-155 itself plus completed work.
- Retrieval gap: `search_codebase` and targeted test search returned “ничего не найдено” despite a fresh main index; the brief’s file citations were therefore validated with targeted local search, not claimed from reviewer snippets.

Собран на: mid tier, режим: subagent
