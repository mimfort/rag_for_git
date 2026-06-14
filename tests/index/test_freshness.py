from reviewer.index.freshness import build_overlay, update_base


class FakeEmb:
    def __init__(self):
        self.calls: list[list[str]] = []

    def embed_documents(self, texts):
        self.calls.append(list(texts))
        return [[0.0]*4 for _ in texts]


class FakeStore:
    """Фейковый store с рекордерами вызовов методов гигиены индекса."""

    def __init__(self):
        self.rows: list = []
        self.deleted_paths: list[tuple[str, str, list[str]]] = []          # (repo, ref, paths)
        self.deleted_missing: list[tuple[str, str, str, list[str]]] = []   # (repo, ref, path, keep_fqns)
        self.cached_embeddings: dict[str, list[float]] = {}                # content_hash -> vector

    def existing_hashes(self, repo, ref): return set()
    def upsert(self, rows): self.rows.extend(rows)

    def find_embeddings_by_hashes(self, repo, hashes):
        return {h: self.cached_embeddings[h] for h in hashes if h in self.cached_embeddings}

    def delete_paths(self, repo, ref, paths):
        if paths:
            self.deleted_paths.append((repo, ref, list(paths)))

    def delete_missing_symbols(self, repo, ref, path, keep_fqns):
        self.deleted_missing.append((repo, ref, path, list(keep_fqns)))


def test_build_overlay_chunks_changed_files_into_pr_ref(monkeypatch):
    files = {"a.py": "def f():\n    return 1\n"}
    store, emb = FakeStore(), FakeEmb()
    build_overlay(store, emb, repo="a/x", pr_number=7,
                  changed_files=list(files),
                  head_sources=files)
    assert all(r.ref == "pr:7" for r in store.rows)
    assert any(r.symbol_fqn == "f" for r in store.rows)


def test_build_overlay_skips_missing_or_empty_sources():
    """Файлы без содержимого в head_sources не попадают в overlay."""
    store, emb = FakeStore(), FakeEmb()
    build_overlay(
        store, emb, repo="a/x", pr_number=3,
        changed_files=["present.py", "absent.py", "empty.py"],
        head_sources={"present.py": "def ok(): pass\n", "empty.py": ""},
    )
    assert all(r.path == "present.py" for r in store.rows)
    assert any(r.symbol_fqn == "ok" for r in store.rows)


def test_build_overlay_sets_repo_on_rows():
    store, emb = FakeStore(), FakeEmb()
    build_overlay(store, emb, repo="a/x", pr_number=7,
                  changed_files=["a.py"], head_sources={"a.py": "def f():\n    return 1\n"})
    assert store.rows and all(r.repo == "a/x" and r.ref == "pr:7" for r in store.rows)


# --- update_base: гигиена removed_files ---

def test_update_base_removed_files_calls_delete_paths():
    """removed_files с .py вызывает delete_paths("a/x", "base", [...])."""
    store, emb = FakeStore(), FakeEmb()
    update_base(store, emb, repo="a/x", target_ref="main",
                changed_files=[],
                read=lambda p: None,
                removed_files=["old.py", "readme.md"])
    assert ("a/x", "base:main", ["old.py"]) in store.deleted_paths


def test_update_base_removed_files_skips_non_py():
    """removed_files без .py — delete_paths не вызывается."""
    store, emb = FakeStore(), FakeEmb()
    update_base(store, emb, repo="a/x", target_ref="main",
                changed_files=[],
                read=lambda p: None,
                removed_files=["readme.md", "setup.cfg"])
    assert store.deleted_paths == []


def test_update_base_removed_files_empty_is_noop():
    """Пустой removed_files — delete_paths не вызывается."""
    store, emb = FakeStore(), FakeEmb()
    update_base(store, emb, repo="a/x", target_ref="main",
                changed_files=[],
                read=lambda p: None)
    assert store.deleted_paths == []


# --- update_base: read→None для файла из changed_files ---

def test_update_base_read_none_calls_delete_paths():
    """Если read(path) вернул None — delete_paths("a/x", "base", [path]) для этого файла."""
    store, emb = FakeStore(), FakeEmb()
    update_base(store, emb, repo="a/x", target_ref="main",
                changed_files=["gone.py"],
                read=lambda p: None)
    assert ("a/x", "base:main", ["gone.py"]) in store.deleted_paths


def test_update_base_read_none_does_not_upsert():
    """Недоступный файл — ничего не добавляется в индекс."""
    store, emb = FakeStore(), FakeEmb()
    update_base(store, emb, repo="a/x", target_ref="main",
                changed_files=["gone.py"],
                read=lambda p: None)
    assert store.rows == []


# --- update_base: delete_missing_symbols после обработки файла ---

def test_update_base_calls_delete_missing_symbols_with_actual_fqns():
    """После обработки файла вызывается delete_missing_symbols с актуальными fqn."""
    src = "def alpha():\n    pass\ndef beta():\n    pass\n"
    store, emb = FakeStore(), FakeEmb()
    update_base(store, emb, repo="a/x", target_ref="main",
                changed_files=["mod.py"],
                read=lambda p: src if p == "mod.py" else None)
    assert len(store.deleted_missing) == 1
    repo, ref, path, keep_fqns = store.deleted_missing[0]
    assert repo == "a/x"
    assert ref == "base:main"
    assert path == "mod.py"
    assert set(keep_fqns) == {"alpha", "beta"}


def test_update_base_non_py_files_not_processed():
    """Не-.py файлы в changed_files пропускаются без вызовов гигиены."""
    store, emb = FakeStore(), FakeEmb()
    update_base(store, emb, repo="a/x", target_ref="main",
                changed_files=["config.yaml", "data.json"],
                read=lambda p: "content")
    assert store.deleted_paths == []
    assert store.deleted_missing == []
    assert store.rows == []


def test_update_base_reuses_cached_embedding_across_branches():
    """Чанк с известным content_hash не переэмбеддится — вектор берётся из кэша."""
    from reviewer.index.models import Chunk
    src = "def alpha():\n    pass\n"
    # вычисляем content_hash так же, как chunker
    h = Chunk(path="mod.py", lang="python", symbol_fqn="alpha", kind="function",
              start_line=1, end_line=2, text="def alpha():\n    pass").content_hash
    store, emb = FakeStore(), FakeEmb()
    store.cached_embeddings[h] = [9.0, 9.0, 9.0, 9.0]
    update_base(store, emb, repo="a/x", target_ref="master",
                changed_files=["mod.py"], read=lambda p: src)
    # эмбеддер не звался (единственный чанк взят из кэша)
    assert emb.calls == []
    assert any(r.embedding == [9.0, 9.0, 9.0, 9.0] for r in store.rows)
