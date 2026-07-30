---
name: configure-review
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
   **never write it silently**. Follow the exact rebuild map below; suggest but **do NOT run** a
   follow-up skill.

## Rebuild guidance

- Changed `paths.ignore` → suggest `/reviewer_sync-codebase`.
- Changed `summary_cluster_depth` → suggest `/reviewer_summarize-subsystems`.
- Changed `summary_cluster_depth_overrides` → suggest `/reviewer_summarize-subsystems`.
- Changed `summary_topk_threshold` → no rebuild needed.
- Changed `context_limits` → no rebuild needed.

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

Choose one profile from tracked-file structure and write all real `context_limits` fields:

| Profile | Condition | search_codebase: floor / ceiling / ratio / abs_floor / candidate_pool / ann_distance_max | graph: hops / callers_topk |
|---|---|---|---|
| tiny-util | fewer than 80 tracked Python files and one package | 3 / 8 / 0.60 / 0.35 / 20 / 0.65 | 1 / 20 |
| standard | 80–800 files | 4 / 15 / 0.50 / 0.30 / 30 / 0.65 | 1 / 25 |
| large / monorepo | over 800 files or at least three large packages | 4 / 25 / 0.45 / 0.30 / 40 / 0.60 | 1 / 30 |

Write the selected profile as:

```yaml
context_limits:
  search_codebase:
    floor: <profile value>
    ceiling: <profile value>
    ratio: <profile value>
    abs_floor: <profile value>
    candidate_pool: <profile value>
    ann_distance_max: <profile value>
  search_tasks:
    floor: <board-size value>
    ceiling: <board-size value>
  graph:
    hops: <profile value>
    callers_topk: <profile value>
```

Map `count_tasks(project)` to `search_tasks` deterministically: `< 150` → `3 / 8`;
`150–800` → `3 / 10`; `800+` → `4 / 14`. A missing tool, zero count, or unavailable corpus
**falls back to asking** the user for small/medium/large, then uses the same mapping.

Preserve every other configuration key and ask for confirmation before writing the assembled draft.

## Completion

Report the changed keys, the selected generic targets/options, and any recommended follow-up. This
skill makes configuration-only recommendations and has no index side effects.
