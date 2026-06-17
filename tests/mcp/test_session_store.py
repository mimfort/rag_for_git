"""Тесты SessionStore.

Unit-тест fail-soft не требует инфраструктуры (мокаем _connect).
Integration-тесты (save/load/delete/TTL) помечены @pytest.mark.integration
и требуют поднятого Postgres (docker compose up -d).
"""
from __future__ import annotations

import pytest

from reviewer.config.settings import Settings
from reviewer.mcp.session_store import SessionStore


def test_session_store_failsoft_without_db(monkeypatch) -> None:
    """Сбой соединения не пробрасывается: save/delete молчат, load → None."""
    store = SessionStore("postgresql://invalid/none")

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "_connect", boom)
    store.save("o/r", 1, {"a": 1})            # не бросает
    assert store.load("o/r", 1, 24) is None   # fail-soft → None
    store.delete("o/r", 1)                     # не бросает


@pytest.mark.integration
def test_session_store_save_load_delete_ttl() -> None:
    pg_dsn = Settings().pg_dsn
    store = SessionStore(pg_dsn)
    store.init_schema()
    store.init_schema()  # идемпотентность
    repo, pr = "owner/sess-test", 999
    store.delete(repo, pr)  # чистый старт

    payload = {"repo": repo, "branch": "main", "items": [1, 2, 3]}
    store.save(repo, pr, payload)
    assert store.load(repo, pr, 24) == payload

    # upsert: повторный save перезаписывает payload
    store.save(repo, pr, {"repo": repo, "branch": "main", "items": []})
    assert store.load(repo, pr, 24)["items"] == []

    # TTL=0 → created_at > now() ложно → строка считается просроченной
    assert store.load(repo, pr, 0) is None

    store.delete(repo, pr)
    assert store.load(repo, pr, 24) is None
    store.close()
