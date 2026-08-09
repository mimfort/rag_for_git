"""Integration-тесты SummaryStore на изолированном ParadeDB.

Инфраструктура запускается командой:
`docker compose --profile test up -d --wait paradedb-test`.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from reviewer.config.settings import Settings
from reviewer.graph.summaries import compute_layout_token
from reviewer.index.store import ChunkStore
from reviewer.index.summary_store import SummaryStore

pytestmark = pytest.mark.integration

DIM = 1024
LAYOUT_TOKEN = compute_layout_token(2, {})


def _generation_provenance() -> dict:
    return {
        "_reviewer": {
            "generation": "summary-fragment-v1",
            "layout_token": LAYOUT_TOKEN,
            "depth": 2,
        }
    }


def _vec(hot: int) -> list[float]:
    """Орт-подобный 1024-вектор с единицей в позиции hot — для предсказуемого ANN."""
    v = [0.0] * DIM
    v[hot] = 1.0
    return v


def _delete_summary_rows(summary_store: SummaryStore, repo: str) -> None:
    """Удалить все тестовые summary-данные одного репозитория."""
    with summary_store._connect() as conn:
        conn.execute("DELETE FROM subsystem_summary_fragments WHERE repo=%s", (repo,))
        conn.execute("DELETE FROM subsystem_summary_state WHERE repo=%s", (repo,))
        conn.execute("DELETE FROM subsystem_summaries WHERE repo=%s", (repo,))
        conn.commit()


@contextmanager
def _database_trigger_barrier(
    dsn: str,
    repo: str,
    table: str,
    events: str,
) -> Iterator[Callable[[], None]]:
    """Остановить тестовую транзакцию перед выбранной записью без sleep."""
    suffix = uuid4().hex
    function_name = f"summary_barrier_fn_{suffix}"
    trigger_name = f"summary_barrier_tr_{suffix}"
    barrier_key = uuid4().int % (2**63)
    event_sql = {
        "insert": sql.SQL("INSERT"),
        "insert_or_update": sql.SQL("INSERT OR UPDATE"),
    }[events]
    blocker = psycopg.connect(dsn, autocommit=True)
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            blocker.execute("SELECT pg_advisory_unlock(%s)", (barrier_key,))
            released = True

    try:
        with psycopg.connect(dsn) as conn:
            conn.execute(
                sql.SQL(
                    """
                    CREATE FUNCTION {}() RETURNS trigger
                    LANGUAGE plpgsql AS $$
                    BEGIN
                        IF NEW.repo = {} THEN
                            PERFORM pg_advisory_xact_lock({});
                        END IF;
                        RETURN NEW;
                    END
                    $$
                    """
                ).format(
                    sql.Identifier(function_name),
                    sql.Literal(repo),
                    sql.Literal(barrier_key),
                )
            )
            conn.execute(
                sql.SQL(
                    "CREATE TRIGGER {} BEFORE {} ON {} "
                    "FOR EACH ROW EXECUTE FUNCTION {}()"
                ).format(
                    sql.Identifier(trigger_name),
                    event_sql,
                    sql.Identifier(table),
                    sql.Identifier(function_name),
                )
            )
        blocker.execute("SELECT pg_advisory_lock(%s)", (barrier_key,))
        yield release
    finally:
        release()
        with psycopg.connect(dsn) as conn:
            conn.execute(
                sql.SQL("DROP TRIGGER IF EXISTS {} ON {}").format(
                    sql.Identifier(trigger_name),
                    sql.Identifier(table),
                )
            )
            conn.execute(
                sql.SQL("DROP FUNCTION IF EXISTS {}()").format(
                    sql.Identifier(function_name)
                )
            )
        blocker.close()


def _wait_for_advisory_or_done(
    dsn: str,
    application_name: str,
    future: Future,
) -> str:
    """Дождаться DB-lock wait или завершения worker без временного sleep."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        for _ in range(300):
            waiting = conn.execute(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_stat_activity "
                "WHERE application_name=%s "
                "AND wait_event_type='Lock' AND wait_event='advisory')",
                (application_name,),
            ).fetchone()[0]
            if waiting:
                return "waiting"
            done, _pending = wait([future], timeout=0.01)
            if done:
                return "done"
    raise AssertionError(f"Worker {application_name} не дошёл до advisory lock")


def _commit_single_fragment(dsn: str, repo: str, cluster_key: str) -> dict:
    """Сохранить один fragment через отдельный connection pool."""
    store = SummaryStore(dsn, min_size=1, max_size=1)
    try:
        return store.commit_summary_bundle(
            repo,
            "dev",
            cluster_key,
            cluster_key,
            f"Сводка {cluster_key}",
            ["same.py#Same"],
            f"hash-{cluster_key}",
            current_fingerprints={"same.py": "fingerprint"},
            new_fragments=[{
                "path": "same.py",
                "fingerprint": "fingerprint",
                "summary": f"Fragment {cluster_key}",
                "provenance": {},
            }],
        )
    finally:
        store.close()


def _prune_empty_branch(dsn: str, repo: str) -> dict:
    """Выполнить полный prune через отдельный connection pool."""
    store = SummaryStore(dsn, min_size=1, max_size=1)
    try:
        return store.prune_verified_layout(
            repo,
            "dev",
            {"kept": "hash-kept"},
            {"kept": {"kept.py": "fingerprint-kept"}},
            2,
            LAYOUT_TOKEN,
        )
    finally:
        store.close()


@pytest.fixture()
def store():
    dsn = Settings().pg_dsn
    repo = f"test/summary-store/{uuid4().hex}"
    schema_store = ChunkStore(dsn)
    try:
        schema_store.init_schema()  # создаёт subsystem_summaries (schema.sql)
    finally:
        schema_store.close()
    summary_store = SummaryStore(dsn)
    try:
        _delete_summary_rows(summary_store, repo)
        yield summary_store, repo
    finally:
        try:
            _delete_summary_rows(summary_store, repo)
        finally:
            summary_store.close()


def test_upsert_then_get_roundtrip(store):
    summary_store, repo = store
    summary_store.upsert_summary(repo, "dev", "reviewer/index", "Индекс",
                                 "Хранилище чанков и ретрив.",
                                 ["reviewer/index/store.py#X"], "h1")
    assert summary_store.get_source_hashes(repo, "dev") == {"reviewer/index": "h1"}
    rows = summary_store.get_summaries(repo, "dev")
    assert len(rows) == 1
    row = rows[0]
    assert row["cluster_key"] == "reviewer/index"
    assert row["title"] == "Индекс"
    assert row["summary"] == "Хранилище чанков и ретрив."
    assert row["source_hash"] == "h1"
    assert "T" in row["updated_at"]        # ISO-таймстамп (зеркало единичного get_summary)
    one = summary_store.get_summary(repo, "dev", "reviewer/index")
    assert one["member_node_ids"] == ["reviewer/index/store.py#X"]


def test_upsert_is_idempotent_update(store):
    summary_store, repo = store
    summary_store.upsert_summary(repo, "dev", "reviewer/index", "A", "old", [], "h1")
    summary_store.upsert_summary(repo, "dev", "reviewer/index", "A", "new", [], "h2")
    assert summary_store.get_source_hashes(repo, "dev") == {"reviewer/index": "h2"}
    assert summary_store.get_summaries(repo, "dev")[0]["summary"] == "new"


def test_list_base_members_reads_base_ref_rows():
    from reviewer.index.store import ChunkStore, ChunkRow
    from reviewer.index.chunker import symbol_skeleton_hash

    repo = f"test/summary-store/{uuid4().hex}"
    cs = ChunkStore(Settings().pg_dsn)
    try:
        cs.init_schema()
        cs.clear(repo)
        cs.upsert([ChunkRow(repo=repo, ref="base:dev", content_hash="h",
                            path="reviewer/x/a.py", lang="python", symbol_fqn="A",
                            kind="function", start_line=3, end_line=9,
                            text="def a(): ...", embedding=[0.0] * 1024)])
        members = cs.list_base_members(repo, "dev")
        # 5-кортеж: skeleton_hash считается на лету из text
        assert ("reviewer/x/a.py", "A", "h", 3, symbol_skeleton_hash("def a(): ...")) in members
    finally:
        try:
            cs.clear(repo)
        finally:
            cs.close()


def test_get_updated_ats_returns_datetime_per_cluster(store):
    from datetime import datetime

    summary_store, repo = store
    summary_store.upsert_summary(repo, "dev", "reviewer/index", "A", "s", [], "h1")
    ats = summary_store.get_updated_ats(repo, "dev")
    assert "reviewer/index" in ats
    assert isinstance(ats["reviewer/index"], datetime)   # сырой datetime, не isoformat


def test_delete_summaries_except_prunes_orphans(store):
    summary_store, repo = store
    summary_store.upsert_summary(repo, "dev", "reviewer/index", "A", "s", [], "h1")
    summary_store.upsert_summary(repo, "dev", "reviewer/graph", "B", "s", [], "h2")
    summary_store.upsert_summary(repo, "dev", "reviewer/old", "C", "s", [], "h3")
    pruned = summary_store.delete_summaries_except(
        repo, "dev", ["reviewer/index", "reviewer/graph"]
    )
    assert pruned == 1                                   # удалён только reviewer/old
    assert set(summary_store.get_source_hashes(repo, "dev")) == {
        "reviewer/index", "reviewer/graph"
    }


def test_delete_summaries_except_empty_keep_deletes_all(store):
    summary_store, repo = store
    summary_store.upsert_summary(repo, "dev", "reviewer/index", "A", "s", [], "h1")
    pruned = summary_store.delete_summaries_except(repo, "dev", [])
    assert pruned == 1
    assert summary_store.get_source_hashes(repo, "dev") == {}


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


def test_commit_summary_bundle_atomically_creates_reuses_removes_and_moves(store):
    summary_store, repo = store
    summary_store.commit_summary_bundle(
        repo, "dev", "target", "Целевой кластер", "Старая сводка",
        ["same.py#Same", "removed.py#Removed"], "target-old",
        current_fingerprints={"same.py": "same", "removed.py": "removed"},
        new_fragments=[
            {"path": "same.py", "fingerprint": "same", "summary": "Same", "provenance": {}},
            {
                "path": "removed.py",
                "fingerprint": "removed",
                "summary": "Removed",
                "provenance": {},
            },
        ],
    )
    summary_store.commit_summary_bundle(
        repo, "dev", "source", "Исходный кластер", "Сводка источника",
        ["moved.py#Moved"], "source-old",
        current_fingerprints={"moved.py": "moved"},
        new_fragments=[
            {"path": "moved.py", "fingerprint": "moved", "summary": "Moved", "provenance": {}},
        ],
    )

    metrics = summary_store.commit_summary_bundle(
        repo, "dev", "target", "Целевой кластер", "Новая сводка",
        ["same.py#Same", "moved.py#Moved", "changed.py#Changed"], "target-new",
        current_fingerprints={
            "same.py": "same",
            "moved.py": "moved",
            "changed.py": "changed-new",
        },
        new_fragments=[{
            "path": "changed.py",
            "fingerprint": "changed-new",
            "summary": "Changed",
            "provenance": {"mode": "incremental"},
        }],
    )

    assert metrics == {"created": 1, "reused": 1, "removed": 1, "moved": 1}
    fragments = summary_store.get_fragments(repo, "dev")
    assert [(row["cluster_key"], row["path"], row["fingerprint"]) for row in fragments] == [
        ("target", "changed.py", "changed-new"),
        ("target", "moved.py", "moved"),
        ("target", "same.py", "same"),
    ]
    assert summary_store.get_summary(repo, "dev", "target")["summary"] == "Новая сводка"


def test_commit_summary_bundle_rolls_back_when_current_coverage_is_incomplete(store):
    summary_store, repo = store
    summary_store.commit_summary_bundle(
        repo, "dev", "target", "Целевой кластер", "Стабильная сводка",
        ["same.py#Same", "removed.py#Removed"], "stable-hash",
        current_fingerprints={"same.py": "same", "removed.py": "removed"},
        new_fragments=[
            {"path": "same.py", "fingerprint": "same", "summary": "Same", "provenance": {}},
            {
                "path": "removed.py",
                "fingerprint": "removed",
                "summary": "Removed",
                "provenance": {},
            },
        ],
    )
    fragments_before = summary_store.get_fragments(repo, "dev")
    summary_before = summary_store.get_summary(repo, "dev", "target")

    with pytest.raises(ValueError):
        summary_store.commit_summary_bundle(
            repo, "dev", "target", "Целевой кластер", "Не должна сохраниться",
            ["same.py#Same", "z-missing.py#Missing"], "new-hash",
            current_fingerprints={"same.py": "same", "z-missing.py": "missing"},
            new_fragments=[],
        )

    assert summary_store.get_fragments(repo, "dev") == fragments_before
    assert summary_store.get_summary(repo, "dev", "target") == summary_before


def test_ambiguous_cross_cluster_fragments_roll_back_then_regeneration_self_heals(store):
    summary_store, repo = store
    with summary_store._connect() as conn:
        for cluster_key, fragment_summary in (("old-a", "A"), ("old-b", "B")):
            conn.execute(
                "INSERT INTO subsystem_summary_fragments "
                "(repo, branch, cluster_key, path, fingerprint, summary, provenance) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    repo,
                    "dev",
                    cluster_key,
                    "same.py",
                    "same",
                    fragment_summary,
                    Jsonb({}),
                ),
            )
        conn.commit()

    with pytest.raises(ValueError, match="однозначного"):
        summary_store.commit_summary_bundle(
            repo,
            "dev",
            "target",
            "Target",
            "Не сохраняется",
            ["same.py#Same"],
            "target-hash",
            current_fingerprints={"same.py": "same"},
            new_fragments=[],
        )
    assert [
        (fragment["cluster_key"], fragment["summary"])
        for fragment in summary_store.get_fragments(repo, "dev")
    ] == [("old-a", "A"), ("old-b", "B")]
    assert summary_store.get_summary(repo, "dev", "target") is None

    metrics = summary_store.commit_summary_bundle(
        repo,
        "dev",
        "target",
        "Target",
        "Сохранено после regeneration",
        ["same.py#Same"],
        "target-hash",
        current_fingerprints={"same.py": "same"},
        new_fragments=[
            {
                "path": "same.py",
                "fingerprint": "same",
                "summary": "Regenerated",
                "provenance": _generation_provenance(),
            }
        ],
    )

    assert metrics["created"] == 1
    assert [
        (fragment["cluster_key"], fragment["summary"])
        for fragment in summary_store.get_fragments(repo, "dev")
    ] == [("target", "Regenerated")]


def test_concurrent_empty_branch_bundles_serialize_same_path(store):
    summary_store, repo = store
    first_app = f"summary-empty-first-{uuid4().hex}"
    second_app = f"summary-empty-second-{uuid4().hex}"
    first_dsn = make_conninfo(summary_store.dsn, application_name=first_app)
    second_dsn = make_conninfo(summary_store.dsn, application_name=second_app)

    with ThreadPoolExecutor(max_workers=2) as executor:
        with _database_trigger_barrier(
            summary_store.dsn,
            repo,
            "subsystem_summary_fragments",
            "insert",
        ) as release:
            first = executor.submit(
                _commit_single_fragment, first_dsn, repo, "cluster-a"
            )
            assert _wait_for_advisory_or_done(
                summary_store.dsn, first_app, first
            ) == "waiting"
            second = executor.submit(
                _commit_single_fragment, second_dsn, repo, "cluster-b"
            )
            assert _wait_for_advisory_or_done(
                summary_store.dsn, second_app, second
            ) == "waiting"
            release()
            first.result(timeout=5)
            second.result(timeout=5)

    assert [
        (fragment["cluster_key"], fragment["path"])
        for fragment in summary_store.get_fragments(repo, "dev")
    ] == [("cluster-b", "same.py")]


def test_bundle_waits_until_prune_depth_transaction_finishes(store):
    summary_store, repo = store
    summary_store.commit_summary_bundle(
        repo,
        "dev",
        "kept",
        "Kept",
        "Сводка kept",
        ["kept.py#Kept"],
        "hash-kept",
        current_fingerprints={"kept.py": "fingerprint-kept"},
        new_fragments=[
            {
                "path": "kept.py",
                "fingerprint": "fingerprint-kept",
                "summary": "Kept",
                "provenance": _generation_provenance(),
            }
        ],
    )
    prune_app = f"summary-prune-{uuid4().hex}"
    bundle_app = f"summary-bundle-{uuid4().hex}"
    prune_dsn = make_conninfo(summary_store.dsn, application_name=prune_app)
    bundle_dsn = make_conninfo(summary_store.dsn, application_name=bundle_app)

    with ThreadPoolExecutor(max_workers=2) as executor:
        with _database_trigger_barrier(
            summary_store.dsn,
            repo,
            "subsystem_summary_state",
            "insert_or_update",
        ) as release:
            prune = executor.submit(_prune_empty_branch, prune_dsn, repo)
            assert _wait_for_advisory_or_done(
                summary_store.dsn, prune_app, prune
            ) == "waiting"
            bundle = executor.submit(
                _commit_single_fragment, bundle_dsn, repo, "new"
            )
            bundle_state = _wait_for_advisory_or_done(
                summary_store.dsn, bundle_app, bundle
            )
            release()
            prune.result(timeout=5)
            bundle.result(timeout=5)

    assert bundle_state == "waiting"
    assert summary_store.get_completed_depth(repo, "dev") == 2
    assert summary_store.get_completed_layout(repo, "dev") == LAYOUT_TOKEN
    assert [
        (fragment["cluster_key"], fragment["path"])
        for fragment in summary_store.get_fragments(repo, "dev")
    ] == [("kept", "kept.py"), ("new", "same.py")]


def test_prune_verified_layout_removes_orphans_and_records_layout(store):
    summary_store, repo = store
    for cluster_key in ("reviewer/index", "reviewer/old"):
        path = f"{cluster_key}/a.py"
        summary_store.commit_summary_bundle(
            repo, "dev", cluster_key, cluster_key, "Сводка",
            [f"{path}#A"], f"hash-{cluster_key}",
            current_fingerprints={path: f"fingerprint-{cluster_key}"},
            new_fragments=[{
                "path": path,
                "fingerprint": f"fingerprint-{cluster_key}",
                "summary": "Фрагмент",
                "provenance": _generation_provenance(),
            }],
        )

    assert summary_store.get_completed_depth(repo, "dev") is None
    assert summary_store.get_completed_layout(repo, "dev") is None
    result = summary_store.prune_verified_layout(
        repo,
        "dev",
        {"reviewer/index": "hash-reviewer/index"},
        {"reviewer/index": {"reviewer/index/a.py": "fingerprint-reviewer/index"}},
        2,
        LAYOUT_TOKEN,
    )

    assert result == {
        "completed": True,
        "race": False,
        "deferred": 0,
        "pruned": 1,
        "fragments_pruned": 1,
        "depth": 2,
        "layout_token": LAYOUT_TOKEN,
    }
    assert summary_store.get_completed_depth(repo, "dev") == 2
    assert summary_store.get_completed_layout(repo, "dev") == LAYOUT_TOKEN
    assert set(summary_store.get_source_hashes(repo, "dev")) == {"reviewer/index"}
    assert [row["cluster_key"] for row in summary_store.get_fragments(repo, "dev")] == [
        "reviewer/index"
    ]


def test_prune_verified_layout_rejects_premature_bootstrap_without_summary(store):
    summary_store, repo = store

    result = summary_store.prune_verified_layout(
        repo,
        "dev",
        {"reviewer/index": "cluster-hash"},
        {"reviewer/index": {"reviewer/index/a.py": "file-hash"}},
        2,
        LAYOUT_TOKEN,
    )

    assert result["completed"] is False
    assert result["race"] is True
    assert result["deferred"] == 1
    assert summary_store.get_completed_depth(repo, "dev") is None
    assert summary_store.get_completed_layout(repo, "dev") is None


def test_prune_verified_layout_rejects_missing_cluster_fingerprint_snapshot(store):
    summary_store, repo = store
    summary_store.upsert_summary(
        repo,
        "dev",
        "reviewer/index",
        "Индекс",
        "Сводка",
        ["reviewer/index/a.py#A"],
        "cluster-hash",
    )

    result = summary_store.prune_verified_layout(
        repo,
        "dev",
        {"reviewer/index": "cluster-hash"},
        {},
        2,
        LAYOUT_TOKEN,
    )

    assert result["completed"] is False
    assert result["race"] is True
    assert summary_store.get_completed_layout(repo, "dev") is None


def test_prune_verified_layout_rejects_incomplete_fragment_coverage_without_delete(store):
    summary_store, repo = store
    summary_store.commit_summary_bundle(
        repo,
        "dev",
        "reviewer/index",
        "Индекс",
        "Сводка",
        ["reviewer/index/a.py#A"],
        "cluster-hash",
        current_fingerprints={"reviewer/index/a.py": "file-hash"},
        new_fragments=[
            {
                "path": "reviewer/index/a.py",
                "fingerprint": "file-hash",
                "summary": "Файл индекса.",
                "provenance": {},
            }
        ],
    )
    summary_store.upsert_summary(
        repo,
        "dev",
        "orphan",
        "Сирота",
        "Не удалять при reject",
        [],
        "orphan-hash",
    )

    result = summary_store.prune_verified_layout(
        repo,
        "dev",
        {"reviewer/index": "cluster-hash"},
        {"reviewer/index": {"reviewer/index/a.py": "file-hash"}},
        2,
        LAYOUT_TOKEN,
    )

    assert result["completed"] is False
    assert result["race"] is True
    assert result["deferred"] == 1
    assert set(summary_store.get_source_hashes(repo, "dev")) == {
        "orphan",
        "reviewer/index",
    }
    assert summary_store.get_completed_layout(repo, "dev") is None


def test_prune_rejects_extra_same_cluster_fragment_without_mutation(store):
    summary_store, repo = store
    summary_store.commit_summary_bundle(
        repo,
        "dev",
        "reviewer/index",
        "Индекс",
        "Сводка",
        ["reviewer/index/a.py#A"],
        "cluster-hash",
        current_fingerprints={"reviewer/index/a.py": "file-hash"},
        new_fragments=[
            {
                "path": "reviewer/index/a.py",
                "fingerprint": "file-hash",
                "summary": "Current",
                "provenance": _generation_provenance(),
            }
        ],
    )
    with summary_store._connect() as conn:
        conn.execute(
            "INSERT INTO subsystem_summary_fragments "
            "(repo, branch, cluster_key, path, fingerprint, summary, provenance) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                repo,
                "dev",
                "reviewer/index",
                "reviewer/index/stale.py",
                "stale-hash",
                "Stale",
                Jsonb(_generation_provenance()),
            ),
        )
        conn.commit()
    summary_store.upsert_summary(
        repo,
        "dev",
        "orphan",
        "Сирота",
        "Не удалять",
        [],
        "orphan-hash",
    )

    result = summary_store.prune_verified_layout(
        repo,
        "dev",
        {"reviewer/index": "cluster-hash"},
        {"reviewer/index": {"reviewer/index/a.py": "file-hash"}},
        2,
        LAYOUT_TOKEN,
    )

    assert result["completed"] is False
    assert result["race"] is True
    assert set(summary_store.get_source_hashes(repo, "dev")) == {
        "orphan",
        "reviewer/index",
    }
    assert {
        fragment["path"]
        for fragment in summary_store.get_fragments(repo, "dev")
        if fragment["cluster_key"] == "reviewer/index"
    } == {"reviewer/index/a.py", "reviewer/index/stale.py"}
    assert summary_store.get_completed_layout(repo, "dev") is None


def test_legacy_completed_depth_without_layout_remains_incomplete_after_schema_rerun(store):
    summary_store, repo = store
    with summary_store._connect() as conn:
        conn.execute(
            "INSERT INTO subsystem_summary_state "
            "(repo, branch, completed_depth, completed_layout) "
            "VALUES (%s,%s,%s,NULL)",
            (repo, "dev", 2),
        )
        conn.commit()
    schema_store = ChunkStore(summary_store.dsn)
    try:
        schema_store.init_schema()
    finally:
        schema_store.close()

    assert summary_store.get_completed_depth(repo, "dev") == 2
    assert summary_store.get_completed_layout(repo, "dev") is None


# ── PRI-167: embedding в SummaryStore + HNSW-индекс ──────────────────────────

@pytest.fixture()
def store_pri167():
    dsn = Settings().pg_dsn
    repo = f"test/summary-store/{uuid4().hex}"
    schema_store = ChunkStore(dsn)
    try:
        schema_store.init_schema()  # создаёт таблицу + HNSW-индекс
    finally:
        schema_store.close()
    summary_store = SummaryStore(dsn)
    try:
        _delete_summary_rows(summary_store, repo)
        yield summary_store, repo
    finally:
        try:
            _delete_summary_rows(summary_store, repo)
        finally:
            summary_store.close()


def test_upsert_writes_embedding_and_search_returns_nearest_first(store_pri167):
    summary_store, repo = store_pri167
    summary_store.upsert_summary(repo, "dev", "auth", "Авторизация", "...",
                                 ["auth/a.py#A"], "h-auth", embedding=_vec(0))
    summary_store.upsert_summary(repo, "dev", "index", "Индекс", "...",
                                 ["index/b.py#B"], "h-index", embedding=_vec(500))
    hits = summary_store.search_summaries(repo, "dev", _vec(0), top_k=1)
    assert [h["cluster_key"] for h in hits] == ["auth"]
    assert hits[0]["source_hash"] == "h-auth"
    assert summary_store.count_summaries(repo, "dev") == 2


def test_upsert_none_embedding_preserves_existing(store_pri167):
    summary_store, repo = store_pri167
    summary_store.upsert_summary(repo, "dev", "auth", "Авторизация", "v1",
                                 ["auth/a.py#A"], "h1", embedding=_vec(0))
    # повторный upsert с embedding=None не должен обнулить вектор
    summary_store.upsert_summary(repo, "dev", "auth", "Авторизация", "v2",
                                 ["auth/a.py#A"], "h1", embedding=None)
    hits = summary_store.search_summaries(repo, "dev", _vec(0), top_k=1)
    assert hits and hits[0]["cluster_key"] == "auth"
    assert hits[0]["summary"] == "v2"          # текст обновился, вектор сохранён


def test_upsert_none_embedding_resets_vector_when_source_hash_changes(store_pri167):
    summary_store, repo = store_pri167
    summary_store.upsert_summary(repo, "dev", "auth", "Авторизация", "v1",
                                 ["auth/a.py#A"], "h1", embedding=_vec(0))

    summary_store.upsert_summary(repo, "dev", "auth", "Авторизация", "v2",
                                 ["auth/a.py#A"], "h2", embedding=None)

    assert [p["cluster_key"] for p in summary_store.get_pending_embeddings(repo, "dev")] == [
        "auth"
    ]
    assert summary_store.search_summaries(repo, "dev", _vec(0), top_k=1) == []


def test_set_embedding_if_source_hash_rejects_stale_and_accepts_current(store_pri167):
    summary_store, repo = store_pri167
    summary_store.upsert_summary(repo, "dev", "auth", "Авторизация", "...",
                                 ["auth/a.py#A"], "current", embedding=None)

    assert summary_store.set_embedding_if_source_hash(
        repo, "dev", "auth", "stale", _vec(0)
    ) is False
    assert summary_store.get_pending_embeddings(repo, "dev")
    assert summary_store.set_embedding_if_source_hash(
        repo, "dev", "auth", "current", _vec(0)
    ) is True

    assert summary_store.get_pending_embeddings(repo, "dev") == []
    assert summary_store.search_summaries(repo, "dev", _vec(0), top_k=1)[0][
        "cluster_key"
    ] == "auth"


def test_upsert_can_atomically_reset_same_hash_embedding_for_backfill(store_pri167):
    summary_store, repo = store_pri167
    summary_store.upsert_summary(
        repo,
        "dev",
        "auth",
        "Старый заголовок",
        "Старый текст",
        ["auth/a.py#A"],
        "same-hash",
        embedding=_vec(0),
    )
    summary_store.upsert_summary(
        repo,
        "dev",
        "auth",
        "Новый заголовок",
        "Новый текст",
        ["auth/a.py#A"],
        "same-hash",
        embedding=None,
        preserve_embedding=False,
    )

    assert [item["cluster_key"] for item in summary_store.get_pending_embeddings(
        repo, "dev"
    )] == ["auth"]
    assert summary_store.search_summaries(repo, "dev", _vec(0), top_k=1) == []


def test_commit_summary_bundle_atomically_resets_same_hash_embedding(store_pri167):
    summary_store, repo = store_pri167
    summary_store.upsert_summary(
        repo,
        "dev",
        "auth",
        "Старый заголовок",
        "Старый текст",
        ["auth/a.py#A"],
        "same-hash",
        embedding=_vec(0),
    )

    summary_store.commit_summary_bundle(
        repo,
        "dev",
        "auth",
        "Новый заголовок",
        "Новый текст",
        ["auth/a.py#A"],
        "same-hash",
        current_fingerprints={"auth/a.py": "file-hash"},
        new_fragments=[
            {
                "path": "auth/a.py",
                "fingerprint": "file-hash",
                "summary": "Файл авторизации",
                "provenance": {},
            }
        ],
    )

    assert [item["cluster_key"] for item in summary_store.get_pending_embeddings(
        repo, "dev"
    )] == ["auth"]
    assert summary_store.search_summaries(repo, "dev", _vec(0), top_k=1) == []


def test_set_embedding_if_source_hash_can_cas_exact_summary_text(store_pri167):
    summary_store, repo = store_pri167
    summary_store.upsert_summary(
        repo,
        "dev",
        "auth",
        "Авторизация",
        "Текущий текст",
        ["auth/a.py#A"],
        "same-hash",
        embedding=None,
    )

    [snapshot] = summary_store.get_pending_embeddings(repo, "dev")
    assert snapshot == {
        "cluster_key": "auth",
        "title": "Авторизация",
        "summary": "Текущий текст",
        "source_hash": "same-hash",
    }
    summary_store.upsert_summary(
        repo,
        "dev",
        "auth",
        "Авторизация",
        "Конкурирующий текст",
        ["auth/a.py#A"],
        "same-hash",
        embedding=None,
        preserve_embedding=False,
    )

    assert summary_store.set_embedding_if_source_hash(
        repo,
        "dev",
        "auth",
        snapshot["source_hash"],
        _vec(0),
        title=snapshot["title"],
        summary=snapshot["summary"],
    ) is False
    assert summary_store.get_pending_embeddings(repo, "dev")

    assert summary_store.set_embedding_if_source_hash(
        repo,
        "dev",
        "auth",
        "same-hash",
        _vec(0),
        title="Авторизация",
        summary="Конкурирующий текст",
    ) is True
    assert summary_store.get_pending_embeddings(repo, "dev") == []


def test_pending_and_set_embedding_backfill(store_pri167):
    summary_store, repo = store_pri167
    summary_store.upsert_summary(repo, "dev", "legacy", "Легаси", "...",
                                 [], "h-legacy", embedding=None)  # без вектора
    pending = summary_store.get_pending_embeddings(repo, "dev")
    assert [p["cluster_key"] for p in pending] == ["legacy"]
    summary_store.set_embedding(repo, "dev", "legacy", _vec(3))
    assert summary_store.get_pending_embeddings(repo, "dev") == []
    hits = summary_store.search_summaries(repo, "dev", _vec(3), top_k=1)
    assert hits[0]["cluster_key"] == "legacy"
