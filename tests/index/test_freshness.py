from reviewer.index.freshness import build_overlay
from reviewer.index.models import Chunk

class FakeEmb:
    def embed_documents(self, texts): return [[0.0]*4 for _ in texts]

class FakeStore:
    def __init__(self): self.rows=[]
    def existing_hashes(self, ref): return set()
    def upsert(self, rows): self.rows.extend(rows)

def test_build_overlay_chunks_changed_files_into_pr_ref(monkeypatch):
    files = {"a.py": "def f():\n    return 1\n"}
    store, emb = FakeStore(), FakeEmb()
    build_overlay(store, emb, pr_number=7,
                  changed_files=list(files),
                  read_head=lambda p: files.get(p))
    assert all(r.ref == "pr:7" for r in store.rows)
    assert any(r.symbol_fqn == "f" for r in store.rows)
