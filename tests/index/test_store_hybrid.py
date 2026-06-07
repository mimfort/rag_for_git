import pytest
from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore, ChunkRow

def _row(ref, path, fqn, text, vec):
    return ChunkRow(ref=ref, content_hash=fqn+ref, path=path, lang="python",
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
        query_text="parse token", query_embedding=base_vec,
        overlay_ref="pr:1", changed_paths=["a.py"], top_k=5, candidates=20,
    )
    paths_texts = {(r.path, r.text) for r in res}
    assert ("a.py", "def f_a(): return NEW_parse_token()") in paths_texts
    assert ("a.py", "def f_a(): return parse_token()") not in paths_texts
