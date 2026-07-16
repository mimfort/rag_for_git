from uuid import uuid4

import psycopg
import pytest

from reviewer.config.settings import Settings
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
            finally:
                store.close()


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
