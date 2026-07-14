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

from collections.abc import Collection
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
    active_keys: Collection[tuple[str, int]] = (),
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

    Возвращает {"purged": [...], "kept": int, "skipped": int, "sessions_deleted": int}.
    kept — overlay с подтверждённо живой сессией; skipped — ref, который GC не
    смог распознать как overlay pr:N (например, "pr:abc") — такие не трогаем,
    но и «живыми» их считать неверно, поэтому счётчик отдельный от kept.
    """
    if session_store is None:
        # Персист сессий выключен → живость overlay определить нечем.
        return {"purged": [], "kept": 0, "skipped": 0, "sessions_deleted": 0}

    # ПОРЯДОК ЧТЕНИЙ КРИТИЧЕН — НЕ МЕНЯТЬ: сначала снимок overlay (T1), потом
    # live-множество сессий (T2 > T1). reviewer-mcp — stdio-сервер (отдельный
    # процесс на клиента, общий Postgres), поэтому «другой процесс строит
    # overlay прямо сейчас» — штатный сценарий, не экзотика.
    #
    # Причина именно такого порядка: резервация ключа сессии (prepare_review)
    # ВСЕГДА коммитится раньше, чем появляется первая строка overlay для этого
    # (repo, pr) — см. reviewer/mcp/service.py. Значит для любого overlay,
    # попавшего в снимок T1, его сессия к моменту T1 уже была закоммичена, а
    # значит и подавно видна в live_keys(), прочитанном позже, на T2. Overlay,
    # созданный уже ПОСЛЕ T1, в снимок не попадёт вовсе — GC его просто не
    # увидит на этом прогоне (безопасно: разберётся со следующим).
    #
    # Обратный порядок (live, потом list) — TOCTOU: overlay, чья сессия
    # зарезервирована и чьи чанки появились МЕЖДУ чтением live и списком
    # overlay, окажется в списке, но отсутствует в уже устаревшем снимке live
    # → GC ошибочно сочтёт его сиротой и удалит overlay идущего прямо сейчас
    # ревью (то самое, из-за которого делается эта проверка).
    try:
        overlays = store.list_overlay_refs()
    except Exception:
        # Сбой листинга overlay — независимая от live-множества проблема;
        # просроченные строки сессий всё равно стоит убрать (session-only
        # гигиена, delete_expired сам fail-soft). Overlay в этом прогоне не
        # трогаем вовсе и пробрасываем исходную ошибку — «не знаю» ≠ «нет».
        session_store.delete_expired(ttl_hours)
        raise

    live = session_store.live_keys(ttl_hours) | set(active_keys)

    purged: list[str] = []
    kept = 0
    skipped = 0
    for repo, ref in overlays:
        pr = _pr_number(ref)
        if pr is None:
            skipped += 1
            continue
        if (repo, pr) in live:
            kept += 1
            continue
        store.delete_ref(repo, ref)
        purged.append(f"{repo} {ref}")

    sessions_deleted = session_store.delete_expired(ttl_hours)
    if purged:
        log.info("GC overlay: удалено %s, оставлено живых %s, нераспознано %s (%s)",
                 len(purged), kept, skipped, ", ".join(purged))
    return {"purged": purged, "kept": kept, "skipped": skipped,
            "sessions_deleted": sessions_deleted}
