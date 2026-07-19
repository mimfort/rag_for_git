"""Unit-тесты GC осиротевших overlay (фейковые store/session_store, без БД)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from reviewer.services.gc import purge_orphaned_overlays


class _FakeStore:
    def __init__(self, overlays: list[tuple[str, str]]) -> None:
        self._overlays = overlays
        self.deleted: list[tuple[str, str]] = []

    def list_overlay_refs(self) -> list[tuple[str, str]]:
        return list(self._overlays)

    def delete_ref(self, repo: str, ref: str) -> None:
        self.deleted.append((repo, ref))


class _FakeSessionStore:
    def __init__(self, live: set[tuple[str, int]], *, boom: bool = False) -> None:
        self._live = live
        self._boom = boom
        self.expired_deleted = 0

    def live_keys(self, ttl_hours: int) -> set[tuple[str, int]]:
        if self._boom:
            raise RuntimeError("db down")
        return set(self._live)

    def delete_expired(self, ttl_hours: int) -> int:
        self.expired_deleted += 1
        return 3


def test_purges_overlay_without_live_session():
    """Ядро бага: overlay без живой сессии — сирота, его удаляем."""
    store = _FakeStore([("a/x", "pr:94")])
    sessions = _FakeSessionStore(live=set())

    report = purge_orphaned_overlays(store, sessions, ttl_hours=24)

    assert store.deleted == [("a/x", "pr:94")]
    assert report["purged"] == ["a/x pr:94"]
    assert report["kept"] == 0
    assert report["sessions_deleted"] == 3


def test_keeps_overlay_with_live_session_row():
    """Ревью с непросроченной строкой сессии живо — его overlay неприкосновенен."""
    store = _FakeStore([("a/x", "pr:5")])
    sessions = _FakeSessionStore(live={("a/x", 5)})

    report = purge_orphaned_overlays(store, sessions, ttl_hours=24)

    assert store.deleted == []
    assert report["kept"] == 1


def test_keeps_overlay_of_active_in_memory_session():
    """Сессия только в памяти процесса (persist упал fail-soft) — тоже живая."""
    store = _FakeStore([("a/x", "pr:5")])
    sessions = _FakeSessionStore(live=set())   # в БД строки нет

    report = purge_orphaned_overlays(store, sessions, ttl_hours=24,
                                     active_keys={("a/x", 5)})

    assert store.deleted == []
    assert report["kept"] == 1


def test_never_deletes_anything_when_live_set_unavailable():
    """Инвариант безопасности: «не знаю живых» ≠ «живых нет» — не удаляем ничего."""
    store = _FakeStore([("a/x", "pr:94")])
    sessions = _FakeSessionStore(live=set(), boom=True)

    with pytest.raises(RuntimeError):
        purge_orphaned_overlays(store, sessions, ttl_hours=24)

    assert store.deleted == []
    assert sessions.expired_deleted == 0


def test_no_session_store_is_noop():
    """Персист сессий выключен → живость определить нечем → не удаляем ничего."""
    store = _FakeStore([("a/x", "pr:94")])

    report = purge_orphaned_overlays(store, None, ttl_hours=24)

    assert store.deleted == []
    assert report == {"purged": [], "kept": 0, "skipped": 0, "sessions_deleted": 0}


def test_ignores_unparsable_ref():
    """Мусорный ref вида 'pr:abc' не удаляем — только то, что уверенно распознали.

    M2: такой ref не «жив» (kept) и не «удалён» (purged) — отдельный счётчик
    skipped, чтобы человек в CLI не читал «оставлено живых N», где N включает мусор.
    """
    store = _FakeStore([("a/x", "pr:abc")])
    sessions = _FakeSessionStore(live=set())

    report = purge_orphaned_overlays(store, sessions, ttl_hours=24)

    assert store.deleted == []
    assert report["kept"] == 0
    assert report["skipped"] == 1


def test_ignores_base_ref_even_if_listed():
    """base:* не удаляется на уровне самой функции (не только по контракту
    list_overlay_refs) — защита от регрессии, если тот метод начнёт отдавать base:*."""
    store = _FakeStore([("a/x", "base:main"), ("a/x", "pr:94")])
    sessions = _FakeSessionStore(live=set())

    report = purge_orphaned_overlays(store, sessions, ttl_hours=24)

    assert ("a/x", "base:main") not in store.deleted
    assert store.deleted == [("a/x", "pr:94")]
    assert report["skipped"] == 1


def test_reads_overlay_snapshot_before_live_keys():
    """C1 (TOCTOU): list_overlay_refs() ДОЛЖЕН читаться раньше live_keys().

    Обратный порядок — гонка: overlay, чья сессия зарезервирована и чьи чанки
    появились МЕЖДУ чтением live и списком overlay, попадёт в список, но
    отсутствует в уже устаревшем снимке live → GC ошибочно сочтёт его сиротой
    и удалит overlay идущего прямо сейчас ревью. Тест краснеет при откате
    порядка вызовов в purge_orphaned_overlays.
    """
    calls: list[str] = []

    class _OrderedStore(_FakeStore):
        def list_overlay_refs(self) -> list[tuple[str, str]]:
            calls.append("list_overlay_refs")
            return super().list_overlay_refs()

    class _OrderedSessionStore(_FakeSessionStore):
        def live_keys(self, ttl_hours: int) -> set[tuple[str, int]]:
            calls.append("live_keys")
            return super().live_keys(ttl_hours)

    store = _OrderedStore([("a/x", "pr:5")])
    sessions = _OrderedSessionStore(live={("a/x", 5)})

    purge_orphaned_overlays(store, sessions, ttl_hours=24)

    assert calls == ["list_overlay_refs", "live_keys"]


def test_delete_expired_still_runs_when_list_overlay_refs_fails():
    """M2: сбой list_overlay_refs не должен блокировать уборку просроченных сессий —
    это независимая гигиена (delete_expired сама fail-soft). Overlay в этом прогоне
    не трогаем и пробрасываем исходную ошибку («не знаю» ≠ «нет»)."""
    store = _FakeStore([])
    store.list_overlay_refs = MagicMock(side_effect=RuntimeError("relation missing"))
    sessions = _FakeSessionStore(live=set())

    with pytest.raises(RuntimeError):
        purge_orphaned_overlays(store, sessions, ttl_hours=24)

    assert sessions.expired_deleted == 1
    assert store.deleted == []
