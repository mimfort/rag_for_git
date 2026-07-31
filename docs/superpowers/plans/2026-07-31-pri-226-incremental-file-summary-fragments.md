# PRI-226 Incremental File Summary Fragments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Пересуммаризировать только added/changed файлы subsystem-кластера, атомарно переиспользовать file fragments и сохранить совместимый cluster-level read API.

**Architecture:** Чистая delta-модель сравнивает current path fingerprints с сохранёнными fragments. MCP read tool выдаёт ограниченный work payload, skill генерирует только недостающие fragments и итоговый текст, а SummaryStore одной транзакцией принимает весь bundle после повторной optimistic-проверки aggregate hash.

**Tech Stack:** Python 3.11–3.13, FastMCP, Pydantic v2, psycopg/ParadeDB, pgvector, pytest, Markdown skills.

## Global Constraints

- Fingerprint файла строится только из текущих `(node_id, skeleton_hash)`; full-content invalidation не добавлять.
- `get_subsystem_summaries`, ANN-query и существующие cluster-level поля остаются обратно совместимыми.
- LLM orchestration остаётся в глобальном plugin skill; reviewer-server не вызывает LLM.
- Hash mismatch или неполный fragment payload не пишет fragments, summary или embedding.
- Новый source hash никогда не сохраняет embedding старого summary; Voyage вызывается только после успешного summary commit.
- Смена effective depth вызывает полный rebuild; завершённый depth фиксируется только после полного uncapped prune.
- Все новые сообщения, комментарии и докстринги — по-русски.
- Unit-тесты не используют внешнюю сеть или localhost; DB-проверки маркируются `integration`.

---

### Task 1: Pure file fingerprint and delta model

**Files:**

- Modify: `reviewer/graph/summaries.py`
- Create: `reviewer/services/summary_fragments.py`
- Modify: `tests/graph/test_summaries.py`
- Create: `tests/services/test_summary_fragments.py`

**Interfaces:**

- Produces: `compute_file_fingerprints(members: list[Member]) -> dict[str, str]`.
- Produces: frozen `StoredSummaryFragment`, `FragmentFile`, `FragmentDelta`.
- Produces: `build_fragment_delta(cluster_key, current, stored, *, bootstrap, full_rebuild) -> FragmentDelta`.
- `FragmentDelta` exposes `added`, `changed`, `removed`, `moved`, `reused` and `pending_paths`.

- [ ] **Step 1: Write failing fingerprint tests**

Add tests proving that two symbols in one file produce one order-independent fingerprint,
that a body-only `content_hash` change does not alter it, and that a changed
`skeleton_hash` does:

```python
def test_compute_file_fingerprints_uses_skeleton_per_path():
    members = [
        Member("pkg/a.py#B", "pkg/a.py", "body-1", "sk-b", 8),
        Member("pkg/a.py#A", "pkg/a.py", "body-2", "sk-a", 1),
        Member("pkg/b.py#C", "pkg/b.py", "body-3", "sk-c", 1),
    ]
    got = compute_file_fingerprints(members)
    assert got["pkg/a.py"] == compute_source_hash([
        ("pkg/a.py#A", "sk-a"), ("pkg/a.py#B", "sk-b")
    ])
    assert set(got) == {"pkg/a.py", "pkg/b.py"}
```

- [ ] **Step 2: Run fingerprint tests and verify RED**

Run:

```bash
../../.venv/bin/pytest tests/graph/test_summaries.py -q
```

Expected: import/attribute failure for `compute_file_fingerprints`.

- [ ] **Step 3: Implement deterministic file fingerprints**

Group `Member` objects by `path` and delegate hashing to existing
`compute_source_hash`; do not duplicate hashing semantics:

```python
def compute_file_fingerprints(members: list[Member]) -> dict[str, str]:
    grouped: dict[str, list[tuple[str, str]]] = {}
    for member in members:
        grouped.setdefault(member.path, []).append(
            (member.node_id, member.skeleton_hash)
        )
    return {
        path: compute_source_hash(items)
        for path, items in sorted(grouped.items())
    }
```

- [ ] **Step 4: Write failing delta classification tests**

Cover these literal cases in `tests/services/test_summary_fragments.py`:

```python
def test_delta_classifies_added_changed_moved_reused_and_removed():
    current = {
        "same.py": "same",
        "changed.py": "new",
        "added.py": "new",
        "moved.py": "same",
    }
    stored = [
        StoredSummaryFragment("cluster", "same.py", "same", "S", {}),
        StoredSummaryFragment("cluster", "changed.py", "old", "C", {}),
        StoredSummaryFragment("old-cluster", "moved.py", "same", "M", {}),
        StoredSummaryFragment("cluster", "removed.py", "old", "R", {}),
    ]
    delta = build_fragment_delta(
        "cluster", current, stored, bootstrap=False, full_rebuild=False
    )
    assert [item.path for item in delta.added] == ["added.py"]
    assert [item.path for item in delta.changed] == ["changed.py"]
    assert [item.path for item in delta.moved] == ["moved.py"]
    assert [item.path for item in delta.reused] == ["same.py"]
    assert [item.path for item in delta.removed] == ["removed.py"]
    assert delta.pending_paths == ("added.py", "changed.py")
```

Also prove:

- bootstrap treats every current file as pending and reuses nothing;
- depth full rebuild treats every current file as pending and reuses nothing;
- duplicate historical rows prefer a matching current-cluster fragment;
- ordering is stable by path.

- [ ] **Step 5: Run delta tests and verify RED**

Run:

```bash
../../.venv/bin/pytest tests/services/test_summary_fragments.py -q
```

Expected: module import failure.

- [ ] **Step 6: Implement the pure delta module**

Use frozen dataclasses:

```python
@dataclass(frozen=True)
class FragmentFile:
    path: str
    fingerprint: str
    summary: str = ""
    provenance: Mapping[str, JsonValue] = field(default_factory=dict)
    from_cluster_key: str | None = None

@dataclass(frozen=True)
class StoredSummaryFragment:
    cluster_key: str
    path: str
    fingerprint: str
    summary: str
    provenance: Mapping[str, JsonValue]

@dataclass(frozen=True)
class FragmentDelta:
    added: tuple[FragmentFile, ...]
    changed: tuple[FragmentFile, ...]
    removed: tuple[FragmentFile, ...]
    moved: tuple[FragmentFile, ...]
    reused: tuple[FragmentFile, ...]

    @property
    def pending_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.added + self.changed)
```

`build_fragment_delta` must classify by exact `(path, fingerprint)` and only use
cross-cluster reuse when both `bootstrap` and `full_rebuild` are false.

- [ ] **Step 7: Run Task 1 tests and ruff**

Run:

```bash
../../.venv/bin/pytest tests/graph/test_summaries.py tests/services/test_summary_fragments.py -q
../../.venv/bin/ruff check reviewer/graph/summaries.py reviewer/services/summary_fragments.py tests/graph/test_summaries.py tests/services/test_summary_fragments.py
```

Expected: PASS and no lint findings.

- [ ] **Step 8: Commit Task 1**

```bash
git add reviewer/graph/summaries.py reviewer/services/summary_fragments.py tests/graph/test_summaries.py tests/services/test_summary_fragments.py
git commit -m "feat(summaries): добавить file-level delta (PRI-226)"
```

---

### Task 2: Transactional fragment storage, depth state, and embedding CAS

**Files:**

- Modify: `reviewer/index/schema.sql`
- Modify: `reviewer/index/summary_store.py`
- Modify: `tests/index/test_summary_store.py`

**Interfaces:**

- Produces: `SummaryStore.get_fragments(repo, branch) -> list[dict]`.
- Produces: `SummaryStore.get_completed_depth(repo, branch) -> int | None`.
- Produces: `SummaryStore.commit_summary_bundle(..., current_fingerprints, new_fragments) -> dict`.
- Produces: `SummaryStore.set_embedding_if_source_hash(...) -> bool`.
- Produces: `SummaryStore.prune_except_and_set_depth(repo, branch, keep_keys, depth) -> dict`.
- Existing `upsert_summary(..., embedding=None)` preserves an embedding only when the source hash is unchanged; a changed hash resets it to NULL.

- [ ] **Step 1: Write failing schema/round-trip integration tests**

Extend the fixture cleanup to delete `subsystem_summary_fragments` and
`subsystem_summary_state`. Add a test that initializes schema, commits one fragment
with provenance and reads it back:

```python
def test_fragment_roundtrip_keeps_provenance_and_timestamp(store):
    summary_store, repo = store
    metrics = summary_store.commit_summary_bundle(
        repo, "dev", "reviewer/index", "Индекс", "Сводка",
        ["reviewer/index/a.py#A"], "cluster-hash",
        current_fingerprints={"reviewer/index/a.py": "file-hash"},
        new_fragments=[{
            "path": "reviewer/index/a.py",
            "fingerprint": "file-hash",
            "summary": "Файл индекса.",
            "provenance": {"generator": "summarize-subsystems"},
        }],
    )
    assert metrics == {"created": 1, "reused": 0, "removed": 0, "moved": 0}
    [fragment] = summary_store.get_fragments(repo, "dev")
    assert fragment["provenance"] == {"generator": "summarize-subsystems"}
    assert "T" in fragment["updated_at"]
```

- [ ] **Step 2: Write failing atomic move/remove/reuse tests**

Add integration cases that:

- seed `same.py`, `removed.py`, and `moved.py` in two clusters;
- commit a target bundle with one new changed fragment;
- assert matching rows are reused/moved, removed rows disappear, and metrics are exact;
- force an exception by supplying no fragment for a required fingerprint and assert the
  pre-existing database state is unchanged.

- [ ] **Step 3: Write failing depth and embedding tests**

Add tests for:

```python
assert summary_store.get_completed_depth(repo, "dev") is None
result = summary_store.prune_except_and_set_depth(
    repo, "dev", ["reviewer/index"], 2
)
assert result["depth"] == 2
assert summary_store.get_completed_depth(repo, "dev") == 2
```

Also prove:

- prune deletes orphan summaries and fragments together;
- changed `source_hash` with `embedding=None` yields a pending embedding instead of
  preserving the old vector;
- `set_embedding_if_source_hash` returns false for a stale hash and true for the
  current hash.

- [ ] **Step 4: Run storage tests and verify RED**

Run:

```bash
docker compose --profile test up -d --wait paradedb-test
TEST_PG_DSN='postgresql://reviewer_test:reviewer_test@localhost:55433/reviewer_test?connect_timeout=2' \
  ../../.venv/bin/pytest tests/index/test_summary_store.py -q -m integration
```

Expected: schema/method failures for the new storage API.

- [ ] **Step 5: Add the two idempotent schema tables**

Add `CREATE TABLE IF NOT EXISTS` statements exactly matching the design:

```sql
CREATE TABLE IF NOT EXISTS subsystem_summary_fragments (
    repo text NOT NULL DEFAULT '',
    branch text NOT NULL,
    cluster_key text NOT NULL,
    path text NOT NULL,
    fingerprint text NOT NULL,
    summary text NOT NULL,
    provenance jsonb NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, branch, cluster_key, path)
);

CREATE INDEX IF NOT EXISTS subsystem_summary_fragments_path
ON subsystem_summary_fragments (repo, branch, path);

CREATE TABLE IF NOT EXISTS subsystem_summary_state (
    repo text NOT NULL DEFAULT '',
    branch text NOT NULL,
    completed_depth integer NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, branch)
);
```

- [ ] **Step 6: Implement reads and atomic bundle persistence**

`commit_summary_bundle` opens one connection/transaction, locks the repo/branch
fragment rows with `FOR UPDATE`, validates that every current path ends with exactly
one matching fragment, then upserts the cluster summary in the same transaction.
Raise `ValueError` before commit when coverage is incomplete or fingerprints differ.

When upserting `subsystem_summaries`, use:

```sql
embedding = CASE
    WHEN subsystem_summaries.source_hash = EXCLUDED.source_hash
    THEN COALESCE(EXCLUDED.embedding, subsystem_summaries.embedding)
    ELSE EXCLUDED.embedding
END
```

This preserves the vector only for the same source hash and resets it for new text.

- [ ] **Step 7: Implement prune/state and embedding compare-and-set**

`prune_except_and_set_depth` deletes summaries and fragments outside `keep_keys`, then
upserts `completed_depth` in the same transaction. Empty `keep_keys` is supported by
the store, while the service retains the existing empty-base no-op gate.

`set_embedding_if_source_hash` executes one conditional UPDATE and returns
`rowcount == 1`.

- [ ] **Step 8: Run storage tests and ruff**

Run:

```bash
TEST_PG_DSN='postgresql://reviewer_test:reviewer_test@localhost:55433/reviewer_test?connect_timeout=2' \
  ../../.venv/bin/pytest tests/index/test_summary_store.py -q -m integration
../../.venv/bin/ruff check reviewer/index/summary_store.py tests/index/test_summary_store.py
docker compose --profile test rm -sfv paradedb-test neo4j-test
```

Expected: PASS, no lint findings, test containers removed with the safe targeted command.

- [ ] **Step 9: Commit Task 2**

```bash
git add reviewer/index/schema.sql reviewer/index/summary_store.py tests/index/test_summary_store.py
git commit -m "feat(index): хранить summary fragments атомарно (PRI-226)"
```

---

### Task 3: MCP work/persist protocol and optimistic race rejection

**Files:**

- Modify: `reviewer/mcp/schemas.py`
- Modify: `reviewer/mcp/service.py`
- Modify: `reviewer/entrypoints/mcp_server.py`
- Modify: `tests/mcp/test_subsystem_summaries.py`
- Modify: `tests/mcp/test_server.py`

**Interfaces:**

- Produces Pydantic `SummaryFragmentIn(path, fingerprint, summary, provenance)`.
- Produces `MCPReviewService.get_subsystem_summary_work(...)`.
- Extends `MCPReviewService.index_subsystem_summary(..., fragments=None)`.
- Registers FastMCP tool `get_subsystem_summary_work`.
- `list_subsystem_clusters` keeps all old fields and adds file delta fields plus top-level `deferred_files`.

- [ ] **Step 1: Write failing list/work tests**

Use the existing `_svc(c)` MagicMock fixture and literal `list_base_members` rows.
Configure:

```python
c.summary_store.get_completed_depth.return_value = 2
c.summary_store.get_fragments.return_value = [
    {
        "cluster_key": "reviewer/index",
        "path": "reviewer/index/a.py",
        "fingerprint": file_hash,
        "summary": "A",
        "provenance": {},
    }
]
```

Assert `list_subsystem_clusters` includes empty `added_files`/`changed_files`, one
`reused_files`, and no change to existing fields. Add a capped case proving
`deferred_files` counts pending file jobs in omitted stale clusters.

For `get_subsystem_summary_work`, assert:

- correct hash returns delta and full reused fragment texts;
- stale hash returns `ready=False` without generation data;
- missing depth state returns `bootstrap=True` and every current path pending;
- changed depth returns `full_rebuild=True` and no reused/moved fragments.

- [ ] **Step 2: Run list/work tests and verify RED**

Run:

```bash
../../.venv/bin/pytest tests/mcp/test_subsystem_summaries.py -q
```

Expected: missing store methods/tool and missing response fields.

- [ ] **Step 3: Add service helpers and read protocol**

Extract one private helper that returns resolved depth, cluster objects, members,
per-file fingerprints, fragments, and depth state. Reuse it from list/work/index so
the three paths cannot drift.

Serialize delta entries as:

```python
{"path": item.path, "fingerprint": item.fingerprint}
```

Moved entries additionally expose `from_cluster_key`; work results expose `summary`
and `provenance` only for reusable/moved fragments.

- [ ] **Step 4: Write failing optimistic persist tests**

Add tests proving:

```python
out = svc.index_subsystem_summary(
    "o/n", "dev", "reviewer/index", "Индекс", "...", "STALE",
    fragments=[],
)
assert out["stored"] is False
assert out["race"] is True
c.summary_store.commit_summary_bundle.assert_not_called()
c.embedder.embed_documents.assert_not_called()
```

Also cover:

- missing or extra pending paths rejects before store;
- wrong file fingerprint rejects before store;
- success calls `commit_summary_bundle` once with the full current map;
- embedder is called after store commit and CAS receives the same source hash;
- Voyage failure leaves `stored=True`, adds a backfill note, and does not lie about
  embedding success;
- legacy `fragments=None` still stores a strictly hash-checked cluster summary.

- [ ] **Step 5: Implement write protocol and post-commit embedding**

Validate `SummaryFragmentIn` payloads through Pydantic. Recompute current aggregate
hash immediately before persistence. Exact incoming paths must equal
`delta.pending_paths`; every payload fingerprint must equal the current file map.

After a successful store call:

```python
try:
    embedding = self.components.embedder.embed_documents(
        [f"{title}\n{summary}"]
    )[0]
    embedded = self.components.summary_store.set_embedding_if_source_hash(
        repo, resolved, cluster_key, source_hash, embedding
    )
except Exception:
    embedded = False
```

Return `embedded` and an explicit backfill note when false.

- [ ] **Step 6: Register the 37th MCP tool and test routing**

Add `get_subsystem_summary_work` beside the existing summary tools and extend
`index_subsystem_summary` with:

```python
fragments: list[SummaryFragmentIn] | None = None
```

Update `test_server_registers_all_tools` from 36 to 37 names and add a direct
FastMCP call proving the typed fragments reach the service.

- [ ] **Step 7: Extend prune to fragments/depth state**

Replace the summary-only store call with:

```python
result = self.components.summary_store.prune_except_and_set_depth(
    repo, resolved, keep_keys, depth
)
```

Return backward-compatible `pruned`/`kept` plus `fragments_pruned` and `depth`.

- [ ] **Step 8: Run MCP tests and ruff**

Run:

```bash
../../.venv/bin/pytest tests/mcp/test_subsystem_summaries.py tests/mcp/test_server.py -q
../../.venv/bin/ruff check reviewer/mcp/schemas.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_subsystem_summaries.py tests/mcp/test_server.py
```

Expected: PASS and no lint findings.

- [ ] **Step 9: Commit Task 3**

```bash
git add reviewer/mcp/schemas.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_subsystem_summaries.py tests/mcp/test_server.py
git commit -m "feat(mcp): добавить incremental summary protocol (PRI-226)"
```

---

### Task 4: Global plugin pipeline, metrics, compatibility guards, and installation

**Files:**

- Modify: `plugin/skills/summarize-subsystems/SKILL.md`
- Modify: `tests/skills/test_summarize_subsystems.py`
- Modify: `tests/skills/test_assembled_prompts.py` only if its assembled snapshot expectations require the new tool text
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Interfaces:**

- Skill calls `get_subsystem_summary_work` once per selected stale cluster.
- Skill dispatches one file job per `added_files + changed_files`.
- Cluster composer receives only fragment text, never unchanged source files.
- Final report includes `created`, `reused`, `removed`, `moved`, `deferred/raced`,
  `fragments_pruned`, and `embedded`.

- [ ] **Step 1: Write failing skill behavior guards**

Add tests asserting the assembled skill:

```python
assert "get_subsystem_summary_work" in text
assert "added_files + changed_files" in text
assert "one file-summary job" in text
assert "must not read unchanged" in text.lower()
assert "reused_fragments" in text
assert "created" in text
assert "reused" in text
assert "removed" in text
assert "moved" in text
assert "deferred" in text
```

Also assert that the persist step passes `fragments` and treats `stored=false` as
deferred/raced rather than success.

- [ ] **Step 2: Run skill tests and verify RED**

Run:

```bash
../../.venv/bin/pytest tests/skills/test_summarize_subsystems.py tests/skills/test_assembled_prompts.py -q
```

Expected: missing incremental protocol instructions.

- [ ] **Step 3: Rewrite the stale-cluster stage**

Keep existing repo/branch, depth confirmation, model choice, cap, prune and backfill
gates. Replace cluster-wide source reads with:

1. `get_subsystem_summary_work`.
2. Exactly one chosen-model subagent per added/changed path.
3. Per-file result `{path, fingerprint, summary, provenance}`.
4. One cluster composer over ordered reused/moved/new fragment texts.
5. `index_subsystem_summary(..., fragments=[new file results])`.
6. Race handling and metric accumulation.

The file prompt must name only its own path. The composer prompt must explicitly
forbid `Read` and source-code claims absent from fragments.

- [ ] **Step 4: Document bootstrap and operational semantics**

In `README.md` and `CLAUDE.md`, document:

- first post-upgrade run bootstraps all file fragments without removing old summaries;
- body-only changes remain intentionally invisible under skeleton freshness;
- depth change forces a full fragment rebuild;
- partial/capped runs do not prune;
- new metrics and optimistic race behavior.

- [ ] **Step 5: Run plugin/doc tests and ruff**

Run:

```bash
../../.venv/bin/pytest tests/skills -q
../../.venv/bin/pytest tests/install/test_codex_plugin_payload.py tests/install/test_codex_cli.py -q
../../.venv/bin/ruff check reviewer tests
```

Expected: PASS and no lint findings.

- [ ] **Step 6: Run full unit suite**

Run:

```bash
../../.venv/bin/pytest -q
```

Expected: all unit tests pass; integration tests remain deselected by project config.

- [ ] **Step 7: Verify local Codex installer dry-run**

Run from the feature worktree:

```bash
../../.venv/bin/reviewer install codex --dry-run
codex plugin list --json
codex mcp list
```

Expected: dry-run succeeds offline; current global state contains one enabled
`rag-reviewer` plugin and exactly one `reviewer` MCP entry.

- [ ] **Step 8: Install the updated local global plugin**

Run:

```bash
../../.venv/bin/reviewer install codex
codex plugin list --json
codex mcp list
```

Verify the installed plugin version uses the current package base version plus the
new `+codex.<payload_digest>` cachebuster. Do not modify or remove unrelated plugins.

- [ ] **Step 9: Commit Task 4**

```bash
git add plugin/skills/summarize-subsystems/SKILL.md tests/skills/test_summarize_subsystems.py tests/skills/test_assembled_prompts.py README.md CLAUDE.md
git commit -m "feat(skills): суммаризировать только изменённые файлы (PRI-226)"
```

If `tests/skills/test_assembled_prompts.py` did not change, omit it from `git add`.

---

### Task 5: Final integration verification and evidence

**Files:**

- Modify only files required to fix findings from verification/review.

**Interfaces:**

- Consumes all Tasks 1–4.
- Produces a verified branch ready for PR against `dev`.

- [ ] **Step 1: Run focused unit and integration suites**

```bash
../../.venv/bin/pytest tests/graph/test_summaries.py tests/services/test_summary_fragments.py tests/mcp/test_subsystem_summaries.py tests/mcp/test_server.py tests/skills/test_summarize_subsystems.py -q
docker compose --profile test up -d --wait paradedb-test neo4j-test
TEST_PG_DSN='postgresql://reviewer_test:reviewer_test@localhost:55433/reviewer_test?connect_timeout=2' \
TEST_NEO4J_URI=neo4j://localhost:17687 \
TEST_NEO4J_USER=neo4j \
TEST_NEO4J_PASSWORD=reviewer_test_pass \
  ../../.venv/bin/pytest tests/index/test_summary_store.py -q -m integration
docker compose --profile test rm -sfv paradedb-test neo4j-test
```

Expected: all selected tests pass and test infrastructure is removed safely.

- [ ] **Step 2: Run full quality gate**

```bash
../../.venv/bin/pytest -q
../../.venv/bin/ruff check .
git diff --check dev...HEAD
```

Expected: unit suite and ruff pass; no whitespace errors.

- [ ] **Step 3: Verify artifact and requirement coverage**

Confirm from fresh command output:

```bash
git log --oneline dev..HEAD
git diff --stat dev...HEAD
git status --short --branch
```

Check each acceptance criterion against tests and the spec. The branch must contain
the brief, design, plan, production changes, plugin instructions and regression tests,
with no unrelated user files.

- [ ] **Step 4: Commit verification fixes if any**

If verification required code changes, repeat the covering RED/GREEN cycle, then:

```bash
git add <only-the-verified-fix-files>
git commit -m "fix(summaries): закрыть интеграционные проверки (PRI-226)"
```

If no files changed, do not create an empty commit.
