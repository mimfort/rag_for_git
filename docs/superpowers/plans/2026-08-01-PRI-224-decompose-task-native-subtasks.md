# PRI-224 Native Subtask Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** добавить `rag-reviewer:decompose-task` и durable server-side `create_subtasks`, которые после одного preview/confirm создают до 20 нативных подзадач YouGile без дублей при повторе с тем же idempotency key.

**Architecture:** отдельный `SubtaskService` выполняет resumable state machine поверх fail-closed Postgres ledger и parent-scoped advisory lock; YouGile предоставляет optional native-subtask capability, а остальные providers отклоняются до write. Parent/child links хранятся authoritative snapshot в Postgres и Neo4j, а terminal `complete` ставится только после targeted write-through обоих слоёв.

**Tech Stack:** Python 3.11+, Pydantic v2, FastMCP, httpx, psycopg/psycopg-pool, PostgreSQL/JSONB/advisory locks, Neo4j Cypher, pytest, pytest integration markers, Markdown plugin skills.

---

## Source Of Truth

- Brief: `docs/superpowers/briefs/2026-08-01-PRI-224-decompose-task-native-subtasks.md`
- Approved design: `docs/superpowers/specs/2026-08-01-PRI-224-decompose-task-native-subtasks-design.md`
- Design commit: `4f8d4fa`
- Work from a feature branch/worktree created at execution time; do not implement directly on `dev`.
- Preserve unrelated untracked files already present in the original workspace.

## File Structure

**Create:**

- `reviewer/tasks/subtasks.py` — validated request, stable hashes/markers, result types and resumable orchestration.
- `reviewer/tasks/subtask_store.py` — fail-closed lazy Postgres ledger, revision CAS and parent advisory-lock lifecycle.
- `reviewer/tasks/subtask_store.sql` — durable operation table without TTL.
- `tests/tasks/test_subtasks.py` — pure contracts and state-machine unit tests.
- `tests/tasks/test_subtask_store.py` — pool/schema/CAS/lock unit tests.
- `tests/tasks/test_subtask_store_integration.py` — restart and multi-connection advisory-lock integration tests.
- `tests/tasks/boards/test_yougile_subtasks.py` — native child create/reconcile/attach REST tests.
- `tests/mcp/test_create_subtasks.py` — MCP lifecycle, replay/conflict, sanitization and targeted write-through tests.
- `tests/index/test_tasks_links_roundtrip.py` — Postgres links migration/round-trip integration coverage.
- `plugin/skills/decompose-task/SKILL.md` — provider-agnostic parent-context, preview, confirm, batch and verification flow.
- `tests/skills/test_decompose_task_skill.py` — static safety contract for the new skill.

**Modify:**

- `reviewer/index/schema.sql` — additive `tasks.links` migration.
- `reviewer/tasks/store.py` — links persistence with absent-versus-empty semantics.
- `reviewer/tasks/graph.py` — atomic outgoing `TASK_LINK` snapshot replacement.
- `reviewer/tasks/service.py` — full-index link snapshots and `get_task(...).links`.
- `reviewer/tasks/boards/base.py` — native-subtask identities and optional protocol.
- `reviewer/tasks/boards/registry.py` — immutable capability declarations and capability-specific validation.
- `reviewer/tasks/boards/runtime.py` — carry registry-owned capabilities through provider lifecycle.
- `reviewer/tasks/boards/yougile.py` — canonical child links, source placement and native primitives.
- `reviewer/mcp/schemas.py` — strict `SubtaskIn`.
- `reviewer/mcp/service.py` — `create_subtasks`, strict callback and capabilities discovery.
- `reviewer/entrypoints/mcp_server.py` — 38th FastMCP tool and public schemas/docs.
- `reviewer/app.py` — lazy ledger/service wiring using keyword-based `Components` construction.
- `pyproject.toml` — package `reviewer.tasks/*.sql`.
- Existing focused tests under `tests/tasks/`, `tests/mcp/`, `tests/docs/`, and `tests/test_app_wiring.py`.
- `README.md`, `README.ru.md`, `AGENTS.md`, `plugin/README.md`, `docs/board-providers.md` — skill/tool/capability docs.
- `.codex-plugin/plugin.json`, `plugin/.codex-plugin/plugin.json` — generated plugin payload hashes after skill addition.

## Global Constraints

- Follow TDD for every behavior change: red test, minimal implementation, green test, commit.
- Unit tests must not use real sockets, localhost, real Postgres/Neo4j or board credentials.
- Integration tests use only `paradedb-test` and `neo4j-test`; remove those services with the safe targeted command, never `down -v`.
- The same idempotency key never automatically resends a persisted `in_flight` item. Ambiguous writes favor no duplicates over liveness.
- Persist only sanitized warnings; a provider secret must never enter operation JSON, result JSON or logs.
- `links` omitted means preserve; `links=[]` means authoritative clear.
- `normalize_meta` remains link-blind.
- YouGile is the sole `native_subtasks` provider in this plan.
- Do not add compatibility aliases for the new skill; the canonical name is `rag-reviewer:decompose-task`.

## Spec Coverage Matrix

| Approved requirement | Plan tasks |
|---|---|
| Strict 1..20 full-TaskDoc request and stable hash/marker | 1, 8 |
| Authoritative Postgres links and Neo4j snapshot replacement | 2, 9 |
| Optional registry capability; unsupported before write | 3, 8 |
| YouGile source placement, canonical child identities and marker reconciliation | 4 |
| Durable global idempotency ledger and parent advisory lock | 5, 9 |
| Checkpoint-before-POST, no resend of ambiguous `in_flight` items | 6 |
| Existing+new UUID union, no-op attach, `board_complete -> complete` | 7 |
| MCP lifecycle, strict write-through, sanitization, 38th tool | 8 |
| Skill preview/confirm, same-key partial retry, sync and verification | 10 |
| Provider/docs/manifests and final verification | 10, 11, 12 |

---

### Task 1: Pure Request And Hash Contracts

**Files:**
- Create: `reviewer/tasks/subtasks.py`
- Create: `tests/tasks/test_subtasks.py`

- [ ] **Step 1: Write failing request-validation and hashing tests**

Create `tests/tasks/test_subtasks.py` with these initial tests:

```python
import pytest

from reviewer.tasks.subtasks import (
    MAX_SUBTASKS,
    marker_for,
    validate_subtask_request,
)


CHILD = {
    "title": "Добавить protocol",
    "problem": "Нет native-subtask write contract.",
    "steps": ["Добавить protocol", "Подключить YouGile"],
    "criteria": ["Unsupported provider не пишет в board"],
    "context": "PRI-224",
}


def _request(**overrides):
    values = {
        "parent_key": "PRI-224",
        "subtasks": [CHILD],
        "idempotency_key": "3f986f4e-dab0-41d0-89f5-c30f47f3d686",
        "board_type": "yougile",
        "project": "PRI",
        "provider_options": {},
    }
    values.update(overrides)
    return validate_subtask_request(**values)


@pytest.mark.parametrize("field", ["parent_key", "idempotency_key"])
def test_request_rejects_blank_identity_fields(field):
    with pytest.raises(ValueError, match=field):
        _request(**{field: "  "})


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"title": ""}, "title"),
        ({"problem": "  "}, "problem"),
        ({"steps": []}, "steps"),
        ({"criteria": [" "]}, "criteria"),
    ],
)
def test_request_requires_autonomous_children(patch, message):
    child = {**CHILD, **patch}
    with pytest.raises(ValueError, match=message):
        _request(subtasks=[child])


def test_request_rejects_batch_outside_one_to_twenty():
    with pytest.raises(ValueError, match="1..20"):
        _request(subtasks=[])
    with pytest.raises(ValueError, match="1..20"):
        _request(subtasks=[CHILD] * (MAX_SUBTASKS + 1))


def test_request_hash_is_canonical_but_child_order_is_significant():
    first = _request(provider_options={"lane": "Backend", "nested": {"b": 2, "a": 1}})
    same = _request(provider_options={"nested": {"a": 1, "b": 2}, "lane": "Backend"})
    second_child = {**CHILD, "title": "Добавить MCP tool"}
    reordered = _request(subtasks=[second_child, CHILD])
    ordered = _request(subtasks=[CHILD, second_child])
    assert first.request_hash == same.request_hash
    assert reordered.request_hash != ordered.request_hash


def test_marker_is_stable_lowercase_hex_and_item_specific():
    request = _request()
    first = marker_for("yougile", "parent-uuid", request.idempotency_key, 0, request.subtasks[0])
    second = marker_for("yougile", "parent-uuid", request.idempotency_key, 1, request.subtasks[0])
    assert first.startswith("reviewer-subtask:")
    assert len(first.removeprefix("reviewer-subtask:")) == 64
    assert first == first.lower()
    assert first != second
```

- [ ] **Step 2: Run the tests and confirm the module is missing**

Run: `.venv/bin/pytest -q tests/tasks/test_subtasks.py`

Expected: collection fails with `ModuleNotFoundError: reviewer.tasks.subtasks`.

- [ ] **Step 3: Add the complete pure request contract**

Create `reviewer/tasks/subtasks.py` with the following public contract; keep this task free of I/O:

```python
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

MAX_SUBTASKS = 20
SUBTASK_MARKER_RE = re.compile(r"reviewer-subtask:[0-9a-f]{64}")
SubtaskPhase = Literal["pending", "in_flight", "created", "attached"]
OperationStatus = Literal["running", "partial", "board_complete", "complete"]


def _text(value: Any, field: str) -> str:
    result = value.strip() if isinstance(value, str) else ""
    if not result:
        raise ValueError(f"{field} must not be blank")
    return result


def _items(value: Any, field: str) -> tuple[str, ...]:
    result = tuple(item.strip() for item in (value or []) if isinstance(item, str) and item.strip())
    if not result:
        raise ValueError(f"{field} must contain a nonblank item")
    return result


def _optional_text(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text or null")
    return value.strip() or None


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


@dataclass(frozen=True)
class SubtaskDraft:
    title: str
    problem: str
    steps: tuple[str, ...]
    criteria: tuple[str, ...]
    context: str | None

    def payload(self) -> dict:
        return {
            "title": self.title,
            "problem": self.problem,
            "steps": list(self.steps),
            "criteria": list(self.criteria),
            "context": self.context,
        }


@dataclass(frozen=True)
class SubtaskRequest:
    parent_key: str
    subtasks: tuple[SubtaskDraft, ...]
    idempotency_key: str
    board_type: str | None
    project: str | None
    provider_options: dict
    request_hash: str


def validate_subtask_request(
    *,
    parent_key: str,
    subtasks: list[dict],
    idempotency_key: str,
    board_type: str | None,
    project: str | None,
    provider_options: dict | None,
) -> SubtaskRequest:
    parent_key = _text(parent_key, "parent_key")
    idempotency_key = _text(idempotency_key, "idempotency_key")
    if not 1 <= len(subtasks or []) <= MAX_SUBTASKS:
        raise ValueError("subtasks must contain 1..20 items")
    drafts = tuple(
        SubtaskDraft(
            title=_text(item.get("title"), "title"),
            problem=_text(item.get("problem"), "problem"),
            steps=_items(item.get("steps"), "steps"),
            criteria=_items(item.get("criteria"), "criteria"),
            context=_optional_text(item.get("context"), "context"),
        )
        for item in subtasks
    )
    payload = {
        "parent_key": parent_key,
        "subtasks": [item.payload() for item in drafts],
        "idempotency_key": idempotency_key,
        "board_type": board_type,
        "project": project,
        "provider_options": provider_options or {},
    }
    digest = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()
    return SubtaskRequest(
        parent_key,
        drafts,
        idempotency_key,
        board_type,
        project,
        dict(provider_options or {}),
        digest,
    )


def marker_for(
    board_type: str,
    parent_task_id: str,
    idempotency_key: str,
    index: int,
    draft: SubtaskDraft,
) -> str:
    item_hash = hashlib.sha256(_canonical_json(draft.payload()).encode()).hexdigest()
    source = "\0".join((board_type, parent_task_id, idempotency_key, str(index), item_hash))
    return "reviewer-subtask:" + hashlib.sha256(source.encode()).hexdigest()
```

- [ ] **Step 4: Run the pure contract tests**

Run: `.venv/bin/pytest -q tests/tasks/test_subtasks.py`

Expected: all tests pass.

- [ ] **Step 5: Commit the pure contract**

```bash
git add reviewer/tasks/subtasks.py tests/tasks/test_subtasks.py
git commit -m "feat(tasks): задать контракт декомпозиции (PRI-224)"
```

---

### Task 2: Authoritative Links In Postgres And Neo4j

**Files:**
- Modify: `reviewer/index/schema.sql:54-76`
- Modify: `reviewer/tasks/store.py:50-187`
- Modify: `reviewer/tasks/graph.py:41-60`
- Modify: `reviewer/tasks/service.py:28-250,307-330`
- Modify: `reviewer/entrypoints/mcp_server.py:194-200`
- Modify: `tests/tasks/test_service.py`
- Modify: `tests/tasks/test_service_batch.py`
- Modify: `tests/tasks/test_graph.py:38-52`

- [ ] **Step 1: Add failing tri-state link tests**

Add focused assertions to the existing task-service fakes and these tests:

```python
def test_index_task_updates_links_when_hash_is_unchanged(service):
    service._store.hashes["ID-1"] = task_content_hash("same")
    task = _brief("ID-1", description="same", links=[{"key": "ID-2", "type": "subtask"}])
    result = service.index_task(task)
    assert service._store.link_updates == [("ID-1", [{"key": "ID-2", "type": "subtask"}])]
    assert service._graph.replaced_links == [("ID-1", [{"key": "ID-2", "type": "subtask"}])]
    assert result["links_stored"] is True


def test_index_task_explicit_empty_links_clears_snapshot(service):
    service.index_task(_brief("ID-1", links=[]))
    assert service._store.link_updates == [("ID-1", [])]
    assert service._graph.replaced_links == [("ID-1", [])]


def test_index_task_missing_links_preserves_snapshot(service):
    task = _brief("ID-1")
    task.pop("links")
    service.index_task(task)
    assert service._store.link_updates == []
    assert service._graph.replaced_links == []


def test_refresh_meta_batch_does_not_touch_links(service):
    service.refresh_meta_batch([{"key": "ID-1", "title": "T", "links": []}])
    assert service._store.link_updates == []
    assert service._graph.replaced_links == []
```

Replace the graph empty no-op test with authoritative replacement tests:

```python
def test_replace_links_empty_still_deletes_outgoing_snapshot():
    driver = _FakeDriver()
    assert TaskGraph(driver).replace_links("ID-1", []) == 0
    assert len(driver.calls) == 1
    query, params = driver.calls[0]
    assert "DELETE old" in query
    assert params["rows"] == []


def test_replace_links_filters_keyless_and_counts():
    driver = _FakeDriver()
    count = TaskGraph(driver).replace_links("ID-1", [
        {"key": "ID-2", "title": "child", "type": "subtask"},
        {"title": "missing key"},
    ])
    assert count == 1
    query, params = driver.calls[0]
    assert "MATCH (t)-[old:TASK_LINK]->()" in query
    assert params["rows"] == [{"key": "ID-2", "title": "child", "type": "subtask"}]
```

- [ ] **Step 2: Run focused tests and verify stale semantics fail**

Run: `.venv/bin/pytest -q tests/tasks/test_service.py tests/tasks/test_service_batch.py tests/tasks/test_graph.py`

Expected: failures show missing `update_links`, `replace_links`, `links_stored`, and `get_task(...).links`.

- [ ] **Step 3: Add the additive schema and lazy forward migration**

Append to `reviewer/index/schema.sql` after `attachments`:

```sql
-- PRI-224: authoritative TaskBrief links snapshot; не входит в embedding/content_hash.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS links jsonb NOT NULL DEFAULT '[]';
```

In `reviewer/tasks/store.py`, add a separate schema lock so the migration never recursively
re-enters `_init_lock` while opening the pool:

```python
_LINKS_SCHEMA = "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS links jsonb NOT NULL DEFAULT '[]'"

def _ensure_links_schema(self) -> None:
    pool = self._ensure_pool()
    if not self._links_schema_ready:
        with self._schema_lock:
            if not self._links_schema_ready:
                with pool.connection() as conn:
                    conn.execute(_LINKS_SCHEMA)
                    conn.commit()
                self._links_schema_ready = True

def _connect(self):
    self._ensure_links_schema()
    return self._ensure_pool().connection()
```

Initialize `_schema_lock = threading.Lock()` and `_links_schema_ready = False` in
`TaskStore.__init__`; reset only `_links_schema_ready` in `close()`.

- [ ] **Step 4: Persist and read links independently from content hashes**

Add `links: list[dict] | None = None` to `TaskRow`, include `links` in `get_task`, and add:

```python
def update_links(self, key: str, links: list[dict]) -> bool:
    with self._connect() as conn:
        result = conn.execute(
            "UPDATE tasks SET links=%s::jsonb WHERE key=%s",
            (json.dumps(links, ensure_ascii=False), key),
        )
        conn.commit()
    return result.rowcount == 1
```

Include `links` in full INSERT/UPSERT. Use `TaskRow.links is None` as the omission sentinel:

```sql
links = CASE
  WHEN %(links_supplied)s THEN EXCLUDED.links
  ELSE tasks.links
END
```

For a new row, store `[]` when omitted; on conflict, preserve the old value when omitted.

- [ ] **Step 5: Replace only outgoing graph links in one query**

Add `TaskGraph.replace_links` and keep `upsert_links` for older direct callers:

```python
def replace_links(self, key: str, links: list[dict]) -> int:
    rows = [
        {"key": link["key"], "title": link.get("title") or "",
         "type": link.get("type") or "relates"}
        for link in links
        if isinstance(link, dict) and link.get("key")
    ]
    self._driver.execute_query(
        "MATCH (t:Task {key: $key}) "
        "OPTIONAL MATCH (t)-[old:TASK_LINK]->() "
        "DELETE old "
        "WITH DISTINCT t "
        "CALL (t) { "
        "  UNWIND $rows AS lk "
        "  MERGE (n:Task {key: lk.key}) "
        "    ON CREATE SET n.title=lk.title, n.codes=[lk.key] "
        "  MERGE (t)-[:TASK_LINK {type: lk.type}]->(n) "
        "  RETURN count(*) AS merged "
        "} "
        "RETURN t.key",
        key=key,
        rows=rows,
    )
    return len(rows)
```

- [ ] **Step 6: Wire authoritative snapshots through `TaskService`**

In both `index_task` and `index_batch`, carry `links_supplied = "links" in task`, filter the
snapshot, and run store/graph updates independently of `embedded/meta_only`:

```python
links_supplied = "links" in task
links = [
    link for link in (task.get("links") or [])
    if isinstance(link, dict) and link.get("key")
]

links_stored = None
if links_supplied:
    try:
        links_stored = self._store.update_links(key, links)
        if not links_stored:
            warnings.append("store: task links row was not updated")
    except Exception as error:
        links_stored = False
        warnings.append(f"store links: {type(error).__name__}: {error}")

if self._graph is not None and links_supplied:
    links_upserted = self._graph.replace_links(key, links)
```

Return `links_stored` in each result. Leave `refresh_meta_batch` unchanged. Add `links` to
`TaskService.get_task` and to the FastMCP `get_task` result docstring.

- [ ] **Step 7: Run the focused suites**

Run: `.venv/bin/pytest -q tests/tasks/test_service.py tests/tasks/test_service_batch.py tests/tasks/test_graph.py`

Expected: all tests pass.

- [ ] **Step 8: Commit authoritative links**

```bash
git add reviewer/index/schema.sql reviewer/tasks/store.py reviewer/tasks/graph.py \
  reviewer/tasks/service.py reviewer/entrypoints/mcp_server.py \
  tests/tasks/test_service.py tests/tasks/test_service_batch.py tests/tasks/test_graph.py
git commit -m "feat(tasks): сохранять authoritative связи задач (PRI-224)"
```

---

### Task 3: Optional Native-Subtask Provider Capability

**Files:**
- Modify: `reviewer/tasks/boards/base.py:26-123`
- Modify: `reviewer/tasks/boards/registry.py:56-217`
- Modify: `reviewer/tasks/boards/runtime.py:15-108`
- Modify: `reviewer/mcp/service.py:725-751`
- Modify: `reviewer/entrypoints/mcp_server.py` (`get_board_targets` docstring)
- Modify: `tests/tasks/boards/test_registry.py`
- Modify: `tests/mcp/test_get_board_targets.py`

- [ ] **Step 1: Add failing capability tests**

Extend `_CompleteProvider` in `tests/tasks/boards/test_registry.py` only inside a capable subclass:

```python
class _NativeProvider(_CompleteProvider):
    def reconcile_native_subtasks(self, source_board_id, markers):
        return []

    def create_native_subtask(self, doc_md, *, title, source_column_id, marker):
        raise AssertionError("not called")

    def replace_native_subtasks(self, parent_task_id, subtask_ids):
        raise AssertionError("not called")


def test_registry_requires_native_methods_only_when_capability_is_declared():
    ordinary = BoardProviderRegistry([fake_spec()])
    assert ordinary.create(
        "fake", credentials={"FAKE_TOKEN": "x"}, options={}, build_defaults=BUILD_DEFAULTS
    )

    capable = replace(
        fake_spec(factory=lambda _: _NativeProvider()),
        capabilities=frozenset({"native_subtasks"}),
    )
    assert BoardProviderRegistry([capable]).create(
        "fake", credentials={"FAKE_TOKEN": "x"}, options={}, build_defaults=BUILD_DEFAULTS
    )


def test_registry_rejects_unknown_capability():
    with pytest.raises(ValueError, match="unknown provider capability"):
        BoardProviderRegistry([replace(fake_spec(), capabilities=frozenset({"magic"}))])


def test_registry_closes_provider_missing_declared_capability_method():
    provider = _CompleteProvider()
    provider.closed = False
    provider.close = lambda: setattr(provider, "closed", True)
    spec = replace(
        fake_spec(factory=lambda _: provider),
        capabilities=frozenset({"native_subtasks"}),
    )
    registry = BoardProviderRegistry([spec])
    with pytest.raises(TypeError, match="reconcile_native_subtasks"):
        registry.create(
            "fake", credentials={"FAKE_TOKEN": "x"}, options={}, build_defaults=BUILD_DEFAULTS
        )
    assert provider.closed is True
```

Add a `get_board_targets` test whose provider tries to spoof capabilities and assert registry data wins.

- [ ] **Step 2: Run capability tests and verify failure**

Run: `.venv/bin/pytest -q tests/tasks/boards/test_registry.py tests/mcp/test_get_board_targets.py`

Expected: failures mention missing `capabilities` and capability-specific validation.

- [ ] **Step 3: Add native identity types and the optional protocol**

Append to `reviewer/tasks/boards/base.py`:

```python
@dataclass(frozen=True)
class NativeSubtaskIdentity:
    board_id: str
    key: str
    title: str
    aliases: tuple[str, ...] = ()
    url: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconciledNativeSubtask:
    marker: str
    identity: NativeSubtaskIdentity


class NativeSubtaskProvider(Protocol):
    def reconcile_native_subtasks(
        self, source_board_id: str, markers: frozenset[str]
    ) -> list[ReconciledNativeSubtask]: ...

    def create_native_subtask(
        self, doc_md: str, *, title: str, source_column_id: str, marker: str
    ) -> NativeSubtaskIdentity: ...

    def replace_native_subtasks(
        self, parent_task_id: str, subtask_ids: list[str]
    ) -> None: ...
```

- [ ] **Step 4: Validate registry-owned capabilities**

Add to `BoardProviderSpec` and registry validation:

```python
capabilities: frozenset[str] = frozenset()

_CAPABILITY_PROVIDER_MEMBERS = {
    "native_subtasks": (
        "reconcile_native_subtasks",
        "create_native_subtask",
        "replace_native_subtasks",
    ),
}
```

Reject non-`frozenset` values and unknown names in `_validate_spec`; after common runtime validation,
validate only methods associated with declared capabilities.

- [ ] **Step 5: Carry capabilities through runtime and target discovery**

Extend `ResolvedProvider` and yield construction:

```python
@dataclass(frozen=True)
class ResolvedProvider:
    board_type: str
    provider: TaskBoardProvider
    secrets: frozenset[str]
    capabilities: frozenset[str]

# in resolved_provider
yield ResolvedProvider(board_type, provider, secrets, spec.capabilities)
```

In `get_board_targets`, place authoritative fields after provider result:

```python
return self._safe_board_payload(
    {
        **result,
        "board_type": resolved.board_type,
        "project": project,
        "capabilities": sorted(resolved.capabilities),
    },
    resolved.secrets,
)
```

- [ ] **Step 6: Run and commit capability support**

Run: `.venv/bin/pytest -q tests/tasks/boards/test_registry.py tests/mcp/test_get_board_targets.py tests/mcp/test_board_provider_extensibility.py`

Expected: all tests pass and existing fake providers need no native methods.

```bash
git add reviewer/tasks/boards/base.py reviewer/tasks/boards/registry.py \
  reviewer/tasks/boards/runtime.py reviewer/mcp/service.py \
  reviewer/entrypoints/mcp_server.py tests/tasks/boards/test_registry.py \
  tests/mcp/test_get_board_targets.py
git commit -m "feat(boards): добавить optional native-subtask capability (PRI-224)"
```

---

### Task 4: Canonical YouGile Links And Native REST Primitives

**Files:**
- Modify: `reviewer/tasks/boards/yougile.py:60-598`
- Modify: `tests/tasks/boards/test_yougile_normalize.py`
- Modify: `tests/tasks/boards/test_yougile_fetch_one.py`
- Modify: `tests/tasks/boards/fakes/yougile.py`
- Create: `tests/tasks/boards/test_yougile_subtasks.py`

- [ ] **Step 1: Write failing canonical-normalization tests**

Add:

```python
def test_subtask_link_uses_canonical_common_key():
    raw = _raw(subtask_ids=["child-uuid"])
    brief = normalize_yougile(
        raw,
        r"PRI-\d+",
        "https://b/#{code}",
        {"child-uuid": {"key": "ID-9", "title": "Child"}},
    )
    assert {"type": "subtask", "key": "ID-9", "title": "Child"} in brief["links"]
    assert all(link["key"] != "child-uuid" for link in brief["links"])


def test_normalize_strips_exact_marker_but_preserves_lookalike():
    exact = "reviewer-subtask:" + "a" * 64
    raw = _raw(description=f"<p>Body</p><small>{exact}</small><p>reviewer-subtask:not-a-hash</p>")
    brief = normalize_yougile(raw, "", "")
    assert exact not in brief["description"]
    assert "reviewer-subtask:not-a-hash" in brief["description"]
```

Add `fetch_one` assertions for `source_board_id` and `source_column_id` in `provider_data`.

- [ ] **Step 2: Write failing native REST tests**

Create `tests/tasks/boards/test_yougile_subtasks.py` around the reusable MockTransport fake:

```python
def test_create_native_subtask_uses_fixed_column_and_marker(board, state):
    marker = "reviewer-subtask:" + "b" * 64
    identity = board.create_native_subtask(
        "## Проблема\n\nТекст",
        title="Child",
        source_column_id="source-column",
        marker=marker,
    )
    request = state.requests[-2]
    assert request.method == "POST"
    assert request.url.path.endswith("/tasks")
    body = request_json(request)
    assert body["columnId"] == "source-column"
    assert marker in body["description"]
    assert identity.board_id == "child-uuid"
    assert identity.key == "ID-9"
    assert identity.aliases == ("PRI-9",)


def test_reconcile_searches_all_source_board_columns(board, state):
    marker = "reviewer-subtask:" + "c" * 64
    state.tasks_by_column["old-column"] = [_child(marker=marker)]
    matches = board.reconcile_native_subtasks("source-board", frozenset({marker}))
    assert [(item.marker, item.identity.board_id) for item in matches] == [(marker, "child-uuid")]


def test_replace_native_subtasks_sends_exact_union(board, state):
    board.replace_native_subtasks("parent-uuid", ["old", "new"])
    request = state.requests[-1]
    assert request.method == "PUT"
    assert request_json(request) == {"subtasks": ["old", "new"]}
```

- [ ] **Step 3: Run YouGile tests and verify missing methods/data**

Run: `.venv/bin/pytest -q tests/tasks/boards/test_yougile_normalize.py tests/tasks/boards/test_yougile_fetch_one.py tests/tasks/boards/test_yougile_subtasks.py`

Expected: failures show old UUID links, missing source metadata and missing native methods.

- [ ] **Step 4: Canonicalize subtask references and strip exact markers**

Change `normalize_yougile` to accept `subtask_refs` and use canonical keys:

```python
description = SUBTASK_MARKER_RE.sub("", raw.description or "")
for subtask_id in raw.subtask_ids:
    ref = subtask_refs.get(subtask_id) or {}
    child_key = ref.get("key") or subtask_id
    link = {"type": "subtask", "key": child_key}
    if ref.get("title"):
        link["title"] = ref["title"]
    links.append(link)
    covered.update((subtask_id, child_key))
```

Build refs in `YougileBoard.normalize` from child GET responses instead of packing key into title.

- [ ] **Step 5: Preserve source board/column metadata**

Set `provider_data` in both listing and point-read paths:

```python
provider_data={
    "source_board_id": board_id,
    "source_column_id": column_id,
}
```

In `fetch_one`, retain the full column response so `boardId` is available.

- [ ] **Step 6: Implement native primitives and declare capability**

Declare `capabilities=frozenset({"native_subtasks"})` in `provider_spec` and add:

```python
def create_native_subtask(
    self, doc_md: str, *, title: str, source_column_id: str, marker: str
) -> NativeSubtaskIdentity:
    description = md_to_html(doc_md) + f"<p><small>{marker}</small></p>"
    created = self._write(
        "POST", "/tasks",
        json={"title": title, "columnId": source_column_id, "description": description},
    ) or {}
    board_id = str(created.get("id") or "")
    if not board_id:
        raise BoardProviderError("unsupported", "YouGile create response has no task id.")
    warnings: list[str] = []
    try:
        task = self._read(f"/tasks/{quote(board_id, safe='')}") or {}
    except Exception:
        task = {}
        warnings.append("canonical child key is unavailable; using board UUID")
    key = str(task.get("idTaskCommon") or board_id)
    project_key = str(task.get("idTaskProject") or "")
    aliases = (project_key,) if project_key and project_key != key else ()
    url_code = project_key or key
    url = self._url_template.replace("{code}", url_code) if self._url_template else None
    return NativeSubtaskIdentity(board_id, key, title, aliases, url, tuple(warnings))


def replace_native_subtasks(self, parent_task_id: str, subtask_ids: list[str]) -> None:
    self._write(
        "PUT",
        f"/tasks/{quote(parent_task_id, safe='')}",
        json={"subtasks": subtask_ids},
    )
```

Implement `reconcile_native_subtasks` by listing all columns for `source_board_id`, paging tasks,
extracting exact markers, and returning one `ReconciledNativeSubtask` per match. Return duplicate
matches rather than selecting one; the state machine will mark duplicate markers manual-required.

- [ ] **Step 7: Extend the HTTP fake for committed-then-timeout behavior**

Add mutable children, `tasks_by_column`, parent subtasks and a handler branch that stores a child
before raising `httpx.ReadTimeout`. The test must assert one POST and later marker discovery.

- [ ] **Step 8: Run and commit YouGile support**

Run: `.venv/bin/pytest -q tests/tasks/boards/test_yougile_normalize.py tests/tasks/boards/test_yougile_fetch_one.py tests/tasks/boards/test_yougile_subtasks.py tests/tasks/boards/test_registry.py`

Expected: all tests pass and only YouGile declares `native_subtasks`.

```bash
git add reviewer/tasks/boards/yougile.py tests/tasks/boards/test_yougile_normalize.py \
  tests/tasks/boards/test_yougile_fetch_one.py tests/tasks/boards/test_yougile_subtasks.py \
  tests/tasks/boards/fakes/yougile.py
git commit -m "feat(yougile): добавить нативные подзадачи (PRI-224)"
```

---

### Task 5: Durable Operation Store And Parent Advisory Lock

**Files:**
- Create: `reviewer/tasks/subtask_store.sql`
- Create: `reviewer/tasks/subtask_store.py`
- Create: `tests/tasks/test_subtask_store.py`
- Modify: `pyproject.toml:96-100`
- Modify: `tests/test_schema_encoding.py`

- [ ] **Step 1: Write fail-closed store unit tests**

Create tests covering lazy schema, revision CAS, lock lifecycle and error propagation:

```python
def test_store_initializes_schema_lazily(fake_pool):
    store = SubtaskOperationStore("postgresql://unused", pool_factory=lambda **_: fake_pool)
    assert fake_pool.opened is False
    assert store.load("missing") is None
    assert fake_pool.opened is True
    assert any("CREATE TABLE IF NOT EXISTS subtask_operations" in call.sql for call in fake_pool.calls)


def test_checkpoint_rejects_stale_revision(store):
    row = store.insert(_operation(revision=0))
    updated = store.checkpoint(replace(row, status="partial"), expected_revision=0)
    assert updated.revision == 1
    with pytest.raises(OperationConflictError):
        store.checkpoint(replace(row, status="complete"), expected_revision=0)


def test_parent_lock_is_fail_closed_when_connection_dies(store):
    with store.try_parent_lock("yougile", "parent-uuid") as lock:
        assert lock is not None
        lock.connection.closed = True
        with pytest.raises(LedgerUnavailableError):
            lock.ensure_alive()
```

- [ ] **Step 2: Run store tests and verify missing module**

Run: `.venv/bin/pytest -q tests/tasks/test_subtask_store.py`

Expected: collection fails with `ModuleNotFoundError`.

- [ ] **Step 3: Add the operation schema**

Create `reviewer/tasks/subtask_store.sql`:

```sql
CREATE TABLE IF NOT EXISTS subtask_operations (
    idempotency_key  text PRIMARY KEY,
    board_type       text NOT NULL,
    parent_input_key text NOT NULL,
    parent_task_id   text NOT NULL,
    source_board_id  text NOT NULL,
    source_column_id text NOT NULL,
    request_hash     text NOT NULL,
    request_payload  jsonb NOT NULL,
    state            jsonb NOT NULL,
    status           text NOT NULL CHECK (
        status IN ('running', 'partial', 'board_complete', 'complete')
    ),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
```

Package it with:

```toml
"reviewer.tasks" = ["*.sql"]
```

- [ ] **Step 4: Implement immutable operation rows and revision CAS**

Create the store around a lazy `ConnectionPool` like `SessionStore`, but do not catch database
errors. Define the insert/read type and persist a `revision` integer inside `state`:

```python
@dataclass(frozen=True)
class SubtaskOperation:
    idempotency_key: str
    board_type: str
    parent_input_key: str
    parent_task_id: str
    source_board_id: str
    source_column_id: str
    request_hash: str
    request_payload: dict
    state: dict
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def revision(self) -> int:
        return int(self.state.get("revision", 0))


class SubtaskOperationStore:
    def __init__(
        self,
        pg_dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 4,
        pool_factory=ConnectionPool,
    ) -> None:
        self.pg_dsn = pg_dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool_factory = pool_factory
        self._pool = None
        self._init_lock = threading.Lock()
        self._schema_ready = False
```

Checkpoint with:

```sql
UPDATE subtask_operations
SET state=%s::jsonb, status=%s, updated_at=now()
WHERE idempotency_key=%s
  AND COALESCE((state->>'revision')::bigint, 0)=%s
RETURNING idempotency_key, board_type, parent_input_key, parent_task_id,
          source_board_id, source_column_id, request_hash, request_payload,
          state, status, created_at, updated_at
```

Raise `OperationConflictError` when no row returns. `load` and `insert` return a frozen
`SubtaskOperation` with a `revision` property.

- [ ] **Step 5: Implement a session-level parent lock**

Use a dedicated checked-out connection for the entire context:

```python
@contextmanager
def try_parent_lock(self, board_type: str, parent_task_id: str):
    pool = self._ensure_pool()
    with pool.connection() as connection:
        acquired = connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, hashtextextended(%s, 0)))",
            (board_type, parent_task_id),
        ).fetchone()[0]
        connection.commit()
        if not acquired:
            yield None
            return
        lock = ParentOperationLock(connection)
        try:
            yield lock
        finally:
            connection.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, hashtextextended(%s, 0)))",
                (board_type, parent_task_id),
            )
            connection.commit()
```

`ParentOperationLock.ensure_alive()` executes `SELECT 1` on the same connection and raises
`LedgerUnavailableError` on any failure.

- [ ] **Step 6: Run store unit tests and schema encoding guard**

Run: `.venv/bin/pytest -q tests/tasks/test_subtask_store.py tests/test_schema_encoding.py`

Expected: all tests pass.

- [ ] **Step 7: Commit the durable store**

```bash
git add reviewer/tasks/subtask_store.py reviewer/tasks/subtask_store.sql \
  tests/tasks/test_subtask_store.py tests/test_schema_encoding.py pyproject.toml
git commit -m "feat(tasks): добавить durable ledger подзадач (PRI-224)"
```

---

### Task 6: Resumable Child Reconciliation And Creation

**Files:**
- Modify: `reviewer/tasks/subtasks.py`
- Modify: `tests/tasks/test_subtasks.py`

- [ ] **Step 1: Add failing state-machine tests for child writes**

Add fakes that record `checkpoint`, `ensure_alive`, `reconcile`, and POST order, then add:

```python
def test_fresh_run_checkpoints_in_flight_before_post(harness):
    result = harness.run()
    assert harness.events[:3] == [
        ("checkpoint", 0, "in_flight"),
        ("lock", "alive"),
        ("post", 0),
    ]
    assert result.created[0].board_id == "child-0"


def test_in_flight_item_reconciles_without_second_post(harness):
    harness.operation = harness.operation_with_phase(0, "in_flight")
    harness.reconciled = {harness.marker(0): harness.identity(0)}
    result = harness.run()
    assert harness.provider.create_calls == []
    assert result.created[0].board_id == "child-0"


def test_unresolved_in_flight_item_is_manual_required(harness):
    harness.operation = harness.operation_with_phase(0, "in_flight")
    result = harness.run()
    assert harness.provider.create_calls == []
    assert result.pending[0].phase == "in_flight"
    assert result.pending[0].manual_required is True
```

- [ ] **Step 2: Run the tests and verify missing service behavior**

Run: `.venv/bin/pytest -q tests/tasks/test_subtasks.py -k 'in_flight or fresh_run'`

Expected: failures show missing `SubtaskService` and state/result types.

- [ ] **Step 3: Add persisted item and public result types**

Add frozen child/result types and JSON serialization for operation state:

```python
@dataclass(frozen=True)
class SubtaskChildResult:
    index: int
    title: str
    key: str | None
    aliases: tuple[str, ...]
    board_id: str | None
    url: str | None
    phase: SubtaskPhase
    manual_required: bool = False


@dataclass(frozen=True)
class WriteThroughResult:
    success: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubtaskBatchResult:
    status: Literal["ok", "partial", "error"]
    board_type: str
    parent_key: str
    idempotency_key: str
    resumed: bool
    created: tuple[SubtaskChildResult, ...] = ()
    attached: tuple[SubtaskChildResult, ...] = ()
    unattached: tuple[SubtaskChildResult, ...] = ()
    pending: tuple[SubtaskChildResult, ...] = ()
    warnings: tuple[str, ...] = ()
    reindexed: bool = False
    category: str | None = None
    retryable: bool | None = None

    def payload(self) -> dict:
        return asdict(self)
```

Add `asdict` to the existing `dataclasses` import. Use `payload()` at the MCP boundary; never
serialize provider exceptions directly.

- [ ] **Step 4: Implement reconcile-before-create transitions**

Add this stable service boundary before implementing transitions:

```python
@dataclass(frozen=True)
class SubtaskPreflight:
    operation: SubtaskOperation | None
    result: SubtaskBatchResult | None


class SubtaskService:
    def __init__(self, store: SubtaskOperationStore) -> None:
        self._store = store

    def preflight(self, request: SubtaskRequest) -> SubtaskPreflight:
        operation = self._store.load(request.idempotency_key)
        if operation is None:
            return SubtaskPreflight(None, None)
        if operation.request_hash != request.request_hash:
            return SubtaskPreflight(operation, _conflict_result(request))
        if operation.status == "complete":
            return SubtaskPreflight(operation, _result_from_operation(operation, resumed=True))
        return SubtaskPreflight(operation, None)

    def run(
        self,
        request: SubtaskRequest,
        *,
        operation: SubtaskOperation | None,
        provider: NativeSubtaskProvider,
        board_type: str,
        write_through: Callable[
            [RawTask, tuple[NativeSubtaskIdentity, ...]], WriteThroughResult
        ],
        sanitize: Callable[[object], str],
    ) -> SubtaskBatchResult:
        if operation is None:
            parent = provider.fetch_one(request.parent_key)
            if parent is None:
                return _error_result(request, board_type, "parent_not_found", False)
            operation = _new_operation(request, board_type, parent)
        with self._store.try_parent_lock(board_type, operation.parent_task_id) as lock:
            if lock is None:
                return _busy_result(request, board_type)
            current = self._store.load(request.idempotency_key)
            if current is None:
                current = self._store.insert(operation)
            return self._run_locked(
                request,
                current,
                provider=provider,
                lock=lock,
                write_through=write_through,
                sanitize=sanitize,
            )
```

Implement `_new_operation`, `_error_result`, `_busy_result` and `_run_locked` in the same module;
the child transitions below and parent transitions in Task 7 are the complete `_run_locked` flow.
Do not change this public signature in later tasks. Inside the acquired parent lock:

```python
reconciled = provider.reconcile_native_subtasks(
    operation.source_board_id,
    frozenset(item["marker"] for item in items if item["phase"] == "in_flight"),
)
by_marker: dict[str, list[NativeSubtaskIdentity]] = {}
for match in reconciled:
    by_marker.setdefault(match.marker, []).append(match.identity)

for item in items:
    matches = by_marker.get(item["marker"], [])
    if len(matches) == 1:
        item.update(_identity_state(matches[0], phase="created"))
        operation = store.checkpoint(_with_items(operation, items), operation.revision)
    elif len(matches) > 1:
        item["manual_required"] = True
        item["warnings"].append("multiple board cards contain the same idempotency marker")
```

For a `pending` item, checkpoint `in_flight`, call `lock.ensure_alive()`, then call the provider.
On success checkpoint `created`. On any exception after the POST call begins, keep persisted
`in_flight`, store only `sanitize(error)`, and never route it back to POST.

- [ ] **Step 5: Run child-state tests**

Run: `.venv/bin/pytest -q tests/tasks/test_subtasks.py -k 'in_flight or fresh_run or child_failure'`

Expected: all selected tests pass and the event sequence proves checkpoint-before-write.

- [ ] **Step 6: Commit child orchestration**

```bash
git add reviewer/tasks/subtasks.py tests/tasks/test_subtasks.py
git commit -m "feat(tasks): возобновлять создание подзадач (PRI-224)"
```

---

### Task 7: Parent Union, Write-Through And Terminal Replay

**Files:**
- Modify: `reviewer/tasks/subtasks.py`
- Modify: `tests/tasks/test_subtasks.py`

- [ ] **Step 1: Add failing attachment/completion tests**

Add:

```python
def test_parent_union_preserves_existing_order_and_deduplicates(harness):
    harness.parent.subtask_ids = ["old", "child-1"]
    harness.identities = [harness.identity(0, "child-0"), harness.identity(1, "child-1")]
    result = harness.run()
    assert harness.provider.replace_calls == [
        ("parent-uuid", ["old", "child-1", "child-0"])
    ]
    assert [child.board_id for child in result.attached] == ["child-0", "child-1"]


def test_existing_union_skips_put_but_becomes_board_complete(harness):
    harness.parent.subtask_ids = ["child-0"]
    result = harness.run()
    assert harness.provider.replace_calls == []
    assert result.status == "ok"
    assert harness.statuses[-2:] == ["board_complete", "complete"]


def test_write_through_failure_stays_board_complete(harness):
    harness.write_through_result = WriteThroughResult(False, ("graph unavailable",))
    result = harness.run()
    assert result.status == "partial"
    assert result.category == "reindex_pending"
    assert result.reindexed is False
    assert harness.operation.status == "board_complete"


def test_completed_replay_uses_no_provider_or_callback(harness):
    harness.operation = harness.completed_operation()
    result = harness.preflight()
    assert result.status == "ok"
    assert harness.provider.calls == []
    assert harness.write_through_calls == []
```

- [ ] **Step 2: Run completion tests and verify failures**

Run: `.venv/bin/pytest -q tests/tasks/test_subtasks.py -k 'union or write_through or replay'`

Expected: failures show missing parent merge, terminal status and replay paths.

- [ ] **Step 3: Implement stable union and verified attachment**

Add a pure helper and use it after rereading parent:

```python
def merge_subtask_ids(existing: list[str], created: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for task_id in [*existing, *created]:
        if task_id and task_id not in seen:
            seen.add(task_id)
            result.append(task_id)
    return result
```

Skip PUT when the exact expected union is already present. Otherwise verify the lock, PUT the
snapshot, refetch parent and mark attached only if every expected UUID is present.

- [ ] **Step 4: Keep terminal completion behind strict write-through**

Checkpoint `board_complete`, call the injected callback while holding the parent lock, and only
checkpoint `complete` when `WriteThroughResult.success` is true:

```python
operation = store.checkpoint(replace(operation, status="board_complete"), operation.revision)
indexed = write_through(parent, tuple(confirmed_identities))
if not indexed.success:
    return _result(
        operation,
        status="partial",
        category="reindex_pending",
        retryable=True,
        reindexed=False,
        extra_warnings=indexed.warnings,
    )
operation = store.checkpoint(replace(operation, status="complete"), operation.revision)
return _result(operation, status="ok", reindexed=True)
```

Add `preflight(request)` that loads by global key, returns a complete replay, raises conflict on
hash mismatch, and returns the incomplete row for provider-backed resume.

- [ ] **Step 5: Run the full state-machine suite**

Run: `.venv/bin/pytest -q tests/tasks/test_subtasks.py`

Expected: all tests pass, including busy parent, lock loss, partial attachment and replay.

- [ ] **Step 6: Commit completion semantics**

```bash
git add reviewer/tasks/subtasks.py tests/tasks/test_subtasks.py
git commit -m "feat(tasks): завершать batch после write-through (PRI-224)"
```

---

### Task 8: MCP Lifecycle, Strict Schema And App Wiring

**Files:**
- Modify: `reviewer/mcp/schemas.py`
- Modify: `reviewer/mcp/service.py`
- Modify: `reviewer/entrypoints/mcp_server.py`
- Modify: `reviewer/app.py`
- Create: `tests/mcp/test_create_subtasks.py`
- Modify: `tests/mcp/test_schemas.py`
- Modify: `tests/mcp/test_server.py`
- Modify: `tests/mcp/test_server_tools.py`
- Modify: `tests/mcp/test_get_board_targets.py`
- Modify: `tests/test_app_wiring.py`

- [ ] **Step 1: Add failing strict-schema and tool-registration tests**

Add to `tests/mcp/test_schemas.py`:

```python
def test_subtask_in_strips_lists_and_rejects_blank_required_fields():
    item = SubtaskIn(
        title=" Child ", problem=" Problem ",
        steps=[" Step ", " "], criteria=[" Criterion ", " "], context=None,
    )
    assert item.title == "Child"
    assert item.steps == ["Step"]
    with pytest.raises(ValidationError):
        SubtaskIn(title="x", problem="p", steps=[], criteria=["c"])
```

Update server tests to expect 38 tools and a typed `create_subtasks` schema with max 20 children.

- [ ] **Step 2: Add failing MCP lifecycle tests**

Create `tests/mcp/test_create_subtasks.py` with fake components/provider and these contracts:

```python
def test_completed_replay_returns_before_provider_factory(service):
    service.subtasks.preflight_result = _complete_result()
    result = service.mcp.create_subtasks(**REQUEST)
    assert result["status"] == "ok"
    assert service.provider_factories == []


def test_same_key_different_payload_conflicts_before_provider_call(service):
    service.subtasks.preflight_result = _conflict_result()
    result = service.mcp.create_subtasks(**REQUEST)
    assert result["category"] == "conflict"
    assert service.provider.board_writes == []


def test_unsupported_provider_performs_no_board_write(service):
    service.provider.capabilities = frozenset()
    result = service.mcp.create_subtasks(**REQUEST)
    assert result["category"] == "unsupported"
    assert service.provider.board_writes == []
    assert service.provider.closed is True


def test_targeted_write_through_indexes_parent_and_children_once(service):
    result = service.mcp.create_subtasks(**REQUEST)
    assert result["reindexed"] is True
    assert len(service.task_service.batch_calls) == 1
    assert [task["key"] for task in service.task_service.batch_calls[0]] == ["ID-224", "ID-301"]


def test_partial_result_and_checkpoint_do_not_persist_provider_secret(service):
    service.provider.secret = "server-secret-value"
    service.provider.fail_message = "failed with server-secret-value"
    result = service.mcp.create_subtasks(**REQUEST)
    assert "server-secret-value" not in str(result)
    assert "server-secret-value" not in str(service.operation_store.saved_states)
    assert service.provider.closed is True
```

- [ ] **Step 3: Run MCP tests and verify missing public surface**

Run: `.venv/bin/pytest -q tests/mcp/test_schemas.py tests/mcp/test_create_subtasks.py tests/mcp/test_server.py tests/test_app_wiring.py`

Expected: failures show missing schema, service method, tool and components.

- [ ] **Step 4: Add strict `SubtaskIn`**

Implement in `reviewer/mcp/schemas.py`:

```python
from typing import Annotated

from reviewer.tasks.subtasks import MAX_SUBTASKS


class SubtaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    problem: str
    steps: list[str]
    criteria: list[str]
    context: str | None = None

    @field_validator("title", "problem")
    @classmethod
    def _required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("steps", "criteria")
    @classmethod
    def _required_items(cls, value: list[str]) -> list[str]:
        result = [item.strip() for item in value if item.strip()]
        if not result:
            raise ValueError("must contain a nonblank item")
        return result


SubtasksIn = Annotated[list[SubtaskIn], Field(min_length=1, max_length=MAX_SUBTASKS)]
```

- [ ] **Step 5: Wire lazy operation components**

Add `subtask_operation_store` and `subtask_service` to `Components`, instantiate them from PG
settings, and convert the final constructor to keyword arguments:

```python
subtask_operation_store = SubtaskOperationStore(
    settings.pg_dsn,
    min_size=settings.pg_pool_min_size,
    max_size=settings.pg_pool_max_size,
)
subtask_service = SubtaskService(subtask_operation_store)

return Components(
    settings=settings,
    store=store,
    graph=graph,
    embedder=embedder,
    reranker=reranker,
    retriever=retriever,
    task_store=task_store,
    task_graph=task_graph,
    task_service=task_service,
    sync_service=sync_service,
    summary_store=summary_store,
    subtask_operation_store=subtask_operation_store,
    subtask_service=subtask_service,
)
```

- [ ] **Step 6: Implement strict targeted write-through**

Add `_write_through_subtasks` to fetch/normalize parent and confirmed children, then call exactly
one `index_batch`. Treat any warning, `links_stored is not True` for supplied links, or graph
warning as failure:

```python
results = self.components.task_service.index_batch(briefs)
warnings = [warning for result in results for warning in (result.get("warnings") or [])]
success = (
    len(results) == len(briefs)
    and not warnings
    and all(result.get("links_stored") is True for result in results)
)
return WriteThroughResult(success, tuple(warnings))
```

Do not call the old fail-soft single-task `_write_through` for this feature.

- [ ] **Step 7: Implement `MCPReviewService.create_subtasks` in the approved order**

Validate/hash, call `subtask_service.preflight`, return terminal replay/conflict before provider
creation, then use `resolved_provider`. Reject missing capability before writes, build a sanitizer
from `resolved.secrets`, and call the state machine. Always pass the final dict through
`_safe_board_payload`.

- [ ] **Step 8: Register the 38th FastMCP tool**

Import `SubtasksIn` from `reviewer.mcp.schemas` and add next to `create_task`:

```python
@mcp.tool()
def create_subtasks(
    parent_key: str,
    subtasks: SubtasksIn,
    idempotency_key: str,
    board_type: str | None = None,
    project: str | None = None,
    provider_options: dict[str, object] | None = None,
) -> dict:
    """Create and attach a confirmed native-subtask batch with durable idempotency."""
    return service.create_subtasks(
        parent_key,
        [item.model_dump() for item in subtasks],
        idempotency_key,
        board_type,
        project,
        provider_options,
    )
```

Update the count from 37 to 38 and include `create_subtasks` in generic board-tool schema guards.

- [ ] **Step 9: Run and commit MCP wiring**

Run: `.venv/bin/pytest -q tests/mcp/test_schemas.py tests/mcp/test_create_subtasks.py tests/mcp/test_get_board_targets.py tests/mcp/test_server.py tests/mcp/test_server_tools.py tests/test_app_wiring.py`

Expected: all tests pass.

```bash
git add reviewer/mcp/schemas.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py \
  reviewer/app.py tests/mcp/test_schemas.py tests/mcp/test_create_subtasks.py \
  tests/mcp/test_get_board_targets.py tests/mcp/test_server.py \
  tests/mcp/test_server_tools.py tests/test_app_wiring.py
git commit -m "feat(mcp): добавить create_subtasks (PRI-224)"
```

---

### Task 9: Postgres/Neo4j Restart And Snapshot Integration

**Files:**
- Create: `tests/index/test_tasks_links_roundtrip.py`
- Create: `tests/tasks/test_subtask_store_integration.py`
- Modify: `tests/tasks/test_integration.py`

- [ ] **Step 1: Add integration tests before starting infrastructure**

Add tests marked `@pytest.mark.integration`:

```python
import uuid

import pytest

from reviewer.config.settings import Settings
from reviewer.tasks.subtask_store import SubtaskOperation, SubtaskOperationStore

pytestmark = pytest.mark.integration


def _operation(*, idempotency_key: str, status: str) -> SubtaskOperation:
    return SubtaskOperation(
        idempotency_key=idempotency_key,
        board_type="yougile",
        parent_input_key="PRI-224",
        parent_task_id="parent-uuid",
        source_board_id="board-1",
        source_column_id="column-1",
        request_hash="hash",
        request_payload={"subtasks": []},
        state={"revision": 0, "items": []},
        status=status,
    )


def test_new_store_instance_reads_completed_operation_after_restart():
    pg_dsn = Settings().pg_dsn
    key = f"integration-{uuid.uuid4()}"
    first = SubtaskOperationStore(pg_dsn)
    first.insert(_operation(idempotency_key=key, status="complete"))
    first.close()
    second = SubtaskOperationStore(pg_dsn)
    try:
        assert second.load(key).status == "complete"
    finally:
        with second._connect() as connection:
            connection.execute("DELETE FROM subtask_operations WHERE idempotency_key=%s", (key,))
            connection.commit()
        second.close()


def test_same_parent_lock_is_rejected_across_keys():
    pg_dsn = Settings().pg_dsn
    first = SubtaskOperationStore(pg_dsn)
    second = SubtaskOperationStore(pg_dsn)
    with first.try_parent_lock("yougile", "parent") as held:
        assert held is not None
        with second.try_parent_lock("yougile", "parent") as blocked:
            assert blocked is None
    first.close()
    second.close()
```

Add links round-trip and graph replacement tests using unique task keys; clean only rows/edges
created by each test.

- [ ] **Step 2: Start isolated test infrastructure**

Run: `docker compose --profile test up -d --wait paradedb-test neo4j-test`

Expected: both test services report healthy.

- [ ] **Step 3: Run focused integration tests**

Run: `.venv/bin/pytest -q -m integration tests/tasks/test_subtask_store_integration.py tests/index/test_tasks_links_roundtrip.py tests/tasks/test_integration.py`

Expected: all selected integration tests pass.

- [ ] **Step 4: Stop only isolated test services**

Run: `docker compose --profile test rm -sfv paradedb-test neo4j-test`

Expected: only `paradedb-test` and `neo4j-test` are removed.

- [ ] **Step 5: Commit integration coverage**

```bash
git add tests/tasks/test_subtask_store_integration.py \
  tests/index/test_tasks_links_roundtrip.py tests/tasks/test_integration.py
git commit -m "test(tasks): проверить durable native-subtask flow (PRI-224)"
```

---

### Task 10: `decompose-task` Skill And Safety Guards

**Files:**
- Create: `plugin/skills/decompose-task/SKILL.md`
- Create: `tests/skills/test_decompose_task_skill.py`
- Modify: `README.md`
- Modify: `README.ru.md`
- Modify: `.codex-plugin/plugin.json` (generated)
- Modify: `plugin/.codex-plugin/plugin.json` (generated)

- [ ] **Step 1: Write failing static skill tests**

Create:

```python
from pathlib import Path

ROOT = Path(__file__).parents[2]
SKILL = ROOT / "plugin/skills/decompose-task/SKILL.md"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_decompose_task_uses_store_first_context_before_draft():
    text = _text()
    assert "get_task(parent_key" in text
    assert "get_task_context(" in text
    assert "search_tasks(" in text
    assert "search_codebase(" in text
    assert text.index("get_task(parent_key") < text.index("Preview")


def test_decompose_task_has_one_confirmation_before_one_batch_write():
    text = _text()
    assert text.count("create_subtasks(") == 1
    assert "explicit confirmation" in text
    assert text.index("explicit confirmation") < text.index("create_subtasks(")
    assert "create_task(" not in text


def test_decompose_task_reuses_key_and_verifies_after_sync():
    text = _text()
    assert "same idempotency_key" in text
    assert "same payload" in text
    assert "sync_board(" in text
    assert "get_task(parent_key" in text
    assert "get_task_context(" in text
    assert "get_task(child" in text


def test_decompose_task_is_provider_agnostic():
    text = _text().lower()
    assert "native_subtasks" in text
    assert "yougile" not in text
    assert "reply in russian" in text
```

- [ ] **Step 2: Run the guard and verify the skill is absent**

Run: `.venv/bin/pytest -q tests/skills/test_decompose_task_skill.py`

Expected: tests fail because `plugin/skills/decompose-task/SKILL.md` does not exist.

- [ ] **Step 3: Write the complete skill flow**

Create `plugin/skills/decompose-task/SKILL.md` with frontmatter and these explicit phases:

```markdown
---
name: decompose-task
description: Decompose one board task into confirmed native subtasks with durable idempotency.
---

# Decompose Task

Reply in Russian. Resolve `task_board` exactly once and use only generic type/project/options.

1. Read the parent with `get_task(parent_key, project=<project>)`. On miss only, call one scoped
   `sync_board(...)` and retry `get_task` once.
2. Call `get_board_targets(...)`; require `native_subtasks` in `capabilities`. Unsupported is a
   no-write stop.
3. Gather `get_task_context`, `search_tasks`, and `search_codebase` before drafting.
4. Draft 1-20 children. Every child must have nonblank title/problem, steps, criteria, and optional
   context. Generate one opaque UUID idempotency key using the local runtime.
5. Preview provider, parent, idempotency key, and the complete canonical body of every child.
   Perform no board write before one explicit confirmation of the whole preview.
6. After confirmation call exactly one `create_subtasks(...)` with the previewed payload and key.
7. On partial result, show created/attached/unattached/pending/warnings. A retry must use the same
   payload and same idempotency_key; never invent a replacement key for an in-flight item.
8. After any confirmed child, call scoped `sync_board(...)`, then verify parent links with
   `get_task(parent_key, ...)`, graph links with `get_task_context(...)`, and each returned child
   with `get_task(child_key, ...)`.
```

- [ ] **Step 4: Add bilingual skill references and regenerate payload manifests**

Add the full `/rag-reviewer:decompose-task` reference to both root READMEs, including preview,
single confirmation, same-key retry and post-write verification. Update their MCP tool count to
38. Then run:

```bash
.venv/bin/python scripts/update_codex_plugin_manifest.py
.venv/bin/python scripts/update_codex_plugin_manifest.py --check
```

Expected: root/plugin Codex manifests carry the new payload digest and the check exits 0.

- [ ] **Step 5: Run skill, naming and payload guards**

Run: `.venv/bin/pytest -q tests/skills/test_decompose_task_skill.py tests/skills/test_skill_names.py tests/install/test_codex_plugin_payload.py`

Expected: all skill and payload tests pass.

- [ ] **Step 6: Commit the skill and generated payload**

```bash
git add plugin/skills/decompose-task/SKILL.md tests/skills/test_decompose_task_skill.py \
  README.md README.ru.md .codex-plugin/plugin.json plugin/.codex-plugin/plugin.json
git commit -m "feat(plugin): добавить skill декомпозиции задач (PRI-224)"
```

---

### Task 11: Documentation, Provider Matrix And Generated Manifests

**Files:**
- Modify: `AGENTS.md`
- Modify: `plugin/README.md`
- Modify: `docs/board-providers.md`
- Modify: `tests/docs/test_board_provider_docs.py`

- [ ] **Step 1: Add failing docs assertions**

Extend docs tests to require:

```python
def test_board_provider_matrix_documents_native_subtasks():
    text = (ROOT / "docs/board-providers.md").read_text(encoding="utf-8")
    assert "native subtasks" in text.lower()
    assert "create_subtasks" in text
    assert "YouGile" in text


def test_root_docs_list_decompose_task():
    for path in ("README.md", "README.ru.md", "AGENTS.md", "plugin/README.md"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "decompose-task" in text, path
```

- [ ] **Step 2: Run docs/install guards and verify missing references**

Run: `.venv/bin/pytest -q tests/docs/test_board_provider_docs.py tests/install/test_codex_plugin_payload.py tests/skills/test_skill_names.py`

Expected: failures name missing `decompose-task`, matrix capability, stale tool count or payload hash.

- [ ] **Step 3: Update provider and plugin documentation**

Document:

- YouGile-only native write capability, with all other providers unsupported;
- result buckets and visible technical idempotency marker;
- tool count 38 in every guarded reference.

Add `rag-reviewer:decompose-task` to `AGENTS.md` and the plugin skill list.

- [ ] **Step 4: Run docs/install tests**

Run: `.venv/bin/pytest -q tests/docs/test_board_provider_docs.py tests/install/test_codex_plugin_payload.py tests/skills/test_skill_names.py tests/skills/test_decompose_task_skill.py`

Expected: all tests pass.

- [ ] **Step 5: Commit provider/plugin docs**

```bash
git add AGENTS.md plugin/README.md docs/board-providers.md \
  tests/docs/test_board_provider_docs.py
git commit -m "docs(plugin): описать декомпозицию задач (PRI-224)"
```

---

### Task 12: Final Verification And Review Gate

**Files:**
- Verify all changed production, test, plugin, generated and documentation files.

- [ ] **Step 1: Run all focused unit suites**

Run:

```bash
.venv/bin/pytest -q \
  tests/tasks/test_subtasks.py \
  tests/tasks/test_subtask_store.py \
  tests/tasks/test_service.py \
  tests/tasks/test_service_batch.py \
  tests/tasks/test_graph.py \
  tests/tasks/boards/test_registry.py \
  tests/tasks/boards/test_yougile_subtasks.py \
  tests/tasks/boards/test_yougile_normalize.py \
  tests/tasks/boards/test_yougile_fetch_one.py \
  tests/mcp/test_schemas.py \
  tests/mcp/test_create_subtasks.py \
  tests/mcp/test_get_board_targets.py \
  tests/mcp/test_server.py \
  tests/mcp/test_server_tools.py \
  tests/test_app_wiring.py \
  tests/skills/test_decompose_task_skill.py
```

Expected: all selected tests pass.

- [ ] **Step 2: Run the complete unit suite**

Run: `.venv/bin/pytest -q`

Expected: exit 0 with no failures; integration tests remain excluded by project config.

- [ ] **Step 3: Run isolated integration suites**

Run:

```bash
docker compose --profile test up -d --wait paradedb-test neo4j-test
.venv/bin/pytest -q -m integration \
  tests/tasks/test_subtask_store_integration.py \
  tests/index/test_tasks_links_roundtrip.py \
  tests/tasks/test_integration.py
docker compose --profile test rm -sfv paradedb-test neo4j-test
```

Expected: selected integration tests pass; only test-profile services are removed.

- [ ] **Step 4: Run lint and generated-manifest checks**

Run:

```bash
.venv/bin/ruff check .
.venv/bin/python scripts/update_codex_plugin_manifest.py --check
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 5: Inspect implementation history and working tree**

Run:

```bash
git status --short
git log --oneline --decorate -15
git diff dev...HEAD --stat
```

Expected: only intended PRI-224 changes are committed; unrelated original workspace files were
never staged or modified.

- [ ] **Step 6: Request code review before integration**

Invoke `superpowers:requesting-code-review` against the full `dev...HEAD` diff. Resolve concrete
findings with new commits, rerun the affected focused tests, then rerun Steps 2-4. Do not amend
existing commits.
