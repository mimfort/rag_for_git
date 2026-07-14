"""GC эфемерных артефактов ревью: осиротевшие overlay pr:N и просроченные сессии.

На «счастливом» пути overlay удаляет MCPReviewService._cleanup (после
publish_review). Но если ревью прервано между prepare_review и publish_review
(пользователь отменил, оркестрирующая LLM-сессия упала или упёрлась в таймаут),
publish_review не вызывается НИКОГДА — и overlay остаётся в Postgres навсегда:
self-healing в начале ReviewService.prepare чистит только тот же самый PR, а
смерженный PR больше никто не ревьюит. Этот модуль — страховка на такой случай.

Критерий сироты: overlay pr:N репо R — сирота, если (R, N) нет ни среди
непросроченных строк review_sessions, ни среди активных сессий процесса.
Отдельная таблица-реестр не нужна: строка сессии создаётся ровно там же, где
строится overlay, поэтому review_sessions.created_at и есть возраст overlay.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_OVERLAY_PREFIX = "pr:"


def _pr_number(ref: str) -> int | None:
    """Номер PR из overlay-ref 'pr:<N>'; None — если ref не распознан."""
    if not ref.startswith(_OVERLAY_PREFIX):
        return None
    try:
        return int(ref[len(_OVERLAY_PREFIX):])
    except ValueError:
        return None


def purge_orphaned_overlays(
    store,
    session_store,
    ttl_hours: int,
    active_keys: set[tuple[str, int]] = frozenset(),
) -> dict:
    """Удалить overlay без живой сессии и просроченные строки review_sessions.

    active_keys — ключи (repo, pr) сессий, живущих в памяти вызывающего процесса.
    Они считаются живыми независимо от БД: SessionStore.save fail-soft, и при
    сбое персиста сессия существует только в памяти — без этой страховки GC снёс
    бы overlay идущего ревью.

    Инвариант безопасности: если множество живых сессий получить НЕ удалось,
    исключение пробрасывается и не удаляется НИЧЕГО — «не знаю живых» ≠ «живых
    нет». Вызывающий решает, как реагировать (prepare_review — fail-soft, CLI —
    показывает ошибку).

    Возвращает {"purged": [...], "kept": int, "sessions_deleted": int}.
    """
    if session_store is None:
        # Персист сессий выключен → живость overlay определить нечем.
        return {"purged": [], "kept": 0, "sessions_deleted": 0}

    live = session_store.live_keys(ttl_hours) | set(active_keys)

    purged: list[str] = []
    kept = 0
    for repo, ref in store.list_overlay_refs():
        pr = _pr_number(ref)
        if pr is None or (repo, pr) in live:
            kept += 1
            continue
        store.delete_ref(repo, ref)
        purged.append(f"{repo} {ref}")

    sessions_deleted = session_store.delete_expired(ttl_hours)
    if purged:
        log.info("GC overlay: удалено %s, оставлено живых %s (%s)",
                 len(purged), kept, ", ".join(purged))
    return {"purged": purged, "kept": kept, "sessions_deleted": sessions_deleted}
