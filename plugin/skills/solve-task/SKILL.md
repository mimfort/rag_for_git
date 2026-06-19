---
name: reviewer_solve-task
description: Gather disciplined context for solving a task, then hand off to development. Use when the user asks to solve/implement a task ("solve PRI-4", "/reviewer_solve-task <key or description>", "реши задачу X"). Reads the task from a connected board (if a key + board), pulls related/similar tasks and relevant code, distills a brief, and enters brainstorming. Requires the reviewer MCP server (and optionally a board MCP).
---

# Solve Task

Gather the right context for a task, distill it into a brief, then enter the normal development
workflow. This skill does NOT plan or implement — it disciplines context-gathering and hands the
brief to `superpowers:brainstorming` (which leads to writing-plans → subagent-driven-development).

## Inputs

`$ARGUMENTS` is either:
- a task key (e.g. `PRI-4`, matching the board's `key_pattern`), or
- a free-text description (e.g. "add a logout endpoint").

## Pipeline

1. **Config.** Resolve the `task_board` block (`type`, `mcp`, `key_pattern`): first from the repo's
   `.review.yml`, and if there is no block there, from the deploy-wide default via
   `get_board_config()` (reviewer MCP) — so a per-repo `.review.yml` is not required when the board
   is configured once in the reviewer deploy (`TASK_BOARD_*` env). If a board is resolved, its tools
   are `mcp__<task_board.mcp>__*`. No block anywhere (`get_board_config()` → `null`), or the board MCP
   is not connected → board-less mode (continue without it).

2. **Identify the task.**
   - If `$ARGUMENTS` matches the board's `key_pattern` AND a board is configured/connected: read the
     task via the playbook `../review-pr/references/task-context-<task_board.type>.md` and build a
     `TaskBrief` `{key, aliases[], title, description, criteria[], status, url, links[]}`. Then call
     `index_task(TaskBrief)` to persist it (idempotent — safe to repeat).
   - Otherwise: treat `$ARGUMENTS` as the task description; do not read the board.

3. **Gather context (best-effort, fail-open).** Any tool returning a "(… unavailable)" / "(ничего не
   найдено)" note or an error is non-fatal — continue.
   - If you have a task key: `get_task_context(key)` → linked tasks, their PRs, and the code those PRs
     touched.
   - `search_tasks("<title>. <first lines of description>")` → semantically similar tasks. If a board
     is connected, you may read the most relevant similar tasks from the board for fuller detail.
   - `search_codebase("<task description>")` → relevant existing code (files/symbols to touch or
     mimic).
   - **Deepen via the code graph (optional — when `search_codebase` surfaced concrete symbols).**
     `search_codebase` chunks are headed by `path#fqn (path:start-end)`; feed those `node_id`s to the
     session-less graph tools to sharpen the brief:
     - `callers(repo, node_id, branch?)` → who depends on the code you would change (blast radius — what not to break);
     - `related_symbols(repo, node_id, branch?)` → neighbors (calls / implementations / tests) to touch or mimic;
     - `definition(repo, symbol, branch?)` → exact source of a symbol to follow as a pattern.
     - **Lazy PR diff (optional).** `get_task_context` surfaces a task and its PRs (id form
       `owner/name#N`); `search_tasks` surfaces similar task keys — fetch a key's context to see
       its PRs. If a related task passed the relevance filter AND its PR is worth inspecting for
       the implementation, parse `repo`/`number` from the PR id and call `get_pr_diff(repo, number)`
       to see what that PR changed — pull it lazily, only when the LLM judges it useful (don't
       fetch diffs for low-relevance tasks).
       Fail-open: a `(diff PR недоступен)` / `(repo не задан…)` note is non-fatal — continue.
     `search_codebase` now returns deduplicated, line-numbered, test-free snippets — keep using the
     graph tools for blast radius, but expand only the few symbols central to the task, and cite
     `path:line` from the line-numbered snippets directly (no re-Read needed for grounding).
     Pass the same `branch` you pass to `search_codebase`.
     Fail-open: a `(граф недоступен)` / `(нет связей)` / `(вызовов не найдено)` note is non-fatal — continue.

   **Branch selection for `search_codebase`.** Before calling `search_codebase`, determine the
   current git branch of the project: `git branch --show-current`. If it is in `REVIEW_BRANCHES`
   (the tracked branches list), pass it as the `branch` parameter — the search will use that
   branch's index. If the user explicitly stated which branch to work from, use that branch instead.
   Otherwise, omit `branch` entirely and the server will use the primary branch (the first entry in
   `REVIEW_BRANCHES`).
   The same branch applies to `callers` / `related_symbols` / `definition` — pass it (or omit it) identically.

4. **Distill the solution brief.** Write a structured markdown brief. Apply a strict relevance
   filter: include an item ONLY if it directly informs the implementation; drop the rest and note how
   many were dropped. Sections:
   - **Task** — key/title/requirements/criteria (or the user's formulation in board-less mode).
   - **Related work** — only the relevant linked/similar tasks and their PRs (what to reuse / follow).
   - **Relevant code** — files/symbols to touch or mimic, each with a one-line "why"; where the graph surfaced them, note key callers / impacted symbols (blast radius).
   - **Constraints / open questions** — limits, unknowns, and context gaps (e.g. "board unavailable",
     "task corpus empty").

5. **Hand off to development.** Show the brief, then invoke `superpowers:brainstorming` with the brief
   as the seed/context. From there the normal cycle takes over (brainstorming → writing-plans →
   subagent-driven-development/TDD). Your job ends at the handoff — do NOT plan or implement here.

## Failure handling (fail-open)

- No `task_board` / board MCP not connected / task not found → board-less: build the brief from
  `search_tasks` (if the corpus is warm) + `search_codebase` + the user's formulation; note the gap.
- Neo4j down → `get_task_context` / `index_task` graph parts degrade (empty + warning); build the
  brief from `search_tasks` + `search_codebase`.
- Empty task corpus (no prior `/reviewer_sync-tasks` or reviews) → `search_tasks` is empty; rely on the board
  (if a key) + `search_codebase`.
- Postgres down → `search_codebase` / `search_tasks` return empty; build the brief from the board (if
  a key) or the user's formulation alone; still hand off to brainstorming.
- Never abort: with any gap, distill what you have, note the deficit in the brief, and still hand off
  to brainstorming.
- Read-only on the board; this skill never writes to it.
