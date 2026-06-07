import pytest
from reviewer.config.settings import Settings
from reviewer.app import build_components
from reviewer.index.freshness import update_base

@pytest.mark.integration
def test_index_then_hybrid_retrieve_finds_relevant_symbol(tmp_path):
    (tmp_path/"auth.py").write_text("def verify_token(t):\n    return t == 'ok'\n")
    (tmp_path/"util.py").write_text("def add(a,b):\n    return a+b\n")
    import subprocess
    for a in (["git","init","-q"],["git","add","-A"],
              ["git","-c","user.email=t@t","-c","user.name=t","commit","-qm","c"]):
        subprocess.run(a, cwd=tmp_path, check=True)
    s = Settings()
    c = build_components(s, connect=False); c.store.init_schema(); c.store.clear()
    from reviewer.gitutil import list_python_files, file_at_ref
    files = list_python_files(str(tmp_path), "HEAD")
    update_base(c.store, c.embedder, str(tmp_path), "HEAD", files,
                read=lambda p: file_at_ref(str(tmp_path), p, "HEAD"))
    qvec = c.embedder.embed_query("token verification")
    hits = c.store.hybrid_search(query_text="token verification",
                                 query_embedding=qvec, overlay_ref="",
                                 changed_paths=[], top_k=5)
    assert any(h.symbol_fqn == "verify_token" for h in hits)
