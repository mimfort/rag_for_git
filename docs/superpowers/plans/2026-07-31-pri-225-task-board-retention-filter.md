# PRI-225 Task Board Retention Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a generic per-repository age/archive filter to task-board synchronization, with exact counters, safe cursor transitions, optional provider pushdown, and project-scoped purge.

**Architecture:** Effective `task_board.sync_filter` is validated into an immutable `TaskSyncFilter`. Providers expose canonical lifecycle metadata through a streaming `TaskListing`, while `SyncService` owns filtering, counters, legacy/JSON cursor transitions, and purge safety. MCP repo mode resolves committed and home policy server-side so client skills never reconstruct effective config.

**Tech Stack:** Python 3.11, dataclasses, PyYAML, Click/FastMCP, Postgres `index_meta`, Neo4j task graph, pytest, Ruff, Markdown plugin skills, generated Codex plugin manifests.

---

## Execution Rules

- Follow TDD for every behavior change: add one focused failing test, observe the expected failure, implement the smallest change, then rerun the focused suite.
- Keep `task_board.sync_filter` separate from provider `options`; no credential value may enter YAML, logs, errors, or MCP results.
- No provider currently proves API pushdown with exact mutually exclusive reason counts. Implement the pushdown-capable contract, but return unfiltered streams and zero provider-side counts in this iteration.
- Preserve the legacy integer cursor whenever no active restriction exists.
- Do not enable retention in the repository's operational `.review.yml`; add only a commented example there.
- Commit checkpoints below are conditional. Run them only when the user explicitly requests commits; otherwise leave the verified changes uncommitted.

## File Map

**Create:**

- `reviewer/tasks/sync_filter.py` — pure active-policy, classification, and fingerprint functions.
- `reviewer/tasks/sync_cursor.py` — legacy integer/versioned JSON cursor parsing and serialization.
- `tests/tasks/test_sync_filter.py` — cutoff, lifecycle, unknown metadata, and fingerprint unit tests.
- `tests/tasks/test_sync_cursor.py` — cursor compatibility, corruption, and round-trip unit tests.

**Modify for configuration:**

- `reviewer/config/task_board.py` — `TaskSyncFilter`, nested validation, sparse config serialization.
- `tests/config/test_task_board_config.py` — filter validation and credential guards.
- `tests/config/test_layers.py` — per-repo effective-policy isolation and invalid-layer quarantine.
- `tests/policy/test_policy.py` — normalized filter propagation outside provider options.

**Modify for provider contract:**

- `reviewer/tasks/boards/base.py` — nullable update timestamp, canonical lifecycle fields, `TaskListingStats`, `TaskListing`, protocol signature.
- `reviewer/tasks/boards/{yougile,youtrack,jira,github,trello,linear,clickup,asana,yandex_tracker,kaiten,weeek}.py` — listing wrapper and lifecycle mapping.
- `tests/tasks/boards/contract.py`, `tests/tasks/boards/test_provider_contract.py`, provider read/normalize tests, and provider test fakes — contract migration.
- `tests/mcp/test_finish_task.py`, `tests/mcp/test_board_provider_extensibility.py`, `tests/tasks/test_sync_integration.py`, and other provider doubles — canonical `RawTask`/`TaskListing` use.

**Modify for orchestration and MCP:**

- `reviewer/tasks/sync.py` — local filtering, counters, transition backfill, cursor persistence, incomplete-listing safety.
- `tests/tasks/test_sync.py` — complete unit coverage of filtering/cursor/purge/error behavior.
- `reviewer/mcp/service.py` — repo-resolved and explicit sync modes.
- `reviewer/entrypoints/mcp_server.py` — public named MCP schema.
- `tests/mcp/test_sync_board.py`, `tests/mcp/test_server_tools.py`, `tests/mcp/test_board_provider_extensibility.py` — wiring and backward compatibility.

**Modify for user-facing contracts:**

- `plugin/skills/sync-tasks/SKILL.md` — server-resolved repo mode and retention reporting.
- `plugin/skills/configure-review/SKILL.md` — retention questions and sibling-preserving YAML edits.
- `tests/skills/test_sync_tasks_guardrail.py`, `tests/skills/test_configure_review_skill.py` — prompt contract guards.
- `docs/board-providers.md`, `README.md`, `README.ru.md`, `.review.yml` — documented config, semantics, and provider capabilities.
- `tests/docs/test_board_provider_docs.py`, `tests/docs/test_readme_onboarding.py`, `tests/test_review_yml_example.py` — documentation guards.
- `.codex-plugin/plugin.json`, `plugin/.codex-plugin/plugin.json`, `plugin/.claude-plugin/plugin.json`, `plugin/assets/icon.svg` — generated only by the manifest script.

---

### Task 1: Validate And Propagate `task_board.sync_filter`

**Files:**

- Modify: `reviewer/config/task_board.py:11-41,194-233`
- Modify: `tests/config/test_task_board_config.py`
- Modify: `tests/config/test_layers.py`
- Modify: `tests/policy/test_policy.py`

- [ ] **Step 1: Add failing normalization tests**

Add imports and these cases to `tests/config/test_task_board_config.py`:

```python
from dataclasses import FrozenInstanceError

import pytest

from reviewer.config.task_board import (
    TaskSyncFilter,
    normalize_task_board_config,
)


@pytest.mark.parametrize(
    ("raw_filter", "expected", "serialized"),
    [
        ({}, TaskSyncFilter(), {}),
        (
            {"max_age_days": 180},
            TaskSyncFilter(max_age_days=180),
            {"max_age_days": 180},
        ),
        (
            {"include_archived": False},
            TaskSyncFilter(include_archived=False),
            {"include_archived": False},
        ),
        (
            {"max_age_days": 180, "include_archived": False},
            TaskSyncFilter(max_age_days=180, include_archived=False),
            {"max_age_days": 180, "include_archived": False},
        ),
    ],
)
def test_task_sync_filter_normalizes_and_round_trips(
    raw_filter, expected, serialized
):
    raw = {
        "type": "yougile",
        "options": {"page_size": 50},
        "sync_filter": raw_filter,
    }

    config = normalize_task_board_config(raw)

    assert config is not None
    assert config.sync_filter == expected
    assert config.options == {"page_size": 50}
    assert config.as_dict()["sync_filter"] == serialized
    assert "sync_filter" not in config.options
    assert normalize_task_board_config(config.as_dict()) == config


def test_absent_task_sync_filter_is_not_serialized():
    config = normalize_task_board_config({"type": "yougile"})
    assert config is not None
    assert config.sync_filter is None
    assert "sync_filter" not in config.as_dict()


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "30", []])
def test_rejects_invalid_task_sync_filter_max_age_days(value):
    with pytest.raises(ValueError, match="max_age_days"):
        normalize_task_board_config({
            "type": "yougile",
            "sync_filter": {"max_age_days": value},
        })


@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
def test_rejects_invalid_task_sync_filter_include_archived(value):
    with pytest.raises(ValueError, match="include_archived"):
        normalize_task_board_config({
            "type": "yougile",
            "sync_filter": {"include_archived": value},
        })


def test_task_sync_filter_is_frozen():
    config = normalize_task_board_config({
        "type": "yougile",
        "sync_filter": {"max_age_days": 30},
    })
    assert config is not None and config.sync_filter is not None
    with pytest.raises(FrozenInstanceError):
        config.sync_filter.max_age_days = 60


def test_rejects_registered_secret_nested_in_sync_filter_without_echoing_value():
    secret = "do-not-echo-retention-secret"
    with pytest.raises(ValueError, match="must not contain credentials") as error:
        normalize_task_board_config({
            "type": "yougile",
            "sync_filter": {"future": [{"YOUTRACK_TOKEN": secret}]},
        })
    assert secret not in str(error.value)
```

- [ ] **Step 2: Run the tests and confirm the red state**

Run:

```bash
.venv/bin/pytest -q tests/config/test_task_board_config.py
```

Expected: collection fails because `TaskSyncFilter` does not exist, or assertions fail because `sync_filter` is not normalized/serialized.

- [ ] **Step 3: Implement the immutable filter and validator**

Add before `TaskBoardConfig` in `reviewer/config/task_board.py`:

```python
@dataclass(frozen=True)
class TaskSyncFilter:
    max_age_days: int | None = None
    include_archived: bool = True

    def as_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        if self.max_age_days is not None:
            result["max_age_days"] = self.max_age_days
        if not self.include_archived:
            result["include_archived"] = False
        return result

    def canonical_dict(self) -> dict[str, JsonValue]:
        return {
            "include_archived": self.include_archived,
            "max_age_days": self.max_age_days,
        }


def normalize_task_sync_filter(raw: object) -> TaskSyncFilter:
    values = _json_mapping(raw, field="task_board.sync_filter")
    max_age_days = values.get("max_age_days")
    if max_age_days is not None and (
        not isinstance(max_age_days, int)
        or isinstance(max_age_days, bool)
        or max_age_days < 1
    ):
        raise ValueError(
            "task_board.sync_filter.max_age_days must be an integer "
            "greater than or equal to 1"
        )
    include_archived = values.get("include_archived", True)
    if not isinstance(include_archived, bool):
        raise ValueError(
            "task_board.sync_filter.include_archived must be a boolean"
        )
    return TaskSyncFilter(
        max_age_days=max_age_days,
        include_archived=include_archived,
    )
```

Place `normalize_task_sync_filter` after `_json_mapping` so the helper is defined before use. Append this field after `warnings` in `TaskBoardConfig` to preserve positional compatibility:

```python
sync_filter: TaskSyncFilter | None = None
```

Add to `TaskBoardConfig.as_dict()`:

```python
if self.sync_filter is not None:
    result["sync_filter"] = self.sync_filter.as_dict()
```

In `normalize_task_board_config`, preserve absence versus present-empty:

```python
sync_filter = (
    normalize_task_sync_filter(raw["sync_filter"])
    if "sync_filter" in raw
    else None
)
```

Pass `sync_filter=sync_filter` to the `TaskBoardConfig` constructor. Keep credential scanning over the complete raw mapping before returning.

- [ ] **Step 4: Run config tests to green**

Run:

```bash
.venv/bin/pytest -q tests/config/test_task_board_config.py
.venv/bin/ruff check reviewer/config/task_board.py tests/config/test_task_board_config.py
```

Expected: all tests pass and Ruff reports no errors.

- [ ] **Step 5: Prove layered and policy propagation without production edits**

Add `test_task_board_sync_filter_replaces_whole_value_per_repo_and_reports_source` to `tests/config/test_layers.py`. Use `tmp_path / "repos/o/first.yml"` with project `ONE`/30 days and `tmp_path / "repos/o/second.yml"` with project `TWO`/90 days. Assert each `resolve_policy_data` call returns its own complete `task_board`, `meta.sources["task_board"]` names the matching home file, and `ReviewPolicy.load_data(...).task_board["sync_filter"]` has the expected values.

Add to `tests/policy/test_policy.py`:

```python
def test_policy_preserves_task_sync_filter_outside_provider_options():
    policy = ReviewPolicy.from_yaml("""
task_board:
  type: yougile
  project: PRI
  options: {page_size: 50}
  sync_filter:
    max_age_days: 180
    include_archived: false
""")
    assert policy.task_board == {
        "type": "yougile",
        "project": "PRI",
        "options": {"page_size": 50},
        "sync_filter": {
            "max_age_days": 180,
            "include_archived": False,
        },
    }
```

Run:

```bash
.venv/bin/pytest -q tests/config/test_layers.py tests/policy/test_policy.py
```

Expected: pass with no changes to `reviewer/config/layers.py` or `reviewer/policy/policy.py`; both already delegate through the task-board normalizer.

- [ ] **Step 6: Conditional commit checkpoint**

If commits were explicitly requested:

```bash
git add reviewer/config/task_board.py tests/config/test_task_board_config.py \
  tests/config/test_layers.py tests/policy/test_policy.py
git commit -m "feat(config): добавить retention-фильтр доски задач (PRI-225)"
```

---

### Task 2: Add The Streaming Provider Contract And Migrate YouGile

**Files:**

- Modify: `reviewer/tasks/boards/base.py:26-59`
- Modify: `reviewer/tasks/boards/yougile.py:121-171,266-306,377-407`
- Modify: `tests/tasks/boards/test_base.py`
- Modify: `tests/tasks/boards/test_yougile_normalize.py`
- Modify: `tests/tasks/boards/test_yougile_fetch_one.py`

- [ ] **Step 1: Add failing base-model tests**

Extend `tests/tasks/boards/test_base.py`:

```python
from reviewer.tasks.boards.base import RawTask, TaskListing, TaskListingStats


def test_rawtask_lifecycle_fields_are_tristate_and_timestamp_is_optional():
    raw = RawTask(
        key="ID-1",
        project_code="PRI-1",
        title="T",
        description="",
        status="Open",
        subtask_ids=[],
        timestamp=None,
        archived=None,
        terminal=False,
    )
    assert raw.timestamp is None
    assert raw.archived is None
    assert raw.terminal is False
    assert not hasattr(raw, "completed")


def test_task_listing_remains_iterable_for_existing_callers():
    raw = RawTask("ID-1", "PRI-1", "T", "", None, [], 1)
    stats = TaskListingStats(filtered_by_age=2)
    listing = TaskListing(rows=[raw], stats=stats)
    assert list(listing) == [raw]
    assert listing.stats.filtered_by_age == 2
```

- [ ] **Step 2: Verify the base tests fail**

Run:

```bash
.venv/bin/pytest -q tests/tasks/boards/test_base.py
```

Expected: failures for missing lifecycle fields and listing types.

- [ ] **Step 3: Implement `RawTask`, `TaskListingStats`, and `TaskListing`**

In `reviewer/tasks/boards/base.py`, import `Iterator` and `TYPE_CHECKING`, then add:

```python
if TYPE_CHECKING:
    from reviewer.config.task_board import TaskSyncFilter


@dataclass
class RawTask:
    key: str
    project_code: str
    title: str
    description: str
    status: str | None
    subtask_ids: list[str]
    timestamp: int | None
    links: list[dict] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    board_id: str = ""
    archived: bool | None = None
    terminal: bool | None = None
    provider_data: dict = field(default_factory=dict)


@dataclass
class TaskListingStats:
    filtered_by_age: int = 0
    filtered_archived: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class TaskListing:
    rows: Iterable[RawTask]
    stats: TaskListingStats = field(default_factory=TaskListingStats)

    def __iter__(self) -> Iterator[RawTask]:
        return iter(self.rows)
```

Change the protocol method to:

```python
def iter_raw(
    self,
    board: str | None,
    limit: int | None,
    *,
    sync_filter: "TaskSyncFilter | None" = None,
    now_ms: int | None = None,
) -> TaskListing:
    ...
```

- [ ] **Step 4: Add failing YouGile lifecycle assertions**

In `tests/tasks/boards/test_yougile_fetch_one.py`, assert the payload with `completed=True` produces `terminal is True`, `archived is None`, and the fixture without `timestamp` produces `timestamp is None`.

In `tests/tasks/boards/test_yougile_normalize.py`, replace only internal `RawTask(completed=...)` arguments with `terminal=...` and add:

```python
def test_normalize_terminal_maps_to_done_status():
    assert normalize_yougile(
        _raw(status="In progress", terminal=True), KP, URL
    )["status"] == "done"
```

Run:

```bash
.venv/bin/pytest -q \
  tests/tasks/boards/test_yougile_normalize.py \
  tests/tasks/boards/test_yougile_fetch_one.py
```

Expected: failures because YouGile still emits integer-zero timestamps and uses `completed` internally.

- [ ] **Step 5: Wrap the YouGile generator and map only proven metadata**

Import `TaskListing` and `TaskListingStats`. Rename the current generator body to `_iter_raw_rows(self, board, limit)`. Add this public wrapper:

```python
def iter_raw(
    self,
    board: str | None,
    limit: int | None,
    *,
    sync_filter=None,
    now_ms=None,
) -> TaskListing:
    return TaskListing(
        rows=self._iter_raw_rows(board, limit),
        stats=TaskListingStats(),
    )
```

Add local parsers and use them in both listing and `fetch_one`:

```python
def _optional_ms(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(payload: Mapping, field: str) -> bool | None:
    value = payload.get(field)
    return value if isinstance(value, bool) else None
```

Construct lifecycle fields as:

```python
timestamp=_optional_ms(task.get("timestamp")),
archived=None,
terminal=_optional_bool(task, "completed"),
```

Do not guess a YouGile archive field or pushdown query. Replace `raw.completed` checks with `raw.terminal is True`.

- [ ] **Step 6: Run focused provider tests**

Run:

```bash
.venv/bin/pytest -q \
  tests/tasks/boards/test_base.py \
  tests/tasks/boards/test_yougile_normalize.py \
  tests/tasks/boards/test_yougile_fetch_one.py
.venv/bin/ruff check reviewer/tasks/boards/base.py reviewer/tasks/boards/yougile.py \
  tests/tasks/boards/test_base.py tests/tasks/boards/test_yougile_normalize.py \
  tests/tasks/boards/test_yougile_fetch_one.py
```

Expected: pass.

- [ ] **Step 7: Conditional commit checkpoint**

If commits were explicitly requested:

```bash
git add reviewer/tasks/boards/base.py reviewer/tasks/boards/yougile.py \
  tests/tasks/boards/test_base.py tests/tasks/boards/test_yougile_normalize.py \
  tests/tasks/boards/test_yougile_fetch_one.py
git commit -m "feat(tasks): добавить lifecycle и listing-контракт (PRI-225)"
```

---

### Task 3: Migrate YouTrack, Jira, GitHub, Trello, And Linear Providers

**Files:**

- Modify: `reviewer/tasks/boards/youtrack.py:168-181,300-315`
- Modify: `reviewer/tasks/boards/jira.py:118-122,579-637`
- Modify: `reviewer/tasks/boards/github.py:170-182,405-467`
- Modify: `reviewer/tasks/boards/trello.py:150-161,319-341,388-401`
- Modify: `reviewer/tasks/boards/linear.py:43-54,214-225,290-343`
- Modify: corresponding `tests/tasks/boards/test_*_{read,normalize}.py`

- [ ] **Step 1: Change provider tests to the canonical lifecycle expectations**

Add or update focused assertions:

```python
# YouTrack and Jira: no machine lifecycle category in current fields.
assert raw.archived is None
assert raw.terminal is None

# GitHub: closed is terminal, archive remains unknown.
assert closed_raw.terminal is True
assert open_raw.terminal is False
assert closed_raw.archived is None

# Trello: native `closed` is archive; terminal remains unknown.
assert archived_raw.archived is True
assert open_raw.archived is False
assert archived_raw.terminal is None

# Linear: completed/canceled state types are terminal.
assert completed_raw.terminal is True
assert started_raw.terminal is False
assert completed_raw.archived is None
```

Change missing/invalid update-time expectations from `0` to `None` in provider read tests.

- [ ] **Step 2: Run the provider tests and observe failures**

Run:

```bash
.venv/bin/pytest -q \
  tests/tasks/boards/test_youtrack_normalize.py \
  tests/tasks/boards/test_jira_read.py \
  tests/tasks/boards/test_github_read.py tests/tasks/boards/test_github_normalize.py \
  tests/tasks/boards/test_trello_read.py tests/tasks/boards/test_trello_normalize.py \
  tests/tasks/boards/test_linear_read.py tests/tasks/boards/test_linear_normalize.py
```

Expected: lifecycle and nullable timestamp assertions fail; providers still return generators with the old signature.

- [ ] **Step 3: Wrap each provider listing without pushdown**

For each of the five providers, import `TaskListing` and `TaskListingStats`, rename the existing generator body to `_iter_raw_rows`, and add this exact public method:

```python
def iter_raw(
    self,
    board: str | None,
    limit: int | None,
    *,
    sync_filter=None,
    now_ms=None,
) -> TaskListing:
    return TaskListing(
        rows=self._iter_raw_rows(board, limit),
        stats=TaskListingStats(),
    )
```

The private generator retains existing board scoping, pagination, and raw-limit behavior verbatim.

- [ ] **Step 4: Apply the audited lifecycle mappings**

Use these exact semantics:

| Provider | `archived` | `terminal` | Missing update time |
|---|---|---|---|
| YouTrack | `None` | `None` | `None` |
| Jira | `None` | `None` | `None` |
| GitHub | `None` | `state == "closed"` when `state` is present, otherwise `None` | `None` |
| Trello | native boolean `closed`, otherwise `None` | `None` | `None` |
| Linear | `None` | state type in `{"completed", "canceled"}`; false for another known type; `None` when absent | `None` |

Every boolean mapping must preserve absence; do not use `bool(payload.get(...))`. Change Trello timestamp sorting to a deterministic nullable key:

```python
rows.sort(key=lambda row: (row.timestamp is None, row.timestamp or 0))
```

- [ ] **Step 5: Run focused tests and lint**

Run the Step 2 test command again, followed by:

```bash
.venv/bin/ruff check reviewer/tasks/boards/{youtrack,jira,github,trello,linear}.py \
  tests/tasks/boards/test_{youtrack_normalize,jira_read,github_read,github_normalize,trello_read,trello_normalize,linear_read,linear_normalize}.py
```

Expected: pass.

- [ ] **Step 6: Conditional commit checkpoint**

If commits were explicitly requested:

```bash
git add reviewer/tasks/boards/{youtrack,jira,github,trello,linear}.py \
  tests/tasks/boards/test_{youtrack_normalize,jira_read,github_read,github_normalize,trello_read,trello_normalize,linear_read,linear_normalize}.py
git commit -m "refactor(tasks): канонизировать lifecycle первой группы досок (PRI-225)"
```

---

### Task 4: Migrate The Remaining Providers And Activate The Shared Contract

**Files:**

- Modify: `reviewer/tasks/boards/clickup.py:158-176,362-458`
- Modify: `reviewer/tasks/boards/asana.py:57-68,288-337,448-500`
- Modify: `reviewer/tasks/boards/yandex_tracker.py:200-216,249-379`
- Modify: `reviewer/tasks/boards/kaiten.py:188-200,477-548`
- Modify: `reviewer/tasks/boards/weeek.py:171-183,458-534,841-864`
- Modify: corresponding provider read/normalize tests
- Modify: `tests/tasks/boards/contract.py:58-109`
- Modify: provider fake implementations and `tests/tasks/boards/test_registry.py`
- Modify: non-provider doubles that construct `RawTask` or implement `iter_raw`

- [ ] **Step 1: Add canonical lifecycle assertions to provider tests**

Use these audited semantics:

| Provider | `archived` | `terminal` |
|---|---|---|
| ClickUp | `None` | known `status.type == "closed"`; `None` when type absent |
| Asana | `None` | native boolean `completed`; `None` when absent |
| Yandex Tracker | `None` | `None` with current status projection |
| Kaiten | `condition == 2`, false for documented live `condition == 1`, otherwise `None` | state/column type `3`; `None` when absent |
| Weeek | `None`; `isDeleted` is not archive | native boolean `isCompleted`; `None` when absent |

Update missing timestamp tests to expect `None`, not `0`.

- [ ] **Step 2: Verify those focused tests fail**

Run:

```bash
.venv/bin/pytest -q \
  tests/tasks/boards/test_clickup_read.py tests/tasks/boards/test_clickup_normalize.py \
  tests/tasks/boards/test_asana_read.py tests/tasks/boards/test_asana_normalize.py \
  tests/tasks/boards/test_yandex_tracker_read.py tests/tasks/boards/test_yandex_tracker_normalize.py \
  tests/tasks/boards/test_kaiten_read.py tests/tasks/boards/test_kaiten_normalize.py \
  tests/tasks/boards/test_weeek_read.py tests/tasks/boards/test_weeek_normalize.py
```

Expected: lifecycle/timestamp assertions fail.

- [ ] **Step 3: Wrap all five listings and apply mappings**

For each provider, use the `iter_raw(..., *, sync_filter=None, now_ms=None) -> TaskListing` wrapper from Task 3 around the unchanged private generator. Return zero provider-side counts. Replace internal `RawTask.completed` reads with `RawTask.terminal`; keep native API field names such as `completed` and `isCompleted` unchanged.

- [ ] **Step 4: Upgrade the shared contract test**

In `tests/tasks/boards/contract.py`, call every provider with hints and verify the common shape:

```python
listing = provider.iter_raw(
    adapter.project,
    None,
    sync_filter=TaskSyncFilter(max_age_days=30, include_archived=False),
    now_ms=1_800_000_000_000,
)
assert isinstance(listing, TaskListing)
rows = list(listing.rows)
assert len(rows) > adapter.min_rows
assert all(row.timestamp is None or isinstance(row.timestamp, int) for row in rows)
assert all(row.archived is None or isinstance(row.archived, bool) for row in rows)
assert all(row.terminal is None or isinstance(row.terminal, bool) for row in rows)
assert listing.stats.filtered_by_age == 0
assert listing.stats.filtered_archived == 0
```

Update test provider implementations in `tests/tasks/boards/provider_fakes.py`, `tests/tasks/boards/test_registry.py`, `tests/mcp/test_finish_task.py`, `tests/mcp/test_board_provider_extensibility.py`, `tests/mcp/test_create_task.py`, `tests/mcp/test_get_board_targets.py`, `tests/entrypoints/test_check_boards.py`, and sync fakes to accept the keyword hints and return an iterable `TaskListing`.

- [ ] **Step 5: Run the full provider contract and provider suite**

Run:

```bash
.venv/bin/pytest -q tests/tasks/boards
.venv/bin/pytest -q \
  tests/mcp/test_finish_task.py \
  tests/mcp/test_board_provider_extensibility.py \
  tests/mcp/test_create_task.py \
  tests/mcp/test_get_board_targets.py \
  tests/entrypoints/test_check_boards.py
.venv/bin/ruff check reviewer/tasks/boards tests/tasks/boards
```

Expected: pass. Existing direct `list(provider.iter_raw(...))` callers remain valid through `TaskListing.__iter__`.

- [ ] **Step 6: Conditional commit checkpoint**

If commits were explicitly requested:

```bash
git add reviewer/tasks/boards/{clickup,asana,yandex_tracker,kaiten,weeek}.py \
  tests/tasks/boards/contract.py tests/tasks/boards/test_provider_contract.py \
  tests/tasks/boards/provider_fakes.py tests/tasks/boards/test_registry.py \
  tests/tasks/boards/test_{clickup_read,clickup_normalize,asana_read,asana_normalize}.py \
  tests/tasks/boards/test_{yandex_tracker_read,yandex_tracker_normalize}.py \
  tests/tasks/boards/test_{kaiten_read,kaiten_normalize,weeek_read,weeek_normalize}.py \
  tests/mcp/test_finish_task.py tests/mcp/test_board_provider_extensibility.py \
  tests/mcp/test_create_task.py tests/mcp/test_get_board_targets.py \
  tests/entrypoints/test_check_boards.py
git commit -m "refactor(tasks): мигрировать providers на retention-контракт (PRI-225)"
```

---

### Task 5: Implement Pure Retention Classification And Fingerprints

**Files:**

- Create: `reviewer/tasks/sync_filter.py`
- Create: `tests/tasks/test_sync_filter.py`

- [ ] **Step 1: Write failing pure-function tests**

Create `tests/tasks/test_sync_filter.py` with helpers and cases:

```python
from reviewer.config.task_board import TaskSyncFilter
from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.sync_filter import (
    FILTER_SEMANTICS_VERSION,
    classify_task,
    filter_fingerprint,
    filter_is_active,
)

DAY_MS = 86_400_000
NOW = 2_000_000_000_000


def _raw(*, timestamp=NOW, archived=False):
    return RawTask("ID-1", "PRI-1", "T", "", None, [], timestamp,
                   archived=archived)


def test_age_cutoff_is_inclusive():
    policy = TaskSyncFilter(max_age_days=30)
    cutoff = NOW - 30 * DAY_MS
    assert classify_task(policy, _raw(timestamp=cutoff - 1), NOW) == "filtered_by_age"
    assert classify_task(policy, _raw(timestamp=cutoff), NOW) == "eligible"
    assert classify_task(policy, _raw(timestamp=cutoff + 1), NOW) == "eligible"


def test_age_classification_precedes_archive():
    policy = TaskSyncFilter(max_age_days=30, include_archived=False)
    assert classify_task(
        policy, _raw(timestamp=NOW - 31 * DAY_MS, archived=True), NOW
    ) == "filtered_by_age"


def test_unknown_timestamp_continues_to_archive_classification():
    policy = TaskSyncFilter(max_age_days=30, include_archived=False)
    assert classify_task(policy, _raw(timestamp=None, archived=True), NOW) == "filtered_archived"
    assert classify_task(policy, _raw(timestamp=None, archived=False), NOW) == "eligible"


def test_empty_filter_is_not_active():
    assert filter_is_active(None) is False
    assert filter_is_active(TaskSyncFilter()) is False
    assert filter_is_active(TaskSyncFilter(max_age_days=1)) is True
    assert filter_is_active(TaskSyncFilter(include_archived=False)) is True


def test_filter_fingerprint_is_stable_and_versioned():
    policy = TaskSyncFilter(max_age_days=180, include_archived=False)
    current = filter_fingerprint(policy)
    assert current == (
        "sha256:cfd52772183d125dd19da907efcef6c88c6023c6b39163e70dee466383848079"
    )
    assert current == filter_fingerprint(policy)
    assert current != filter_fingerprint(
        policy, semantics_version=FILTER_SEMANTICS_VERSION + 1
    )
```

Also parametrize archive true/false/unknown and assert changes to either filter field change the fingerprint.

- [ ] **Step 2: Run and observe the missing-module failure**

Run:

```bash
.venv/bin/pytest -q tests/tasks/test_sync_filter.py
```

Expected: import failure for `reviewer.tasks.sync_filter`.

- [ ] **Step 3: Implement the pure module**

Create `reviewer/tasks/sync_filter.py`:

```python
from __future__ import annotations

import hashlib
import json
from typing import Literal

from reviewer.config.task_board import TaskSyncFilter
from reviewer.tasks.boards.base import RawTask

DAY_MS = 86_400_000
FILTER_SEMANTICS_VERSION = 1
FilterDecision = Literal["eligible", "filtered_by_age", "filtered_archived"]


def filter_is_active(sync_filter: TaskSyncFilter | None) -> bool:
    return bool(
        sync_filter is not None
        and (
            sync_filter.max_age_days is not None
            or not sync_filter.include_archived
        )
    )


def classify_task(
    sync_filter: TaskSyncFilter | None,
    raw: RawTask,
    now_ms: int,
) -> FilterDecision:
    if sync_filter is None:
        return "eligible"
    if sync_filter.max_age_days is not None and raw.timestamp is not None:
        cutoff = now_ms - sync_filter.max_age_days * DAY_MS
        if raw.timestamp < cutoff:
            return "filtered_by_age"
    if not sync_filter.include_archived and raw.archived is True:
        return "filtered_archived"
    return "eligible"


def filter_fingerprint(
    sync_filter: TaskSyncFilter | None,
    *,
    semantics_version: int = FILTER_SEMANTICS_VERSION,
) -> str | None:
    if not filter_is_active(sync_filter):
        return None
    assert sync_filter is not None
    payload = {
        "semantics_version": semantics_version,
        "sync_filter": sync_filter.canonical_dict(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
```

- [ ] **Step 4: Run pure tests and Ruff**

Run:

```bash
.venv/bin/pytest -q tests/tasks/test_sync_filter.py
.venv/bin/ruff check reviewer/tasks/sync_filter.py tests/tasks/test_sync_filter.py
```

Expected: pass.

- [ ] **Step 5: Conditional commit checkpoint**

If commits were explicitly requested:

```bash
git add reviewer/tasks/sync_filter.py tests/tasks/test_sync_filter.py
git commit -m "feat(tasks): добавить чистый retention-классификатор (PRI-225)"
```

---

### Task 6: Add Legacy-Compatible Cursor Serialization

**Files:**

- Create: `reviewer/tasks/sync_cursor.py`
- Create: `tests/tasks/test_sync_cursor.py`

- [ ] **Step 1: Write cursor codec tests**

Create tests for missing, integer, active JSON, inactive serialization, corruption, version, and fingerprint validation:

```python
from reviewer.config.task_board import TaskSyncFilter
from reviewer.tasks.sync_cursor import (
    parse_task_sync_cursor,
    serialize_task_sync_cursor,
)


def test_parse_legacy_integer_preserves_known_no_filter():
    parsed = parse_task_sync_cursor("200")
    assert parsed.cursor.watermark == 200
    assert parsed.cursor.old_filter is None
    assert parsed.cursor.old_filter_known is True
    assert parsed.warning is None


def test_inactive_filter_serializes_as_legacy_integer():
    assert serialize_task_sync_cursor(200, None) == "200"
    assert serialize_task_sync_cursor(200, TaskSyncFilter()) == "200"


def test_filtered_cursor_round_trips():
    policy = TaskSyncFilter(max_age_days=180, include_archived=False)
    encoded = serialize_task_sync_cursor(200, policy)
    parsed = parse_task_sync_cursor(encoded)
    assert parsed.cursor.watermark == 200
    assert parsed.cursor.old_filter == policy
    assert parsed.cursor.old_filter_known is True
    assert parsed.warning is None


def test_corrupt_cursor_resets_and_marks_old_filter_unknown():
    parsed = parse_task_sync_cursor("not-json-or-int")
    assert parsed.cursor.watermark == 0
    assert parsed.cursor.old_filter is None
    assert parsed.cursor.old_filter_known is False
    assert "cursor" in parsed.warning
```

Add cases for unsupported version, bool/negative watermark, missing fingerprint, and a mismatched fingerprint.

- [ ] **Step 2: Run and confirm the module is missing**

Run:

```bash
.venv/bin/pytest -q tests/tasks/test_sync_cursor.py
```

Expected: import failure.

- [ ] **Step 3: Implement the codec**

Create `reviewer/tasks/sync_cursor.py` with these public types and behavior:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from collections.abc import Mapping

from reviewer.config.task_board import (
    TaskSyncFilter,
    normalize_task_sync_filter,
)
from reviewer.tasks.sync_filter import filter_fingerprint, filter_is_active

CURSOR_VERSION = 1


@dataclass(frozen=True)
class TaskSyncCursor:
    watermark: int
    old_filter: TaskSyncFilter | None
    old_filter_known: bool
    stored_value: str | None


@dataclass(frozen=True)
class CursorParseResult:
    cursor: TaskSyncCursor
    warning: str | None = None


def _invalid(value: str | None, reason: str) -> CursorParseResult:
    return CursorParseResult(
        TaskSyncCursor(0, None, False, value),
        f"sync cursor invalid: {reason}; full backfill enabled",
    )


def parse_task_sync_cursor(value: str | None) -> CursorParseResult:
    if value is None:
        return CursorParseResult(TaskSyncCursor(0, None, True, None))
    try:
        watermark = int(value)
    except (TypeError, ValueError):
        watermark = None
    if watermark is not None:
        if watermark < 0:
            return _invalid(value, "negative legacy watermark")
        return CursorParseResult(TaskSyncCursor(watermark, None, True, value))
    try:
        payload = json.loads(value)
        if not isinstance(payload, Mapping):
            return _invalid(value, "JSON value is not an object")
        if payload.get("version") != CURSOR_VERSION:
            return _invalid(value, "unsupported version")
        raw_watermark = payload.get("watermark")
        if (
            not isinstance(raw_watermark, int)
            or isinstance(raw_watermark, bool)
            or raw_watermark < 0
        ):
            return _invalid(value, "watermark is not a non-negative integer")
        old_filter = normalize_task_sync_filter(payload.get("sync_filter"))
        expected = filter_fingerprint(old_filter)
        if not expected or payload.get("filter_fingerprint") != expected:
            return _invalid(value, "filter fingerprint mismatch")
        return CursorParseResult(
            TaskSyncCursor(raw_watermark, old_filter, True, value)
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return _invalid(value, "unparseable value")


def serialize_task_sync_cursor(
    watermark: int,
    sync_filter: TaskSyncFilter | None,
) -> str:
    if not filter_is_active(sync_filter):
        return str(watermark)
    assert sync_filter is not None
    payload = {
        "filter_fingerprint": filter_fingerprint(sync_filter),
        "sync_filter": sync_filter.canonical_dict(),
        "version": CURSOR_VERSION,
        "watermark": watermark,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
```

- [ ] **Step 4: Run codec and classifier tests**

Run:

```bash
.venv/bin/pytest -q tests/tasks/test_sync_filter.py tests/tasks/test_sync_cursor.py
.venv/bin/ruff check reviewer/tasks/sync_cursor.py tests/tasks/test_sync_cursor.py
```

Expected: pass.

- [ ] **Step 5: Conditional commit checkpoint**

If commits were explicitly requested:

```bash
git add reviewer/tasks/sync_cursor.py tests/tasks/test_sync_cursor.py
git commit -m "feat(tasks): версионировать retention-состояние курсора (PRI-225)"
```

---

### Task 7: Filter Before Normalization And Report Exact Counters

**Files:**

- Modify: `reviewer/tasks/sync.py:17-183`
- Modify: `tests/tasks/test_sync.py:8-327`

- [ ] **Step 1: Upgrade sync fakes and preserve the no-filter baseline**

Change `FakeProvider.iter_raw` to accept the keyword hints, record them, and return `TaskListing`. Record normalized/meta-normalized keys. Extend `FakeMeta` with `get_calls`, `set_calls`, and optional read/write errors.

Add:

```python
def test_no_filter_preserves_legacy_summary_and_integer_cursor():
    provider = FakeProvider([_raw("ID-1", 100), _raw("ID-2", 200)])
    tasks, meta = FakeTaskService(), FakeMeta()
    summary = SyncService([provider], tasks, meta, now_ms=lambda: 1_000).run()
    assert meta.store[("", "tasks:fake:*")] == "200"
    assert summary["eligible"] == summary["enumerated"] == 2
    assert summary["filtered_by_age"] == 0
    assert summary["filtered_archived"] == 0
    assert summary["age_unknown"] == 0
    assert summary["archive_unknown"] == 0
    assert summary["filter_applied"] is False
    assert summary["filter_fingerprint"] is None
```

Run existing `tests/tasks/test_sync.py`; expected failures are limited to the new constructor/signature until production code catches up.

- [ ] **Step 2: Add failing local-filter tests**

Add tests named:

- `test_filtered_rows_never_reach_normalize_or_normalize_meta`
- `test_provider_and_local_filter_counts_merge_exactly`
- `test_unknown_timestamp_with_age_filter_is_full_normalized`
- `test_unknown_metadata_counts_rows_but_warns_once_per_provider`
- `test_age_filtered_row_does_not_increment_archive_unknown`
- `test_by_board_contains_retention_counts_and_source`

The first test should use three rows and a high watermark, then assert:

```python
assert provider.normalized == []
assert provider.meta_normalized == ["ELIGIBLE-1"]
assert summary["enumerated"] == 3
assert summary["eligible"] == 1
assert summary["filtered_by_age"] == 1
assert summary["filtered_archived"] == 1
assert summary["enumerated"] == (
    summary["eligible"]
    + summary["filtered_by_age"]
    + summary["filtered_archived"]
)
```

- [ ] **Step 3: Run focused tests and confirm filtering is absent**

Run:

```bash
.venv/bin/pytest -q tests/tasks/test_sync.py
```

Expected: new retention assertions fail; old baseline tests continue to describe the required compatibility behavior.

- [ ] **Step 4: Implement list consumption, local classification, and counters**

Update the constructor and run signature without changing existing positional arguments:

```python
def __init__(self, providers, task_service, meta_store, *, now_ms=None) -> None:
    self._providers = [
        item if isinstance(item, SyncProvider) else SyncProvider(item)
        for item in providers
    ]
    self._tasks = task_service
    self._meta = meta_store
    self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)


def run(
    self,
    board=None,
    limit=None,
    purge_orphaned=False,
    keep_with_prs=True,
    board_type=None,
    force_renormalize=False,
    *,
    sync_filter: TaskSyncFilter | None = None,
    filter_source: str | None = None,
) -> dict:
```

Initialize these additive fields at provider and aggregate levels:

```python
"eligible": 0,
"filtered_by_age": 0,
"filtered_archived": 0,
"age_unknown": 0,
"archive_unknown": 0,
"filter_applied": filter_is_active(sync_filter),
"filter_fingerprint": filter_fingerprint(sync_filter),
"filter_source": filter_source,
```

Pass `sync_filter` to the provider only when `limit is None`; otherwise pass `None`. Consume `listing.rows`, classify before active-key insertion, and use this diagnostic logic:

```python
decision = classify_task(sync_filter, raw, run_now_ms)
age_unknown = bool(
    sync_filter is not None
    and sync_filter.max_age_days is not None
    and raw.timestamp is None
)
archive_unknown = bool(
    decision != "filtered_by_age"
    and sync_filter is not None
    and not sync_filter.include_archived
    and raw.archived is None
)
if age_unknown:
    one["age_unknown"] += 1
if archive_unknown:
    one["archive_unknown"] += 1
if decision != "eligible":
    one[decision] += 1
    continue
one["eligible"] += 1
active_keys.append(raw.key)
```

Warn once for each unknown category, not once per row. Read lazy provider stats only after the stream completes, sanitize warnings, and set:

```python
one["enumerated"] = (
    delivered_rows
    + listing.stats.filtered_by_age
    + listing.stats.filtered_archived
)
```

For no-filter unknown timestamps, retain the old effective-zero watermark behavior. When an age filter is active and timestamp is unknown, route the eligible row through full normalization.

- [ ] **Step 5: Run the sync unit suite**

Run:

```bash
.venv/bin/pytest -q tests/tasks/test_sync.py
.venv/bin/ruff check reviewer/tasks/sync.py tests/tasks/test_sync.py
```

Expected: no-filter, local filtering, exact counters, unknown metadata, and `by_board` tests pass.

- [ ] **Step 6: Conditional commit checkpoint**

If commits were explicitly requested:

```bash
git add reviewer/tasks/sync.py tests/tasks/test_sync.py
git commit -m "feat(tasks): фильтровать задачи до нормализации (PRI-225)"
```

---

### Task 8: Implement Filter Transitions, Cursor Safety, And Eligible-Only Purge

**Files:**

- Modify: `reviewer/tasks/sync.py`
- Modify: `tests/tasks/test_sync.py`
- Modify: `tests/tasks/test_sync_integration.py`

- [ ] **Step 1: Add failing transition and partial-sync tests**

Add these unit tests:

- `test_looser_age_filter_backfills_newly_eligible_row_below_watermark`
- `test_enabling_archived_tasks_backfills_archived_row_below_watermark`
- `test_removing_filter_backfills_and_writes_legacy_integer`
- `test_row_eligible_under_old_and_new_filter_uses_metadata_refresh`
- `test_natural_aging_filters_without_fingerprint_change`
- `test_unknown_old_filter_full_normalizes_every_currently_eligible_row`
- `test_filter_state_rewrites_without_marking_cursor_advanced`
- `test_limit_disables_purge_pushdown_and_all_cursor_state_writes`
- `test_limit_remains_a_raw_enumeration_cap`

For a looser-age test, seed `FakeMeta` with `serialize_task_sync_cursor(watermark, TaskSyncFilter(max_age_days=30))`, run with 180 days, and assert a 60-day-old row below watermark goes through full `normalize`.

- [ ] **Step 2: Add failing listing/cursor/purge error tests**

Add:

- `test_listing_failure_skips_index_purge_and_cursor_write`
- `test_any_incomplete_provider_skips_deploy_wide_union_purge`
- `test_cursor_read_failure_warns_and_full_backfills`
- `test_corrupt_cursor_warns_and_full_backfills`
- `test_cursor_write_failure_keeps_indexed_results_and_warns`
- `test_normalize_failure_keeps_eligible_key_active_for_purge`
- parametrized `test_purge_receives_only_eligible_keys(keep_with_prs)`

The purge assertion must be:

```python
assert task_service.purged_with == (
    ["ARCHIVE-UNKNOWN", "ELIGIBLE"],
    keep_with_prs,
    "PRI",
)
```

Filtered age/archive keys must be absent; unknown archive remains active.

- [ ] **Step 3: Run and observe transition/error failures**

Run:

```bash
.venv/bin/pytest -q tests/tasks/test_sync.py
```

Expected: transition, cursor rewrite, incomplete-listing, and eligible-only purge tests fail.

- [ ] **Step 4: Integrate cursor parsing and old/new policy comparison**

Read the cursor through `parse_task_sync_cursor`. Use the same `run_now_ms` for current and old classification. The full-normalization decision is:

```python
old_decision = (
    classify_task(cursor.old_filter, raw, run_now_ms)
    if cursor.old_filter_known
    else None
)
full_normalize = bool(
    force_renormalize
    or not cursor.old_filter_known
    or old_decision != "eligible"
    or (
        sync_filter is not None
        and sync_filter.max_age_days is not None
        and raw.timestamp is None
    )
    or (raw.timestamp is not None and raw.timestamp > cursor.watermark)
)
```

This runs only after the current filter classified the row as eligible. A legacy no-filter row with `timestamp=None` retains effective-zero metadata-refresh behavior.

Persist `serialize_task_sync_cursor(max_ts, sync_filter)` when an unlimited completed run needs a numeric advance, active-filter state change, filter removal, or corruption repair. Keep `cursor_advanced` true only for `max_ts > cursor.watermark`, not for a JSON rewrite alone. Use `limit is not None`, never truthiness, for partial mode.

- [ ] **Step 5: Make listing completion explicit and suppress unsafe purge**

Return `(active_keys, summary, complete)` from `_sync_provider`. Catch provider construction/stream iteration failures, sanitize them, discard accumulated normalize batches, return `complete=False`, and do not write cursor state.

In `run`, track `all_complete`. If any provider is incomplete, skip union purge and append one warning. Keep active eligible keys before normalization so normalization failures do not remove existing rows.

- [ ] **Step 6: Add a real metadata round-trip integration test**

Update the integration fake to return `TaskListing` and implement `normalize_meta`. Preserve the existing no-filter assertion:

```python
assert components.store.get_index_meta("", _REF) == "1000"
```

Add `test_filtered_cursor_round_trip_backfills_newly_eligible_task`:

1. Run a restrictive 30-day policy with a fixed clock.
2. Assert the stored value parses as JSON with that filter.
3. Run a 180-day policy against a row below the watermark.
4. Assert the row is reported changed/indexed and the new cursor stores the 180-day filter.

- [ ] **Step 7: Run unit, integration, and lint checks**

Run:

```bash
.venv/bin/pytest -q \
  tests/tasks/test_sync_filter.py \
  tests/tasks/test_sync_cursor.py \
  tests/tasks/test_sync.py \
  tests/tasks/test_service.py
docker compose --profile test up -d --wait paradedb-test neo4j-test
.venv/bin/pytest -q -m integration tests/tasks/test_sync_integration.py
docker compose --profile test rm -sfv paradedb-test neo4j-test
.venv/bin/ruff check reviewer/tasks/sync.py reviewer/tasks/sync_filter.py \
  reviewer/tasks/sync_cursor.py tests/tasks/test_sync.py \
  tests/tasks/test_sync_filter.py tests/tasks/test_sync_cursor.py \
  tests/tasks/test_sync_integration.py
```

Expected: all commands pass. Do not use `docker compose down -v`.

- [ ] **Step 8: Conditional commit checkpoint**

If commits were explicitly requested:

```bash
git add reviewer/tasks/sync.py tests/tasks/test_sync.py tests/tasks/test_sync_integration.py
git commit -m "feat(tasks): backfill-ить смену retention-фильтра (PRI-225)"
```

---

### Task 9: Add Server-Resolved Repo Mode To `sync_board`

**Files:**

- Modify: `reviewer/mcp/service.py:545-610,753-798`
- Modify: `reviewer/entrypoints/mcp_server.py:104-128`
- Modify: `tests/mcp/test_sync_board.py`
- Modify: `tests/mcp/test_server_tools.py`
- Modify: `tests/mcp/test_board_provider_extensibility.py`

- [ ] **Step 1: Add failing repo-mode service tests**

Add to `tests/mcp/test_sync_board.py`:

- `test_repo_mode_resolves_effective_policy_and_threads_filter_separately`
- parametrized `test_repo_mode_rejects_mixed_explicit_board_configuration_before_io`
- `test_branch_without_repo_is_configuration_error`
- `test_repo_mode_policy_failure_never_falls_back_to_deploy_sync`
- `test_repo_mode_rejects_skipped_home_layer_instead_of_syncing_lower_policy`
- `test_repo_mode_disabled_board_keeps_not_configured_error`
- `test_repo_mode_rejects_ambiguous_board_type`
- `test_explicit_mode_accepts_generic_filter_without_changing_legacy_arguments`
- `test_invalid_explicit_filter_returns_configuration_error_before_board_io`

Use a policy fixture:

```python
policy = SimpleNamespace(
    task_board={
        "type": "fake",
        "project": "PRI",
        "options": {"lane": "Backend"},
        "sync_filter": {
            "max_age_days": 30,
            "include_archived": False,
        },
    },
    task_board_warnings=[],
)
meta = SimpleNamespace(
    sources={"task_board": "home:repos/o/r.yml"},
    warnings=(),
)
```

Assert provider build options remain exactly `{"lane": "Backend"}` and `SyncService.run` receives a typed filter plus `filter_source`.

- [ ] **Step 2: Add failing FastMCP schema tests**

In `tests/mcp/test_server_tools.py`, assert `sync_board` exposes `repo`, `branch`, and `sync_filter`, keeps `provider_options`, and still excludes legacy mutation fields. Add a routing test proving repo-mode fields are forwarded by keyword.

Run:

```bash
.venv/bin/pytest -q tests/mcp/test_sync_board.py tests/mcp/test_server_tools.py
```

Expected: new schema and repo-mode tests fail.

- [ ] **Step 3: Extend the service signature without breaking positional callers**

Keep the existing positional prefix and add keyword-only fields:

```python
def sync_board(
    self,
    board: str | None = None,
    limit: int | None = None,
    purge_orphaned: bool = False,
    keep_with_prs: bool = True,
    board_type: str | None = None,
    provider_options: Mapping[str, JsonValue] | None = None,
    force_renormalize: bool = False,
    *,
    repo: str | None = None,
    branch: str | None = None,
    sync_filter: Mapping[str, JsonValue] | None = None,
    status_field: str | None = None,
) -> dict:
```

Use `BoardProviderError("configuration", message, secrets=secrets)` through `_board_error` for mode/config errors.

- [ ] **Step 4: Implement mutually exclusive repo resolution**

Use `repo is not None` to select repo mode. Reject empty repo, `branch` without repo, and any repo-mode `board`, `board_type`, `provider_options`, `sync_filter`, or `status_field` supplied with `is not None` checks.

Then:

```python
resolved = self._resolve_repo_branch(repo, branch)
if isinstance(resolved, str):
    return config_error(resolved.strip("()"))
normalized_repo, resolved_branch = resolved
try:
    policy, meta = self._resolve_policy(normalized_repo, resolved_branch)
except Exception as error:
    return config_error(
        f"effective repository policy could not be resolved: {error}"
    )
if meta.warnings:
    return config_error(
        "effective repository policy could not be resolved safely: "
        + "; ".join(meta.warnings)
    )
task_board = policy.task_board
if task_board is None:
    return {"status": "error", "reason": "task board REST is not configured"}
board = task_board.get("project")
board_type = task_board.get("type")
provider_options = task_board.get("options") or {}
raw_filter = task_board.get("sync_filter")
typed_filter = (
    normalize_task_sync_filter(raw_filter) if raw_filter is not None else None
)
filter_source = meta.sources.get("task_board", "env")
```

Require a singular string `board_type` in repo mode. Force the scoped `resolved_provider` path in repo mode; never fall into deploy-wide dispatch.

In explicit mode, retain legacy argument migration and normalize the optional explicit filter separately. Pass `filter_source="explicit"`.

- [ ] **Step 5: Expose a named public MCP schema**

In `reviewer/entrypoints/mcp_server.py`, expose repo/branch first for readability but delegate every argument by keyword:

```python
return service.sync_board(
    board=board,
    limit=limit,
    purge_orphaned=purge_orphaned,
    keep_with_prs=keep_with_prs,
    board_type=board_type,
    provider_options=provider_options,
    force_renormalize=force_renormalize,
    repo=repo,
    branch=branch,
    sync_filter=sync_filter,
)
```

- [ ] **Step 6: Run MCP and extensibility regressions**

Run:

```bash
.venv/bin/pytest -q \
  tests/mcp/test_sync_board.py \
  tests/mcp/test_server_tools.py \
  tests/mcp/test_board_provider_extensibility.py
.venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py \
  tests/mcp/test_sync_board.py tests/mcp/test_server_tools.py \
  tests/mcp/test_board_provider_extensibility.py
```

Expected: repo mode, explicit mode, schema, sanitization, closure, and provider extensibility pass.

- [ ] **Step 7: Conditional commit checkpoint**

If commits were explicitly requested:

```bash
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py \
  tests/mcp/test_sync_board.py tests/mcp/test_server_tools.py \
  tests/mcp/test_board_provider_extensibility.py
git commit -m "feat(mcp): резолвить retention policy по репозиторию (PRI-225)"
```

---

### Task 10: Update Skills, Documentation, And Capability Guards

**Files:**

- Modify: `plugin/skills/sync-tasks/SKILL.md:11-26`
- Modify: `plugin/skills/configure-review/SKILL.md:57-88`
- Modify: `tests/skills/test_sync_tasks_guardrail.py`
- Modify: `tests/skills/test_configure_review_skill.py`
- Modify: `docs/board-providers.md:7-85`
- Modify: `README.md:378-410,509-550`
- Modify: `README.ru.md:382-415,513-537`
- Modify: `.review.yml:6-14`
- Modify: `tests/docs/test_board_provider_docs.py`
- Modify: `tests/docs/test_readme_onboarding.py`
- Modify: `tests/test_review_yml_example.py`

- [ ] **Step 1: Add failing skill contract guards**

Change sync-tasks guards to require `repo=`, `branch=`, and reporting of:

```text
eligible filtered_by_age filtered_archived age_unknown archive_unknown
filter_applied filter_fingerprint filter_source by_board warnings
```

Assert the repo-mode call block does not contain `board=`, `board_type=`, `provider_options=`, or `sync_filter=`, and the skill no longer reads `.review.yml`, calls `get_board_config`, or calls `get_board_targets`.

Add configure-review tests requiring a sibling `sync_filter` block, separate age/archive questions, sibling/comment preservation, last-modified age semantics, archive distinct from done, explicit purge, filter-change backfill, and shared-project corpus warning.

- [ ] **Step 2: Run skill tests and observe failures**

Run:

```bash
.venv/bin/pytest -q \
  tests/skills/test_sync_tasks_guardrail.py \
  tests/skills/test_configure_review_skill.py
```

Expected: failures against current skill text.

- [ ] **Step 3: Rewrite sync-tasks as a repo-mode trigger**

Make the core call exactly:

```text
sync_board(repo=<canonical owner/name>, branch=<tracked target branch>,
           limit=<limit or null>,
           purge_orphaned=<explicit request or false>,
           keep_with_prs=<explicit request or true>,
           force_renormalize=<explicit request or false>)
```

State that policy/configuration errors never retry through unfiltered explicit mode. Report all retention/source/purge fields in Russian.

- [ ] **Step 4: Extend configure-review without clobbering siblings**

Add this generic sibling after `options`:

```yaml
sync_filter:
  max_age_days: <integer >= 1, or omit for no age limit>
  include_archived: <boolean, default true>
```

Require separate questions for age/archive, preservation of every other `task_board` field/comment, home per-repo as the recommended target, and no credentials.

- [ ] **Step 5: Add failing docs guards, then update docs**

Extend provider docs tests with `Archive metadata` and `Retention pushdown` columns. Record exact archive support from the provider audit:

- Exact archive: Trello (`closed`) and Kaiten (`condition`).
- Unknown archive: YouGile, YouTrack, Jira, GitHub Issues, Linear, ClickUp, Asana, Yandex Tracker, Weeek.
- Exact-count retention pushdown: unsupported for all providers in this iteration.

Update English/Russian README examples and text with defaults, inclusive cutoff, shared-project corpus, explicit purge, transition backfill, and server repo-mode resolution.

Add a commented, non-operative example to `.review.yml`:

```yaml
  # sync_filter:
  #   max_age_days: 180
  #   include_archived: false
```

Do not activate retention for this repository.

- [ ] **Step 6: Run user-facing contract tests**

Run:

```bash
.venv/bin/pytest -q \
  tests/skills/test_sync_tasks_guardrail.py \
  tests/skills/test_configure_review_skill.py \
  tests/docs/test_board_provider_docs.py \
  tests/docs/test_readme_onboarding.py \
  tests/test_review_yml_example.py
```

Expected: pass.

- [ ] **Step 7: Conditional commit checkpoint**

If commits were explicitly requested, delay this commit until generated plugin files are refreshed in Task 11 so source and digest remain atomic.

---

### Task 11: Regenerate Plugin Metadata And Run Final Verification

**Files:**

- Generate: `.codex-plugin/plugin.json`
- Generate: `plugin/.codex-plugin/plugin.json`
- Generate: `plugin/.claude-plugin/plugin.json`
- Generate/check: `plugin/assets/icon.svg`
- Verify all files changed by Tasks 1–10

- [ ] **Step 1: Regenerate plugin metadata from source**

Run:

```bash
.venv/bin/python scripts/update_codex_plugin_manifest.py
.venv/bin/python scripts/update_codex_plugin_manifest.py --check
```

Expected: generation succeeds; the read-only check prints no errors. Never hand-edit the digest/version fields.

- [ ] **Step 2: Run focused subsystem suites**

Run:

```bash
.venv/bin/pytest -q \
  tests/config/test_task_board_config.py \
  tests/config/test_layers.py \
  tests/policy/test_policy.py \
  tests/tasks/test_sync_filter.py \
  tests/tasks/test_sync_cursor.py \
  tests/tasks/test_sync.py \
  tests/tasks/boards \
  tests/mcp/test_sync_board.py \
  tests/mcp/test_server_tools.py \
  tests/mcp/test_board_provider_extensibility.py \
  tests/skills/test_sync_tasks_guardrail.py \
  tests/skills/test_configure_review_skill.py \
  tests/docs/test_board_provider_docs.py \
  tests/docs/test_readme_onboarding.py \
  tests/test_review_yml_example.py \
  tests/install/test_codex_plugin_payload.py \
  tests/test_ci_gates.py
```

Expected: zero failures.

- [ ] **Step 3: Run repository-wide quality gates**

Run:

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
.venv/bin/python scripts/update_codex_plugin_manifest.py --check
git diff --check
```

Expected: all commands exit zero. If an unrelated pre-existing failure appears, record the exact command and failure without altering unrelated user changes.

- [ ] **Step 4: Audit acceptance criteria against observable evidence**

Confirm from tests and output:

1. No-filter sync writes the legacy integer cursor and indexes exactly as before.
2. Filtered rows never call `normalize` or `normalize_meta`.
3. Different project policies resolve from independent home per-repo files.
4. Unlimited scoped purge receives only eligible keys and respects `keep_with_prs`.
5. Partial sync writes no cursor/filter state and performs no purge.
6. Looser/removed filters full-normalize newly eligible rows below watermark.
7. Aggregate and `by_board` summaries satisfy exact reason-count invariants.
8. Generic provider contract and YouGile unknown-archive behavior are covered.
9. Skills, README files, provider docs, root comments, and plugin manifests agree.

- [ ] **Step 5: Conditional final commit checkpoint**

If commits were explicitly requested, inspect `git status`, `git diff`, and recent commit style, then stage only PRI-225 files and commit the remaining user-facing/generated changes:

```bash
git add plugin/skills/sync-tasks/SKILL.md plugin/skills/configure-review/SKILL.md \
  docs/board-providers.md README.md README.ru.md .review.yml \
  tests/skills/test_sync_tasks_guardrail.py tests/skills/test_configure_review_skill.py \
  tests/docs/test_board_provider_docs.py tests/docs/test_readme_onboarding.py \
  tests/test_review_yml_example.py .codex-plugin/plugin.json \
  plugin/.codex-plugin/plugin.json plugin/.claude-plugin/plugin.json \
  plugin/assets/icon.svg
git commit -m "docs(tasks): описать retention-фильтр и обновить plugin (PRI-225)"
```

Do not amend, force-push, or stage unrelated worktree changes.
