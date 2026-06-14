import psycopg
import pytest
from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore, ChunkRow

def _row(ref, path, fqn, text, vec, repo="a/x"):
    return ChunkRow(repo=repo, ref=ref, content_hash=fqn+ref+repo, path=path, lang="python",
                    symbol_fqn=fqn, kind="function", start_line=1, end_line=2,
                    text=text, embedding=vec)

@pytest.mark.integration
def test_overlay_shadows_base_for_changed_paths():
    s = Settings()
    store = ChunkStore(s.pg_dsn); store.init_schema()
    store.clear()
    d = s.embedding_dim
    base_vec = [0.0]*d; base_vec[0] = 1.0
    store.upsert([
        _row("base", "a.py", "f_a", "def f_a(): return parse_token()", base_vec),
        _row("base", "b.py", "f_b", "def f_b(): pass", [0.0]*d),
        _row("pr:1", "a.py", "f_a", "def f_a(): return NEW_parse_token()", base_vec),
    ])
    res = store.hybrid_search(
        "a/x", query_text="parse token", query_embedding=base_vec,
        overlay_ref="pr:1", changed_paths=["a.py"], top_k=5, candidates=20,
    )
    paths_texts = {(r.path, r.text) for r in res}
    assert ("a.py", "def f_a(): return NEW_parse_token()") in paths_texts
    assert ("a.py", "def f_a(): return parse_token()") not in paths_texts

@pytest.mark.integration
def test_delete_ref_removes_only_target_ref():
    """delete_ref удаляет только чанки указанного ref, не трогая остальные."""
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    store.clear()
    d = s.embedding_dim
    vec = [0.0] * d
    store.upsert([
        _row("base", "x.py", "f_x", "def f_x(): pass", vec),
        _row("pr:42", "x.py", "f_x", "def f_x(): return 1", vec),
        _row("pr:42", "y.py", "f_y", "def f_y(): pass", vec),
    ])
    store.delete_ref("a/x", "pr:42")
    with psycopg.connect(s.pg_dsn) as conn:
        remaining = conn.execute(
            "SELECT ref, path FROM chunks ORDER BY ref, path"
        ).fetchall()
    assert remaining == [("base", "x.py")]


@pytest.mark.integration
def test_delete_missing_symbols_removes_stale_only():
    """delete_missing_symbols удаляет только символы path, отсутствующие в keep_fqns."""
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    store.clear()
    d = s.embedding_dim
    vec = [0.0] * d
    store.upsert([
        _row("base", "mod.py", "alpha", "def alpha(): pass", vec),
        _row("base", "mod.py", "beta", "def beta(): pass", vec),
        _row("base", "mod.py", "gamma", "def gamma(): pass", vec),
        _row("base", "other.py", "delta", "def delta(): pass", vec),
    ])
    # Оставляем только alpha и beta; gamma должна исчезнуть
    store.delete_missing_symbols("a/x", "base", "mod.py", ["alpha", "beta"])
    with psycopg.connect(s.pg_dsn) as conn:
        remaining = {(r[0], r[1]) for r in conn.execute(
            "SELECT path, symbol_fqn FROM chunks WHERE ref='base' ORDER BY path, symbol_fqn"
        ).fetchall()}
    assert ("mod.py", "alpha") in remaining
    assert ("mod.py", "beta") in remaining
    assert ("mod.py", "gamma") not in remaining
    assert ("other.py", "delta") in remaining  # не трогаем другой path


@pytest.mark.integration
def test_delete_missing_symbols_empty_keep_fqns_removes_all():
    """delete_missing_symbols с пустым keep_fqns удаляет все чанки path."""
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    store.clear()
    d = s.embedding_dim
    vec = [0.0] * d
    store.upsert([
        _row("base", "mod.py", "alpha", "def alpha(): pass", vec),
        _row("base", "mod.py", "beta", "def beta(): pass", vec),
        _row("base", "other.py", "delta", "def delta(): pass", vec),
    ])
    store.delete_missing_symbols("a/x", "base", "mod.py", [])
    with psycopg.connect(s.pg_dsn) as conn:
        remaining = {(r[0], r[1]) for r in conn.execute(
            "SELECT path, symbol_fqn FROM chunks WHERE ref='base'"
        ).fetchall()}
    assert ("mod.py", "alpha") not in remaining
    assert ("mod.py", "beta") not in remaining
    assert ("other.py", "delta") in remaining


@pytest.mark.integration
def test_delete_paths_except_removes_unlisted_paths():
    """delete_paths_except удаляет пути, не входящие в keep_paths."""
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    store.clear()
    d = s.embedding_dim
    vec = [0.0] * d
    store.upsert([
        _row("base", "a.py", "fa", "def fa(): pass", vec),
        _row("base", "b.py", "fb", "def fb(): pass", vec),
        _row("base", "c.py", "fc", "def fc(): pass", vec),
        _row("pr:1", "a.py", "fa", "def fa(): pass", vec),
    ])
    store.delete_paths_except("a/x", "base", ["a.py", "b.py"])
    with psycopg.connect(s.pg_dsn) as conn:
        remaining = {(r[0], r[1]) for r in conn.execute(
            "SELECT ref, path FROM chunks ORDER BY ref, path"
        ).fetchall()}
    assert ("base", "a.py") in remaining
    assert ("base", "b.py") in remaining
    assert ("base", "c.py") not in remaining   # удалён
    assert ("pr:1", "a.py") in remaining        # другой ref — не трогаем


@pytest.mark.integration
def test_delete_paths_except_empty_keep_is_noop():
    """delete_paths_except с пустым keep_paths — no-op, ничего не удаляется."""
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    store.clear()
    d = s.embedding_dim
    vec = [0.0] * d
    store.upsert([
        _row("base", "a.py", "fa", "def fa(): pass", vec),
        _row("base", "b.py", "fb", "def fb(): pass", vec),
    ])
    store.delete_paths_except("a/x", "base", [])
    with psycopg.connect(s.pg_dsn) as conn:
        count = conn.execute("SELECT count(*) FROM chunks WHERE ref='base'").fetchone()[0]
    assert count == 2


@pytest.mark.integration
def test_two_repo_isolation():
    """hybrid_search фильтрует по repo: результаты одного репо не попадают в другое."""
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    store.clear()
    d = s.embedding_dim
    vec = [0.0] * d
    vec[0] = 1.0

    # Один и тот же path#fqn под двумя репозиториями
    store.upsert([
        _row("base", "mod.py", "func", "def func(): return repo_ax()", vec, repo="a/x"),
        _row("base", "mod.py", "func", "def func(): return repo_by()", vec, repo="b/y"),
    ])

    res_ax = store.hybrid_search(
        "a/x", query_text="repo_ax", query_embedding=vec,
        overlay_ref="pr:0", changed_paths=[], top_k=10, candidates=20,
    )
    res_by = store.hybrid_search(
        "b/y", query_text="repo_by", query_embedding=vec,
        overlay_ref="pr:0", changed_paths=[], top_k=10, candidates=20,
    )

    texts_ax = {r.text for r in res_ax}
    texts_by = {r.text for r in res_by}

    assert any("repo_ax" in t for t in texts_ax), "a/x должен вернуть свои чанки"
    assert not any("repo_by" in t for t in texts_ax), "a/x не должен видеть чанки b/y"
    assert any("repo_by" in t for t in texts_by), "b/y должен вернуть свои чанки"
    assert not any("repo_ax" in t for t in texts_by), "b/y не должен видеть чанки a/x"


@pytest.mark.integration
def test_two_branch_isolation():
    """hybrid_search с разными base_ref изолирует ветки одного репо."""
    s = Settings()
    store = ChunkStore(s.pg_dsn)
    store.init_schema()
    store.clear()
    d = s.embedding_dim
    vec = [0.0] * d
    vec[0] = 1.0
    store.upsert([
        _row("base:main", "mod.py", "func", "def func(): return on_main()", vec),
        _row("base:master", "mod.py", "func", "def func(): return on_master()", vec),
    ])
    res_main = store.hybrid_search(
        "a/x", query_text="func", query_embedding=vec,
        overlay_ref="pr:0", changed_paths=[], top_k=10, candidates=20,
        base_ref="base:main",
    )
    texts = {r.text for r in res_main}
    assert any("on_main" in t for t in texts)
    assert not any("on_master" in t for t in texts)
