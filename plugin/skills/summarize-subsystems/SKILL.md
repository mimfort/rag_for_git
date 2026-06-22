---
name: reviewer_summarize-subsystems
description: Precompute concise per-subsystem summaries (GraphRAG community summaries) over the base code index, so ask / PR-walkthrough get a cheap high-level prior. Use when the user asks to build/refresh subsystem summaries ("просуммируй подсистемы", "построй обзоры модулей", "summarize subsystems"). Requires a built base index + the reviewer MCP server.
---

# Summarize subsystems (community summaries)

Cluster the base code graph into subsystems (by module path) and write a short, **grounded**
summary for each, persisted for `ask` / PR-walkthrough to use as a cheap high-level prior. This
skill reads code and writes summaries to the reviewer store; it does NOT modify code or post to
GitHub.

**Always write summaries and answer the user in Russian** (the project language), regardless of this
file's language. Tool calls, code identifiers and `path:line` stay verbatim.

## Tools

<!-- include: _common/tool-usage.md -->
Plus `list_subsystem_clusters` and `index_subsystem_summary` (reviewer MCP), and the harness `Read`.

## Pipeline

1. **Resolve repo/branch.**

<!-- include: _common/branch-selection.md -->

2. **List clusters.** Call `list_subsystem_clusters(repo, branch)`. Empty / `note` about an empty
   index → tell the user (in Russian) to run `/reviewer_sync-codebase` first, then stop.

3. **Summarize only STALE clusters.** For each cluster with `stale == true` (fresh ones are already
   up to date — skip them, this keeps the pass incremental and cheap):
   - `Read` a few representative files (from `files` / `top_symbols`) to ground the summary — do NOT
     invent behavior the code does not show.
   - Write, in Russian:
     - `title` — one line: what this subsystem is.
     - `summary` — a compact paragraph: what it does, its key symbols (from `top_symbols`) and
       invariants. No `path:line` required in the text; it is a high-level prior.
   - Persist: `index_subsystem_summary(repo, branch, cluster_key, title, summary, source_hash)` —
     pass back the cluster's own `source_hash` from step 2.

4. **Report (Russian).** How many clusters summarized vs skipped-as-fresh.

## Grounding (hard rule)

<!-- include: _common/anti-hallucination.md -->

Every summary must reflect real code you read. If a cluster is unclear, say so briefly rather than
guessing.

## Notes

- Precondition: base index built (`reviewer index`). Re-running is incremental: unchanged subsystems
  (matching `source_hash`) are skipped.
- Read-only on code and GitHub; only writes summaries to the reviewer store.
