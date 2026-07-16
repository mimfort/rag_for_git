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


def test_live_keys_raises_when_db_unavailable(monkeypatch) -> None:
    """live_keys НЕ fail-soft: «не знаю живых» ≠ «живых нет».

    Тихий пустой ответ при сбое БД заставил бы GC счесть сиротой overlay
    идущего прямо сейчас ревью и удалить его.
    """
    store = SessionStore("postgresql://invalid/none")

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "_connect", boom)
    with pytest.raises(RuntimeError):
        store.live_keys(24)


@pytest.mark.integration
def test_live_keys_and_delete_expired() -> None:
    """live_keys видит только непросроченные; delete_expired сносит просроченные.

    ВАЖНО (тот же класс дефекта, что store.clear() без repo — TRUNCATE chunks
    снёс рабочий индекс в этой ветке): delete_expired(ttl_hours) разворачивается
    в ``DELETE ... WHERE created_at <= now() - ttl_hours``. При ttl_hours=0 это
    удаляет ВСЕ строки review_sessions на общей БД — включая сессии чужих идущих
    прямо сейчас ревью (GC счёл бы их overlay сиротами). Поэтому здесь back-date'им
    ТОЛЬКО свою строку и вызываем delete_expired(24) — прод-семантика, сносит
    лишь реально просроченное.
    """
    pg_dsn = Settings().pg_dsn
    store = SessionStore(pg_dsn)
    store.init_schema()
    repo, pr = "owner/gc-test", 998
    store.delete(repo, pr)  # чистый старт
    try:
        store.save(repo, pr, {"repo": repo})

        assert (repo, pr) in store.live_keys(24)
        # TTL=0 → чтением live_keys(0) строка мгновенно просрочена (не трогаем
        # саму таблицу — delete_expired(0) здесь НЕ вызываем, см. docstring).
        assert (repo, pr) not in store.live_keys(0)

        with store._connect() as conn:
            conn.execute(
                "UPDATE review_sessions SET created_at = now() - interval '48 hours' "
                "WHERE repo=%s AND pr_number=%s",
                (repo, pr),
            )
            conn.commit()
        assert store.delete_expired(24) >= 1
        assert store.load(repo, pr, 24) is None   # строка физически удалена
    finally:
        store.delete(repo, pr)  # уборка своей строки (no-op, если уже удалена)
        store.close()
