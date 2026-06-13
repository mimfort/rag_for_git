"""Integration: TaskStore + TaskGraph на живых Postgres+Neo4j (без Voyage).

Эмбеддер фейковый (детерминированный 1024-вектор) — проверяем SQL/Cypher, не Voyage.
Требует docker compose up -d. Маркер integration (исключён из дефолтного прогона).
"""
from __future__ import annotations

import hashlib

import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore
from reviewer.tasks.store import TaskRow, TaskStore, build_task_text, task_content_hash

pytestmark = pytest.mark.integration


def _vec(seed: str) -> list[float]:
    h = hashlib.sha256(seed.encode()).digest()
    # детерминированный ненулевой 1024-вектор
    return [((h[i % len(h)] + i) % 17) / 17.0 for i in range(1024)]


class _FakeEmbedder:
    def embed_documents(self, texts):
        return [_vec(t) for t in texts]

    def embed_query(self, text):
        return _vec(text)


@pytest.fixture()
def store():
    s = Settings()
    cs = ChunkStore(s.pg_dsn)
    cs.init_schema()  # создаёт таблицу tasks (schema.sql)
    cs.close()
    st = TaskStore(s.pg_dsn)
    with st._connect() as conn:
        conn.execute("TRUNCATE tasks RESTART IDENTITY")
        conn.commit()
    yield st
    st.close()


def test_taskstore_upsert_and_search(store):
    emb = _FakeEmbedder()
    text = build_task_text("Add logout", "Clear the session on logout", [])
    store.upsert_task(TaskRow(
        key="ID-1", aliases=["PRI-1"], title="Add logout",
        description="Clear the session on logout", status="Open", url=None,
        content_hash=task_content_hash(text), text=text,
        embedding=emb.embed_documents([text])[0]))

    assert store.existing_hash("ID-1") == task_content_hash(text)
    hits = store.search("logout session", emb.embed_query("logout session"), top_k=5)
    assert any(h.key == "ID-1" for h in hits)
