from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Event
from unittest.mock import Mock
from uuid import uuid4

import psycopg
import pytest

from reviewer.config.settings import Settings
from reviewer.index.freshness import update_base
from reviewer.index.store import ChunkRow, ChunkStore


def _row(ref, path, fqn, text, vec, *, repo):
    return ChunkRow(
        repo=repo,
        ref=ref,
        content_hash=fqn + ref + repo,
        path=path,
        lang="python",
        symbol_fqn=fqn,
        kind="function",
        start_line=1,
        end_line=2,
        text=text,
        embedding=vec,
    )


@pytest.fixture
def store_and_settings():
    settings = Settings()
    repo = f"test/store-hybrid-{uuid4().hex}"
    other_repo = f"test/store-hybrid-other-{uuid4().hex}"
    store = ChunkStore(settings.pg_dsn)
    try:
        store.init_schema()
        store.clear(repo)
        store.clear(other_repo)
        yield store, settings, repo, other_repo
    finally:
        try:
            store.clear(repo)
        finally:
            try:
                store.clear(other_repo)
                with store._connect() as conn:
                    conn.execute("DELETE FROM index_meta WHERE repo = ANY(%s)", ([repo, other_repo],))
            finally:
                store.close()


@pytest.mark.integration
def test_incremental_index_repairs_existing_coordinates_like_clean_index(
    store_and_settings,
):
    store, settings, repo, _ = store_and_settings
    embedding_provider = Mock()
    vector = [1.0] + [0.0] * (settings.embedding_dim - 1)
    embedding_provider.embed_documents.side_effect = lambda texts: [vector for _ in texts]
    unchanged = "def kept():\n    return 1\n"
    files = {
        "a.py": unchanged + "\ndef removed(): pass\n",
        "b.py": unchanged,
        "moving.py": "def moved():\n    return 42\n",
    }
    paths = list(files)
    update_base(store, embedding_provider, repo, "dev", paths, files.get)
    assert store.chunk_locations(repo, "base:dev", paths)["a.py", "kept"][1:3] == (1, 2)
    assert embedding_provider.embed_documents.call_count == 1
    assert len(embedding_provider.embed_documents.call_args.args[0]) == 3

    files["a.py"] = "# inserted before kept\n\n" + unchanged
    files["moved.py"] = files.pop("moving.py")
    paths = list(files)
    update_base(store, embedding_provider, repo, "dev", paths, files.get, ["moving.py"])
    update_base(store, embedding_provider, repo, "clean", paths, files.get)

    repaired = store.chunk_locations(repo, "base:dev", paths)
    assert repaired == store.chunk_locations(repo, "base:clean", paths)
    assert repaired["a.py", "kept"][1:3] == (3, 4)
    assert repaired["b.py", "kept"][1:3] == (1, 2)
    assert repaired["moved.py", "moved"][1:3] == (1, 2)
    assert ("a.py", "removed") not in repaired
    assert store.chunk_locations(repo, "base:dev", ["moving.py"]) == {}
    assert embedding_provider.embed_documents.call_count == 1


def _snapshot_rows(store, repo):
    return sorted((hit.path, hit.text) for hit in store.bm25_search(
        repo, "snapshotmarker", base_ref="base:dev", top_k=10,
    ))


def _fixture_embedder(dim):
    embedder = Mock()
    vector = [1.0] + [0.0] * (dim - 1)
    embedder.embed_documents.side_effect = lambda texts: [vector for _ in texts]
    return embedder


@pytest.mark.integration
def test_base_snapshot_publication_is_atomic_for_an_independent_reader(
    store_and_settings, monkeypatch,
):
    store, settings, repo, _ = store_and_settings
    reader = ChunkStore(settings.pg_dsn)
    embedder = _fixture_embedder(settings.embedding_dim)
    original = {"a.py": "def primary(): return 'snapshotmarker A'\n",
                "b.py": "def deleted(): return 'snapshotmarker obsolete'\n"}
    changed = {"a.py": "def primary(): return 'snapshotmarker B'\n"}
    update_base(store, embedder, repo, "dev", list(original), original.get)
    store.set_index_meta(repo, "base:dev", "sha-a")
    expected_meta = store.get_index_meta_row(repo, "base:dev")
    initial = _snapshot_rows(reader, repo)
    assert len(initial) == 2
    observed = []
    fail = True
    original_connect = store._connect

    class ObservedConnection:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, query, params=None):
            if query.strip().startswith("DELETE FROM chunks") and params and params[:2] == (repo, "base:dev"):
                observed.append((reader.get_index_meta_row(repo, "base:dev"), _snapshot_rows(reader, repo)))
                if fail:
                    raise RuntimeError("fixture: prune failed")
            return self._conn.execute(query, params)

    @contextmanager
    def connect():
        with original_connect() as conn:
            yield ObservedConnection(conn)

    try:
        monkeypatch.setattr(store, "_connect", connect)
        with pytest.raises(RuntimeError, match="prune failed"):
            update_base(store, embedder, repo, "dev", list(changed), changed.get, ["b.py"],
                        indexed_sha="sha-b", expected_meta=expected_meta)
        assert observed == [(expected_meta, initial)]
        assert _snapshot_rows(reader, repo) == initial
        assert reader.get_index_meta_row(repo, "base:dev") == expected_meta

        fail = False
        update_base(store, embedder, repo, "dev", list(changed), changed.get, ["b.py"],
                    indexed_sha="sha-b", expected_meta=expected_meta)
        assert observed and all(snapshot == (expected_meta, initial) for snapshot in observed)
        assert _snapshot_rows(reader, repo) == [("a.py", changed["a.py"].strip())]
        assert reader.get_index_meta(repo, "base:dev") == "sha-b"
    finally:
        reader.close()


@pytest.mark.integration
@pytest.mark.parametrize("has_previous_snapshot", [True, False])
def test_base_snapshot_publication_rejects_a_stale_concurrent_plan(
    store_and_settings, has_previous_snapshot,
):
    store, settings, repo, _ = store_and_settings
    embedder = _fixture_embedder(settings.embedding_dim)
    delayed = _fixture_embedder(settings.embedding_dim)
    ready = Event()
    release = Event()
    real_embed = delayed.embed_documents.side_effect

    def await_publish(texts):
        ready.set()
        assert release.wait(10), "fresh publisher was blocked during embedding"
        return real_embed(texts)

    delayed.embed_documents.side_effect = await_publish
    original = {"original.py": "def original(): return 'snapshotmarker A'\n"}
    if not has_previous_snapshot:
        original = {}
    fresh = {"fresh.py": "def added(): return 'snapshotmarker B'\n"}
    stale = {"stale.py": "def other(): return 'snapshotmarker C'\n"}
    if original:
        update_base(store, embedder, repo, "dev", list(original), original.get)
        store.set_index_meta(repo, "base:dev", "sha-a")
    expected_meta = store.get_index_meta_row(repo, "base:dev")
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(
            update_base, store, delayed, repo, "dev", list(stale), stale.get, list(original),
            indexed_sha="sha-c", expected_meta=expected_meta,
        )
        try:
            if not ready.wait(5) and pending.done():
                pending.result()
            assert ready.is_set()
            update_base(store, embedder, repo, "dev", list(fresh), fresh.get, list(original),
                        indexed_sha="sha-b", expected_meta=expected_meta)
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="индекс изменился"):
            pending.result(timeout=5)
    assert store.get_index_meta(repo, "base:dev") == "sha-b"
    assert _snapshot_rows(store, repo) == [("fresh.py", fresh["fresh.py"].strip())]


@pytest.mark.integration
def test_full_snapshot_can_publish_an_empty_python_tree(store_and_settings):
    store, settings, repo, other_repo = store_and_settings
    embedder = _fixture_embedder(settings.embedding_dim)
    sources = {"old.py": "def previous(): return 'snapshotmarker'\n"}
    for project, branch in [(repo, "dev"), (repo, "main"), (other_repo, "dev")]:
        update_base(store, embedder, project, branch, list(sources), sources.get,
                    indexed_sha="sha-a")
    expected_meta = store.get_index_meta_row(repo, "base:dev")

    update_base(store, embedder, repo, "dev", [], sources.get,
                indexed_sha="sha-b", expected_meta=expected_meta, full_snapshot=True)

    assert _snapshot_rows(store, repo) == []
    assert store.get_index_meta(repo, "base:dev") == "sha-b"
    assert len(_snapshot_rows(store, other_repo)) == 1
    assert store.count_chunks(repo, "base:main") == 1


@pytest.mark.integration
def test_overlay_shadows_base_for_changed_paths(store_and_settings):
    store, settings, repo, _ = store_and_settings
    d = settings.embedding_dim
    base_vec = [0.0] * d
    base_vec[0] = 1.0
    store.upsert(
        [
            _row(
                "base",
                "a.py",
                "f_a",
                "def f_a(): return parse_token()",
                base_vec,
                repo=repo,
            ),
            _row("base", "b.py", "f_b", "def f_b(): pass", [0.0] * d, repo=repo),
            _row(
                "pr:1",
                "a.py",
                "f_a",
                "def f_a(): return NEW_parse_token()",
                base_vec,
                repo=repo,
            ),
        ]
    )
    res = store.hybrid_search(
        repo,
        query_text="parse token",
        query_embedding=base_vec,
        overlay_ref="pr:1",
        changed_paths=["a.py"],
        top_k=5,
        candidates=20,
    )
    paths_texts = {(r.path, r.text) for r in res}
    assert ("a.py", "def f_a(): return NEW_parse_token()") in paths_texts
    assert ("a.py", "def f_a(): return parse_token()") not in paths_texts


@pytest.mark.integration
def test_delete_ref_removes_only_target_ref(store_and_settings):
    """delete_ref удаляет только чанки указанного ref, не трогая остальные."""
    store, settings, repo, _ = store_and_settings
    d = settings.embedding_dim
    vec = [0.0] * d
    store.upsert(
        [
            _row("base", "x.py", "f_x", "def f_x(): pass", vec, repo=repo),
            _row("pr:42", "x.py", "f_x", "def f_x(): return 1", vec, repo=repo),
            _row("pr:42", "y.py", "f_y", "def f_y(): pass", vec, repo=repo),
        ]
    )
    store.delete_ref(repo, "pr:42")
    with psycopg.connect(settings.pg_dsn) as conn:
        remaining = conn.execute(
            "SELECT ref, path FROM chunks WHERE repo=%s ORDER BY ref, path",
            (repo,),
        ).fetchall()
    assert remaining == [("base", "x.py")]


@pytest.mark.integration
def test_delete_missing_symbols_removes_stale_only(store_and_settings):
    """delete_missing_symbols удаляет только символы path, отсутствующие в keep_fqns."""
    store, settings, repo, _ = store_and_settings
    d = settings.embedding_dim
    vec = [0.0] * d
    store.upsert(
        [
            _row("base", "mod.py", "alpha", "def alpha(): pass", vec, repo=repo),
            _row("base", "mod.py", "beta", "def beta(): pass", vec, repo=repo),
            _row("base", "mod.py", "gamma", "def gamma(): pass", vec, repo=repo),
            _row("base", "other.py", "delta", "def delta(): pass", vec, repo=repo),
        ]
    )
    # Оставляем только alpha и beta; gamma должна исчезнуть
    store.delete_missing_symbols(repo, "base", "mod.py", ["alpha", "beta"])
    with psycopg.connect(settings.pg_dsn) as conn:
        remaining = {
            (r[0], r[1])
            for r in conn.execute(
                "SELECT path, symbol_fqn FROM chunks "
                "WHERE repo=%s AND ref='base' ORDER BY path, symbol_fqn",
                (repo,),
            ).fetchall()
        }
    assert ("mod.py", "alpha") in remaining
    assert ("mod.py", "beta") in remaining
    assert ("mod.py", "gamma") not in remaining
    assert ("other.py", "delta") in remaining  # не трогаем другой path


@pytest.mark.integration
def test_delete_missing_symbols_empty_keep_fqns_removes_all(store_and_settings):
    """delete_missing_symbols с пустым keep_fqns удаляет все чанки path."""
    store, settings, repo, _ = store_and_settings
    d = settings.embedding_dim
    vec = [0.0] * d
    store.upsert(
        [
            _row("base", "mod.py", "alpha", "def alpha(): pass", vec, repo=repo),
            _row("base", "mod.py", "beta", "def beta(): pass", vec, repo=repo),
            _row("base", "other.py", "delta", "def delta(): pass", vec, repo=repo),
        ]
    )
    store.delete_missing_symbols(repo, "base", "mod.py", [])
    with psycopg.connect(settings.pg_dsn) as conn:
        remaining = {
            (r[0], r[1])
            for r in conn.execute(
                "SELECT path, symbol_fqn FROM chunks WHERE repo=%s AND ref='base'",
                (repo,),
            ).fetchall()
        }
    assert ("mod.py", "alpha") not in remaining
    assert ("mod.py", "beta") not in remaining
    assert ("other.py", "delta") in remaining


@pytest.mark.integration
def test_delete_paths_except_removes_unlisted_paths(store_and_settings):
    """delete_paths_except удаляет пути, не входящие в keep_paths."""
    store, settings, repo, _ = store_and_settings
    d = settings.embedding_dim
    vec = [0.0] * d
    store.upsert(
        [
            _row("base", "a.py", "fa", "def fa(): pass", vec, repo=repo),
            _row("base", "b.py", "fb", "def fb(): pass", vec, repo=repo),
            _row("base", "c.py", "fc", "def fc(): pass", vec, repo=repo),
            _row("pr:1", "a.py", "fa", "def fa(): pass", vec, repo=repo),
        ]
    )
    store.delete_paths_except(repo, "base", ["a.py", "b.py"])
    with psycopg.connect(settings.pg_dsn) as conn:
        remaining = {
            (r[0], r[1])
            for r in conn.execute(
                "SELECT ref, path FROM chunks WHERE repo=%s ORDER BY ref, path",
                (repo,),
            ).fetchall()
        }
    assert ("base", "a.py") in remaining
    assert ("base", "b.py") in remaining
    assert ("base", "c.py") not in remaining  # удалён
    assert ("pr:1", "a.py") in remaining  # другой ref — не трогаем


@pytest.mark.integration
def test_delete_paths_except_empty_keep_is_noop(store_and_settings):
    """delete_paths_except с пустым keep_paths — no-op, ничего не удаляется."""
    store, settings, repo, _ = store_and_settings
    d = settings.embedding_dim
    vec = [0.0] * d
    store.upsert(
        [
            _row("base", "a.py", "fa", "def fa(): pass", vec, repo=repo),
            _row("base", "b.py", "fb", "def fb(): pass", vec, repo=repo),
        ]
    )
    store.delete_paths_except(repo, "base", [])
    with psycopg.connect(settings.pg_dsn) as conn:
        count = conn.execute(
            "SELECT count(*) FROM chunks WHERE repo=%s AND ref='base'",
            (repo,),
        ).fetchone()[0]
    assert count == 2


@pytest.mark.integration
def test_two_repo_isolation(store_and_settings):
    """hybrid_search фильтрует по repo: результаты одного репо не попадают в другое."""
    store, settings, repo, other_repo = store_and_settings
    d = settings.embedding_dim
    vec = [0.0] * d
    vec[0] = 1.0

    # Один и тот же path#fqn под двумя репозиториями
    store.upsert(
        [
            _row("base", "mod.py", "func", "def func(): return repo_ax()", vec, repo=repo),
            _row(
                "base",
                "mod.py",
                "func",
                "def func(): return repo_by()",
                vec,
                repo=other_repo,
            ),
        ]
    )

    res_ax = store.hybrid_search(
        repo,
        query_text="repo_ax",
        query_embedding=vec,
        overlay_ref="pr:0",
        changed_paths=[],
        top_k=10,
        candidates=20,
    )
    res_by = store.hybrid_search(
        other_repo,
        query_text="repo_by",
        query_embedding=vec,
        overlay_ref="pr:0",
        changed_paths=[],
        top_k=10,
        candidates=20,
    )

    texts_ax = {r.text for r in res_ax}
    texts_by = {r.text for r in res_by}

    assert any("repo_ax" in t for t in texts_ax), f"{repo} должен вернуть свои чанки"
    assert not any("repo_by" in t for t in texts_ax), f"{repo} не должен видеть чужие чанки"
    assert any("repo_by" in t for t in texts_by), f"{other_repo} должен вернуть свои чанки"
    assert not any("repo_ax" in t for t in texts_by), (
        f"{other_repo} не должен видеть чанки {repo}"
    )


@pytest.mark.integration
def test_fetch_nodes_at_returns_only_given_ref(store_and_settings):
    """fetch_nodes_at берёт текст строго указанного ref, не сливая base/overlay."""
    store, settings, repo, _ = store_and_settings
    d = settings.embedding_dim
    vec = [0.0] * d
    store.upsert(
        [
            _row(
                "base:dev",
                "svc.py",
                "f",
                "def f(a, b):\n    ...",
                vec,
                repo=repo,
            ),
            _row(
                "pr:1",
                "svc.py",
                "f",
                "def f(a, b, c):\n    ...",
                vec,
                repo=repo,
            ),
        ]
    )
    base = store.fetch_nodes_at(repo, ["svc.py#f"], "base:dev")
    overlay = store.fetch_nodes_at(repo, ["svc.py#f"], "pr:1")
    assert [n.text for n in base] == ["def f(a, b):\n    ..."]
    assert [n.text for n in overlay] == ["def f(a, b, c):\n    ..."]
    assert store.fetch_nodes_at(repo, [], "base:dev") == []


@pytest.mark.integration
def test_hybrid_search_surfaces_ann_distance_and_bm25_hit(store_and_settings):
    """hybrid_search заполняет ann_distance/bm25_hit (PRI-202) для каждого результата."""
    store, settings, repo, _ = store_and_settings
    d = settings.embedding_dim
    vec = [0.0] * d
    vec[0] = 1.0
    store.upsert(
        [
            _row(
                "base",
                "a.py",
                "f_login",
                "def f_login(): login token verify",
                vec,
                repo=repo,
            ),
            _row(
                "base",
                "b.py",
                "f_other",
                "def f_other(): pass",
                [0.0] * d,
                repo=repo,
            ),
        ]
    )
    hits = store.hybrid_search(
        repo,
        query_text="login token verify",
        query_embedding=vec,
        overlay_ref="pr:0",
        changed_paths=[],
        top_k=10,
        candidates=10,
    )
    assert hits
    for hit in hits:
        assert (hit.ann_distance is None) or isinstance(hit.ann_distance, float)
        assert isinstance(hit.bm25_hit, bool)
    # хотя бы один лексически совпавший чанк помечен bm25_hit
    assert any(hit.bm25_hit for hit in hits)


@pytest.mark.integration
def test_two_branch_isolation(store_and_settings):
    """hybrid_search с разными base_ref изолирует ветки одного репо."""
    store, settings, repo, _ = store_and_settings
    d = settings.embedding_dim
    vec = [0.0] * d
    vec[0] = 1.0
    store.upsert(
        [
            _row(
                "base:main",
                "mod.py",
                "func",
                "def func(): return on_main()",
                vec,
                repo=repo,
            ),
            _row(
                "base:master",
                "mod.py",
                "func",
                "def func(): return on_master()",
                vec,
                repo=repo,
            ),
        ]
    )
    res_main = store.hybrid_search(
        repo,
        query_text="func",
        query_embedding=vec,
        overlay_ref="pr:0",
        changed_paths=[],
        top_k=10,
        candidates=20,
        base_ref="base:main",
    )
    texts = {r.text for r in res_main}
    assert any("on_main" in text for text in texts)
    assert not any("on_master" in text for text in texts)


@pytest.mark.integration
def test_bm25_search_is_bounded_by_repository_branch_and_candidate_count(store_and_settings):
    store, settings, repo, other_repo = store_and_settings
    vector = [1.0] + [0.0] * (settings.embedding_dim - 1)
    text = "def lexicalfixture(): pass"
    store.upsert([
        _row(ref, path, "lexicalfixture", text, vector, repo=owner)
        for owner, ref, path in (
            (repo, "base:dev", "code.py"),
            (repo, "base:dev", "tests/test_code.py"),
            (repo, "base:main", "other_branch.py"),
            (repo, "pr:7", "overlay.py"),
            (other_repo, "base:dev", "other_repo.py"),
        )
    ])

    hits = store.bm25_search(repo, "lexicalfixture", top_k=2, base_ref="base:dev")

    assert {hit.path for hit in hits} == {"code.py", "tests/test_code.py"}
    assert all(hit.bm25_hit and hit.ann_distance is None for hit in hits)
    assert store.bm25_search(repo, "lexicalfixture", top_k=1, base_ref="base:dev") == hits[:1]


@pytest.mark.integration
def test_covering_lines_prefers_narrowest_chunk_in_requested_repo_and_branch(
    store_and_settings,
):
    store, settings, repo, other_repo = store_and_settings
    vec = [1.0] + [0.0] * (settings.embedding_dim - 1)
    path = "pkg/target.py"
    broad = replace(_row("base:dev", path, "container", "broad", vec, repo=repo),
                    start_line=1, end_line=50)
    narrow = replace(_row("base:dev", path, "container.target", "narrow", vec, repo=repo),
                     start_line=26, end_line=30)
    store.upsert([
        broad, narrow,
        replace(narrow, ref="base:main", symbol_fqn="other_branch", start_line=27, end_line=27),
        replace(narrow, repo=other_repo, symbol_fqn="other_repo", start_line=27, end_line=27),
    ])
    result = store.fetch_retrieved_covering_lines(
        repo, {path: 27, "pkg/missing.py": 26}, base_ref="base:dev",
    )
    assert [(r.path, r.symbol_fqn, r.start_line, r.end_line) for r in result] == [
        (path, "container.target", 26, 30),
    ]
    assert store.fetch_retrieved_covering_lines(repo, {path: 51}, base_ref="base:dev") == []
