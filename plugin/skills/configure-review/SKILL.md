---
name: configure-review
description: Use when configuring or changing a repository's layered review policy, ignored tracked paths, retrieval limits, summary depth, or non-secret task-board metadata.
---

# Configure Review

Always answer the user in Russian. Update only the requested policy values; **Never clobber** foreign
keys such as `categories`. This skill is standalone: no reviewer MCP, database, or board connection
is required for the baseline.

## Scope

Manage `summary_cluster_depth`, `summary_cluster_depth_overrides`, `summary_topk_threshold`,
`paths.ignore`, `context_limits`, and the optional `task_board` block, including its generic
`sync_filter`. An empty `task_board:` disables the board for this repository. Never read, request,
display, or write credential values in either policy target.

Untracked `.venv`, `node_modules`, `__pycache__`, `dist`, and `build` are gitignored and never
enter the index. Do not use a filesystem walk to find them.

## Pipeline

1. Resolve the canonical lowercase repository id and the target branch. Present these targets in
   this order and ask the user to select one:
   - **Recommended/default:** `home:repos/<owner>/<name>.yml`, stored at
     `$XDG_CONFIG_HOME/rag-reviewer/repos/<owner>/<name>.yml` (or
     `~/.config/rag-reviewer/...` when `XDG_CONFIG_HOME` is unset). It needs no commit and is not
     visible to the team.
   - **Team-visible:** committed `.review.yml` at the selected target ref. It is committed and
     visible to the team; read it from that ref, never from an uncommitted worktree file.
   For a nested id such as `group/service`, use `home:repos/group/service.yml`. A home policy is
   owned by the OS account running reviewer: on a shared service account it can affect that
   account's workloads, so use committed policy for team-owned settings.
2. After the target is selected, verify the repository with `git rev-parse --git-dir` and read the
   selected file, preserving unrelated keys and comments. Do not inspect or copy credentials.
3. Scan only tracked Python files:
   ```bash
   git -C <path> ls-tree -r --name-only <branch> | grep '\.py$'
   ```
   Count directory prefixes at depths 1–3. This is not a filesystem walk.
4. Measure churn with `git log --since="6 months ago" --name-only --pretty=format: -- '*.py'`.
   If history is too short or unavailable, say so and recommend from structure alone.
5. Propose depth and ignore changes. Ask the user about every candidate for `paths.ignore` and
   **never write it silently**. Assemble a draft that preserves the selected file's unrelated
   keys/comments, then request final confirmation before writing it. Follow the exact rebuild map
   below; suggest but **do NOT run** a follow-up skill.

## Rebuild guidance

- Changed `paths.ignore` → suggest `rag-reviewer:sync-codebase`.
- Changed `summary_cluster_depth` → suggest `rag-reviewer:summarize-subsystems`.
- Changed `summary_cluster_depth_overrides` → suggest `rag-reviewer:summarize-subsystems`.
- Changed `summary_topk_threshold` → no rebuild needed.
- Changed `context_limits` → no rebuild needed.
- Changed `task_board.sync_filter` → suggest `rag-reviewer:sync-tasks` for a full unlimited run
  (`limit=null`); do **NOT run** it automatically.

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
  sync_filter:
    max_age_days: <integer >= 1, or omit for no age limit>
    include_archived: <boolean, default true>
```

`project` scopes board sync and task retrieval. Explain that an empty `task_board.project` can mix
all projects, then ask for the intended project prefix.

`sync_filter` is a generic sibling of provider `options`. The `sync_filter` block is optional.
Never put `sync_filter` under `options`. Ask two separate questions:

- `max_age_days`: choose an integer greater than or equal to 1, or no age limit.
- `include_archived`: choose whether archived tasks are included; the default is `true`.

Age uses task last-modified time and an inclusive cutoff: a task modified exactly at the cutoff is
eligible. Archive is distinct from terminal/done; `include_archived: false` excludes only tasks
known to be archived. Age filtering runs first. Only while `include_archived: false`, unknown
archive metadata does not itself exclude the row; an archive warning is emitted only then and only
when age filtering did not already exclude the row.

### Editing `sync_filter` safely

When changing only `sync_filter`, use this deterministic materialization procedure:

1. Read policy layers in precedence order: non-secret ENV/deploy `task_board` defaults,
   `home:review.yml`, committed `.review.yml`, then `home:repos/<owner>/<name>.yml`; stop at the
   selected target. Never inspect or copy credential env values. For a committed target, do not
   read the higher repo-home layer. For the recommended home per-repo target, include all layers.
2. If the selected layer has a non-empty `task_board` mapping, use that mapping alone as the edit
   base. Preserve every sibling and field-attached comment already present, but do not copy or
   overlay omitted fields from lower layers: the selected mapping already shadows the complete
   lower block.
3. If the selected layer has no `task_board` key, resolve only the lower layers with normal
   whole-block replacement, then materialize the complete lower effective non-secret `task_board`
   into the selected-layer draft. Copy `type`, `project`, `key_pattern`, `url_template`,
   `create_target`, `done_target`, `options`, every other non-secret sibling, and field-attached
   comments. If no lower board exists, ask for a fully configured board; never write a new partial
   `task_board` containing only the filter.
4. If the selected layer explicitly contains null or an empty mapping, preserve that disable and do
   not add `sync_filter`. Only proceed when the user explicitly chooses to replace it with a fully
   configured board assembled from confirmed values; never resurrect lower fields silently.
5. For cases 2 or 3, patch only `sync_filter` in the chosen or materialized block. The selected layer
   remains a self-contained whole-block replacement.

Because policy layers replace the whole `task_board` block, preserve every sibling field and
comment when changing `sync_filter`. Repositories using the same project share one task corpus, so
different retention views require different project scopes. Keep home per-repo as the recommended
target for repository-specific policy. A filter change is evaluated on the next successful full
sync and backfills newly eligible tasks; purge remains explicit and is never enabled by a filter
change.

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
