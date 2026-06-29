"""Интеграционный тест round-trip tasks.attachments (jsonb) — PRI-196."""
import pytest

from reviewer.config.settings import Settings
from reviewer.index.store import ChunkStore
from reviewer.tasks.store import TaskRow, TaskStore


@pytest.mark.integration
def test_attachments_roundtrip():
    s = Settings()
    # Применяем схему (включая новую колонку attachments), если ещё не применена.
    ChunkStore(s.pg_dsn).init_schema()
    store = TaskStore(s.pg_dsn)
    atts = [{"name": "spec.md", "mime_type": "text/markdown", "size": 4,
             "content_text": "spec"}]
    store.upsert_task(TaskRow(
        key="ATT-1", aliases=[], title="t", description="d", status=None, url=None,
        content_hash="h1", text="t\n\nd", embedding=[0.0] * s.embedding_dim, project="ATT",
        attachments=atts))
    row = store.get_task("ATT-1")
    assert row is not None
    assert row.attachments == atts
    store.delete_tasks(["ATT-1"])
    store.close()
