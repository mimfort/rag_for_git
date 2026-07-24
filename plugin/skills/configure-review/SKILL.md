---
name: reviewer_configure-review
description: Configure or update a repo's .review.yml context layer and generic task-board metadata without secrets. Use when the user asks to set up or tune review config, context depth, ignored tracked paths, retrieval limits, or board selection.
---

# Configure Review

Always answer the user in Russian. Update only the requested `.review.yml` values; **Never clobber**
foreign keys such as `categories`. This skill is standalone: no reviewer MCP, database, or board
connection is required for the baseline.

## Scope

Manage `summary_cluster_depth`, `summary_cluster_depth_overrides`, `summary_topk_threshold`,
`paths.ignore`, `context_limits`, and the optional `task_board` block. An empty `task_board:`
disables the board for this repository. Never put credential values in `.review.yml`.

Untracked `.venv`, `node_modules`, `__pycache__`, `dist`, and `build` are gitignored and never
enter the index. Do not use a filesystem walk to find them.

## Pipeline

1. Resolve the repository and branch, verify it with `git rev-parse --git-dir`, and read the
   existing `.review.yml` without changing unrelated keys or comments.
2. Scan only tracked Python files:
   ```bash
   git -C <path> ls-tree -r --name-only <branch> | grep '\.py$'
   ```
   Count directory prefixes at depths 1–3. This is not a filesystem walk.
3. Measure churn with `git log --since="6 months ago" --name-only --pretty=format: -- '*.py'`.
   If history is too short or unavailable, say so and recommend from structure alone.
4. Propose depth and ignore changes. Ask the user about every candidate for `paths.ignore` and
   **never write it silently**. Changing depth/threshold/limits needs no rebuild needed; changing
   ignore can require `/reviewer_sync-codebase`. Suggest that command or
   `/reviewer_summarize-subsystems` when relevant, but **do NOT run** either command.

## Generic board metadata

Ask whether to keep, disable, or configure `task_board`. A configured block uses only this shape:

```yaml
task_board:
  type: <registered board_type>
  project: <optional project prefix>
  key_pattern: '<optional task-key pattern>'
  create_target: <selected target id or null>
  done_target: <selected target id or null>
  options: {}
```

`project` scopes board sync and task retrieval. Explain that an empty `task_board.project` can mix
all projects, then ask for the intended project prefix.

When a board type is selected, call the read-only discovery tool:

```
get_board_targets(board_type=<type>, project=<project>, provider_options=<task_board.options or {}>)
```

Its normalized response is `{board_type, project, targets, options, warnings}`. Present a
**pick-list** of `targets` by `label`; use `purposes` to select `create_target` and `done_target`.
For every option whose `required_for` contains `sync`, `create`, or `finish`, present its `choices`
by label and write the selected `id` into `task_board.options`. If discovery is unavailable, empty,
or returns an error, **fall back to asking** the user for each required generic value. Do not guess
targets or options.

The resulting values are non-secret metadata. Board access is configured outside this file; do not
request, display, or write credentials.

## Retrieval profile

Choose one profile from tracked-file structure and show the full `context_limits` draft:

| Profile | Condition |
|---|---|
| tiny-util | fewer than 80 tracked Python files and one package |
| standard | 80–800 files |
| large / monorepo | over 800 files or at least three large packages |

Use `count_tasks(project)` only when reviewer MCP is available to size task retrieval. A missing
tool, zero count, or unavailable corpus **falls back to asking** the user for small/medium/large.
Preserve every other configuration key and ask for confirmation before writing the assembled draft.

## Completion

Report the changed keys, the selected generic targets/options, and any recommended follow-up. This
skill makes configuration-only recommendations and has no index side effects.
