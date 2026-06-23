from reviewer.index.freshness import build_overlay, update_base


class _FakeStore:
    def __init__(self):
        self.upserted = []
        self.deleted = []

    def existing_hashes(self, repo, ref):
        return set()

    def find_embeddings_by_hashes(self, repo, hashes):
        return {}

    def upsert(self, rows):
        self.upserted.extend(rows)

    def delete_paths(self, repo, ref, paths):
        self.deleted.extend(paths)

    def delete_missing_symbols(self, repo, ref, path, keep_fqns):
        pass


class _FakeEmbedder:
    def embed_documents(self, texts):
        return [[0.0] * 1024 for _ in texts]


def test_build_overlay_skips_ignored_paths():
    store, emb = _FakeStore(), _FakeEmbedder()
    build_overlay(
        store, emb, "o/r", 1,
        ["vendor/x.py", "reviewer/a.py"],
        head_sources={"vendor/x.py": "def a():\n    pass\n",
                      "reviewer/a.py": "def b():\n    pass\n"},
        ignore=["vendor"],
    )
    paths = {r.path for r in store.upserted}
    assert "vendor/x.py" not in paths
    assert "reviewer/a.py" in paths


def test_update_base_skips_and_purges_newly_ignored():
    store, emb = _FakeStore(), _FakeEmbedder()
    sources = {"vendor/x.py": "def a():\n    pass\n",
               "reviewer/a.py": "def b():\n    pass\n"}
    update_base(
        store, emb, "o/r", "main",
        ["vendor/x.py", "reviewer/a.py"],
        read=lambda p: sources.get(p),
        ignore=["vendor"],
    )
    paths = {r.path for r in store.upserted}
    assert "vendor/x.py" not in paths
    assert "reviewer/a.py" in paths
    assert "vendor/x.py" in store.deleted   # ставший игнор-путь вычищается из base
