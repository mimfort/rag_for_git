import subprocess

import pytest

from reviewer.app import build_components
from reviewer.config.settings import Settings
from reviewer.gitutil import file_at_ref, list_python_files
from reviewer.index.freshness import update_base
from reviewer.index.refs import base_ref


_REPO = "test/pipeline"


@pytest.mark.integration
def test_index_then_hybrid_retrieve_finds_relevant_symbol(tmp_path):
    (tmp_path / "auth.py").write_text("def verify_token(t):\n    return t == 'ok'\n")
    (tmp_path / "util.py").write_text("def add(a,b):\n    return a+b\n")
    for args in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c"],
    ):
        subprocess.run(args, cwd=tmp_path, check=True)

    settings = Settings()
    components = build_components(settings, connect=False)
    try:
        components.store.init_schema()
        components.store.clear(_REPO)
        files = list_python_files(str(tmp_path), "HEAD")
        update_base(
            components.store,
            components.embedder,
            _REPO,
            "HEAD",
            files,
            read=lambda path: file_at_ref(str(tmp_path), path, "HEAD"),
        )
        query_vector = components.embedder.embed_query("token verification")
        hits = components.store.hybrid_search(
            _REPO,
            query_text="token verification",
            query_embedding=query_vector,
            overlay_ref="",
            changed_paths=[],
            top_k=5,
            base_ref=base_ref("HEAD"),
        )
        assert any(hit.symbol_fqn == "verify_token" for hit in hits)
    finally:
        try:
            components.store.clear(_REPO)
        finally:
            try:
                with components.store._connect() as conn:
                    conn.execute("DELETE FROM index_meta WHERE repo=%s", (_REPO,))
                    conn.commit()
            finally:
                try:
                    components.store.close()
                finally:
                    try:
                        components.task_store.close()
                    finally:
                        components.summary_store.close()
