# PRI-155 Impact Evidence Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `get_impact` return grounded caller candidates from both changed and unchanged PR files while reporting missing index metadata as an explicit coverage gap.

**Architecture:** Keep the existing base-vs-overlay target-signature gate, but remove the path-level caller filter. Enrich every resolved caller with PR scope, keep unresolved graph IDs outside `CallerRef`, then teach the global plugin to verify both scopes through source and diff before producing a finding.

**Tech Stack:** Python 3.11+, dataclasses, existing graph/chunk-store interfaces, FastMCP, Markdown skill prompts, pytest, ruff.

## Global Constraints

- Preserve `extract_signature(str) -> str | None`; decorated declarations are already supported by PRI-158.
- Preserve the current target gate: both base and overlay chunks exist, both signatures parse, and the signatures differ.
- Added and removed target symbols remain outside scope.
- Missing caller metadata is a coverage gap, never a fabricated `CallerRef` and never sufficient for a finding.
- Do not add dependencies, networked unit tests, LLM calls, or a new MCP tool.
- Keep the current empty sentinel compatible: it must still contain `не найдено`.
- The global plugin remains the decision-maker: Python supplies evidence and scope, not a breakage verdict.

---

## File Map

- `reviewer/tools/impact.py` — caller evidence model, computation, and text formatter.
- `tests/tools/test_impact.py` — unit and StructuredTool regression coverage for the engine contract.
- `reviewer/tools/code_tools.py` — PR-session tool description presented to in-process agents.
- `reviewer/mcp/service.py` — service-layer description of `get_impact`.
- `reviewer/entrypoints/mcp_server.py` — public FastMCP schema description.
- `plugin/skills/_common/tool-usage.md` — shared one-line tool contract included in plugin prompts.
- `plugin/skills/review-pr/references/blast-radius-prompt.md` — verification policy consumed by the global review plugin.
- `tests/skills/test_common_blocks.py` — guard for the shared tool description.
- `tests/skills/test_assembled_prompts.py` — assembled blast-radius prompt contract.
- `tests/mcp/test_server.py` — public MCP tool-description contract.

### Task 1: Return grounded caller evidence without path-level false negatives

**Files:**

- Modify: `tests/tools/test_impact.py:12-128`
- Modify: `reviewer/tools/impact.py:1-86`

**Interfaces:**

- Consumes: `graph.callers(repo, node_ids, branch=branch) -> set[str]`.
- Consumes: `store.fetch_nodes(repo, node_ids, overlay_ref, changed_paths, base_ref=...) -> list[Retrieved]`.
- Produces: `CallerRef(node_id, path, line, snippet, changed_file=False)`.
- Produces: `ImpactItem(node_id, old_sig, new_sig, callers=[], unresolved_caller_ids=[])`.
- Produces: `format_impact(items) -> str` with `[в PR]`, `[вне PR]`, and explicit coverage-gap rows.

- [ ] **Step 1: Make the fake store model overlay/base selection**

Replace `_Store.fetch_nodes` in `tests/tools/test_impact.py` with an implementation that mirrors the production freshness rule:

```python
def fetch_nodes(self, repo, node_ids, overlay_ref, changed_paths, *, base_ref="base"):
    changed = set(changed_paths or [])
    overlay = self._by_ref.get(overlay_ref, {})
    base = self._by_ref.get(base_ref, {})
    out = []
    for nid in node_ids:
        path = nid.split("#", 1)[0]
        source = overlay if path in changed else base
        if nid in source:
            out.append(_ret(nid, source[nid]))
    return out
```

This makes a same-file caller test exercise the same source choice as
`ChunkStore.fetch_nodes`: overlay for files in the PR, base otherwise.

- [ ] **Step 2: Write failing scope and unresolved-metadata tests**

Replace `test_compute_impact_no_external_callers_skipped` and add the missing-node case:

```python
def test_compute_impact_keeps_callers_in_changed_files_and_marks_scope():
    graph = _Graph({"svc.py#f": ["svc.py#local", "outside.py#g"]})
    store = _Store({
        "pr:1": {
            "svc.py#f": "def f(a, b, c):\n    ...",
            "svc.py#local": "def local():\n    f(1, 2)",
        },
        "base:dev": {
            "svc.py#f": "def f(a, b):\n    ...",
            "outside.py#g": "def g():\n    f(1, 2)",
        },
    })

    items = compute_impact(
        graph,
        store,
        repo="r",
        branch="dev",
        changed_node_ids=["svc.py#f"],
        changed_paths=["svc.py"],
        overlay_ref="pr:1",
    )

    assert len(items) == 1
    callers = {caller.node_id: caller for caller in items[0].callers}
    assert callers["svc.py#local"].changed_file is True
    assert callers["outside.py#g"].changed_file is False
    assert items[0].unresolved_caller_ids == []


def test_compute_impact_separates_unresolved_graph_callers_from_grounded_callers():
    graph = _Graph({"svc.py#f": ["missing.py#g"]})
    store = _Store({
        "pr:1": {"svc.py#f": "def f(a, b):\n    ..."},
        "base:dev": {"svc.py#f": "def f(a):\n    ..."},
    })

    items = compute_impact(
        graph,
        store,
        repo="r",
        branch="dev",
        changed_node_ids=["svc.py#f"],
        changed_paths=["svc.py"],
        overlay_ref="pr:1",
    )

    assert len(items) == 1
    assert items[0].callers == []
    assert items[0].unresolved_caller_ids == ["missing.py#g"]

    rendered = format_impact(items)
    assert "missing.py#g" in rendered
    assert "пробел покрытия" in rendered
    assert ":0 |" not in rendered
```

Keep the existing decorated async test unchanged; it is the PRI-158 regression.

- [ ] **Step 3: Run the new tests and verify the old contract fails**

Run:

```bash
.venv/bin/pytest \
  tests/tools/test_impact.py::test_compute_impact_keeps_callers_in_changed_files_and_marks_scope \
  tests/tools/test_impact.py::test_compute_impact_separates_unresolved_graph_callers_from_grounded_callers \
  -q
```

Expected: FAIL because same-file callers are filtered, `CallerRef.changed_file` is absent, and missing nodes are still converted to a stub.

- [ ] **Step 4: Extend the evidence dataclasses**

In `reviewer/tools/impact.py`, retain positional compatibility for existing tests by appending defaulted fields:

```python
@dataclass
class CallerRef:
    """Прочитанный из индекса caller-кандидат для проверки."""

    node_id: str
    path: str
    line: int
    snippet: str
    changed_file: bool = False


@dataclass
class ImpactItem:
    """Символ с изменённой сигнатурой и evidence его callers."""

    node_id: str
    old_sig: str
    new_sig: str
    callers: list[CallerRef] = field(default_factory=list)
    unresolved_caller_ids: list[str] = field(default_factory=list)
```

Update the module/class docstrings so they no longer promise callers only outside the diff.

- [ ] **Step 5: Remove the path filter and classify resolved/unresolved callers**

Replace the caller block in `compute_impact` with:

```python
caller_ids = sorted(graph.callers(repo, [nid], branch=branch))
if not caller_ids:
    continue
nodes = {
    node.node_id: node
    for node in store.fetch_nodes(
        repo,
        caller_ids,
        overlay_ref,
        changed_paths,
        base_ref=base_ref(branch),
    )
}
callers: list[CallerRef] = []
unresolved: list[str] = []
for cid in caller_ids:
    node = nodes.get(cid)
    if node is None:
        unresolved.append(cid)
        continue
    snippet = extract_signature(node.text) or (
        node.text.splitlines()[0] if node.text else ""
    )
    callers.append(
        CallerRef(
            cid,
            node.path,
            node.start_line,
            snippet,
            changed_file=node.path in changed,
        )
    )
items.append(ImpactItem(nid, old_sig, new_sig, callers, unresolved))
```

Do not compare base/overlay caller bodies and do not suppress a caller merely because its symbol changed.

- [ ] **Step 6: Render truthful scope and coverage**

Implement the formatter rows with one stable line per grounded caller plus one gap row:

```python
rows = [
    f"    - [{'в PR' if caller.changed_file else 'вне PR'}] "
    f"{caller.path}:{caller.line} | {caller.snippet}"
    for caller in item.callers
]
if item.unresolved_caller_ids:
    rows.append(
        "    - [пробел покрытия] метаданные callers не найдены в индексе "
        f"({len(item.unresolved_caller_ids)}): "
        + ", ".join(item.unresolved_caller_ids)
    )
```

Use the heading `кандидаты callers для проверки:`. Preserve a no-result sentinel containing `не найдено`.

- [ ] **Step 7: Strengthen formatter and end-to-end assertions**

Extend `test_format_impact_renders_callers`:

```python
items = [
    ImpactItem(
        "svc.py#f",
        "def f(a):",
        "def f(a, b):",
        [
            CallerRef("svc.py#local", "svc.py", 10, "def local():", changed_file=True),
            CallerRef("a.py#g", "a.py", 20, "def g():", changed_file=False),
        ],
    )
]
out = format_impact(items)
assert "[в PR] svc.py:10" in out
assert "[вне PR] a.py:20" in out
assert "кандидаты" in out
assert "устаревшие вызывающие" not in out
```

Extend `test_get_impact_tool_registered_and_runs` to assert `[вне PR]` appears in the StructuredTool result. Keep `test_get_impact_tool_no_graph` unchanged.

- [ ] **Step 8: Run the complete engine test file**

Run:

```bash
.venv/bin/pytest tests/tools/test_impact.py -q
.venv/bin/ruff check reviewer/tools/impact.py tests/tools/test_impact.py
```

Expected: all impact tests PASS and ruff reports no errors.

- [ ] **Step 9: Commit the engine contract**

```bash
git add reviewer/tools/impact.py tests/tools/test_impact.py
git commit -m "fix(tools): вернуть grounded callers в get_impact (PRI-155)"
```

### Task 2: Align the global plugin and public MCP contract

**Files:**

- Modify: `tests/skills/test_common_blocks.py:68-76`
- Modify: `tests/skills/test_assembled_prompts.py:51-60`
- Modify: `tests/mcp/test_server.py:76-119`
- Modify: `plugin/skills/_common/tool-usage.md:12-22`
- Modify: `plugin/skills/review-pr/references/blast-radius-prompt.md:1-86`
- Modify: `reviewer/tools/code_tools.py:149-153`
- Modify: `reviewer/mcp/service.py:368-370`
- Modify: `reviewer/entrypoints/mcp_server.py:72-74`

**Interfaces:**

- Consumes: Task 1 text with `[в PR]`, `[вне PR]`, and `[пробел покрытия]`.
- Produces: a global skill rule that verifies every grounded caller through `read_file` and `get_changed_file_diff`.
- Produces: MCP schema text describing callers inside and outside changed PR files plus explicit coverage gaps.

- [ ] **Step 1: Add failing prompt/common-block contract assertions**

Extend `test_blast_radius_assembled_has_tooling_and_confidence_tail`:

```python
assert "[в PR]" in b
assert "[вне PR]" in b
assert "does not prove that its call site was updated" in b
assert "coverage gap, not a finding" in b
```

Extend `test_tool_usage_has_both_tool_families`:

```python
assert "including files changed by the PR" in text
```

- [ ] **Step 2: Add a failing public MCP description assertion**

In `test_server_registers_all_tools`, after the exact name-set assertion, select the registered tool and check its schema description:

```python
impact = next(tool for tool in tools if tool.name == "get_impact")
assert "inside and outside changed PR files" in impact.description
assert "coverage gaps" in impact.description
```

- [ ] **Step 3: Run the contract tests and verify failure**

Run:

```bash
.venv/bin/pytest \
  tests/skills/test_common_blocks.py::test_tool_usage_has_both_tool_families \
  tests/skills/test_assembled_prompts.py::test_blast_radius_assembled_has_tooling_and_confidence_tail \
  tests/mcp/test_server.py::test_server_registers_all_tools \
  -q
```

Expected: FAIL because all current descriptions still promise callers only outside the diff and the prompt has no unresolved-ID rule.

- [ ] **Step 4: Update the shared one-line tool contract**

Change the `get_impact` entry in `plugin/skills/_common/tool-usage.md` to:

```markdown
- `get_impact` — grounded caller candidates for changed signatures, including files
  changed by the PR; marks PR scope and reports unresolved graph IDs as coverage gaps;
```

Do not add nested include markers.

- [ ] **Step 5: Rewrite only the caller half of the blast-radius method**

In `plugin/skills/review-pr/references/blast-radius-prompt.md`:

- describe check A as stale call-sites the per-file diff review can miss, including a caller in a changed file;
- state that grounded rows are marked `[в PR]` or `[вне PR]`;
- state that a changed caller file or caller symbol does not prove that its call site was updated;
- require `read_file` for every grounded caller;
- require `get_changed_file_diff` for both scopes: inspect the relevant hunk for `[в PR]`, confirm the sentinel for `[вне PR]`;
- state exactly: `An unresolved caller ID is a coverage gap, not a finding; never report breakage from that ID alone.`;
- preserve the empty-result rule, lower-bound warning, confidence scale, interface-expansion section, anchoring, and output schema.

Update the anchoring rationale: a stale caller may be outside the changed file or merely outside the changed hunk, so the finding still anchors on the changed signature.

- [ ] **Step 6: Align the three Python tool descriptions**

Use equivalent wording at every public layer:

```python
"""Blast-radius: symbols whose signature changed -> grounded caller candidates
inside and outside changed PR files, plus explicit index coverage gaps."""
```

Use Russian equivalents in `reviewer/tools/code_tools.py` and `reviewer/mcp/service.py`. Do not change method signatures or routing.

- [ ] **Step 7: Run prompt and MCP contract tests**

Run:

```bash
.venv/bin/pytest \
  tests/skills/test_common_blocks.py \
  tests/skills/test_assembled_prompts.py \
  tests/mcp/test_server.py \
  -q
.venv/bin/ruff check \
  reviewer/tools/code_tools.py \
  reviewer/mcp/service.py \
  reviewer/entrypoints/mcp_server.py \
  tests/skills/test_common_blocks.py \
  tests/skills/test_assembled_prompts.py \
  tests/mcp/test_server.py
```

Expected: all selected tests PASS and ruff reports no errors.

- [ ] **Step 8: Run repository verification**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
```

Expected: the full non-integration suite PASSes without external/localhost network access and ruff reports no errors.

- [ ] **Step 9: Commit the global plugin contract**

```bash
git add \
  plugin/skills/_common/tool-usage.md \
  plugin/skills/review-pr/references/blast-radius-prompt.md \
  reviewer/tools/code_tools.py \
  reviewer/mcp/service.py \
  reviewer/entrypoints/mcp_server.py \
  tests/skills/test_common_blocks.py \
  tests/skills/test_assembled_prompts.py \
  tests/mcp/test_server.py
git commit -m "fix(skills): проверять callers внутри файлов PR (PRI-155)"
```

## Final Verification

- [ ] `git diff --check HEAD~2..HEAD` reports no whitespace errors.
- [ ] `rg -n "callers outside the PR diff|вызывающие вне диффа|live outside the diff" reviewer plugin/skills` returns no stale current-contract claims.
- [ ] `.venv/bin/pytest tests/tools/test_impact.py tests/skills/test_common_blocks.py tests/skills/test_assembled_prompts.py tests/mcp/test_server.py -q` passes.
- [ ] `.venv/bin/pytest -q` passes.
- [ ] `.venv/bin/ruff check .` passes.
- [ ] The implementation matches `docs/superpowers/specs/2026-07-29-pri-155-impact-evidence-contract-design.md` and the rewritten PRI-155 acceptance criteria.

## Chosen Execution Mode

Use **subagent-driven development**. Task 1 establishes a narrow typed/output
contract; Task 2 consumes that completed contract and can be reviewed independently.
A fresh implementer per task plus specification and quality review between them gives
better isolation than a long inline session, while the two-task size avoids unnecessary
fan-out.
