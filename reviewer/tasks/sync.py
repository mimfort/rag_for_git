"""Оркестратор server-side синка доски: enumerate → watermark → normalize →
index_batch → purge. Board-агностичен (видит только TaskBoardProvider).

Курсор инкрементальности — в index_meta (repo="", ref="tasks:<board>"), значение
= str(max timestamp ms). Полный enumerate всегда (нужно для purge active-keys и
свежести статусов); normalize/index пропускаются для timestamp <= cursor.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_CURSOR_REPO = ""  # задачи глобальны (таблица tasks без repo-скоупа)


class SyncService:
    def __init__(self, provider, task_service, meta_store) -> None:
        self._provider = provider
        self._tasks = task_service
        self._meta = meta_store

    def _cursor_ref(self, board: str | None) -> str:
        return f"tasks:{board or '*'}"

    def run(self, board=None, limit=None, purge_orphaned=False,
            keep_with_prs=True) -> dict:
        warnings: list[str] = []
        ref = self._cursor_ref(board)
        try:
            prev = self._meta.get_index_meta(_CURSOR_REPO, ref)
            cursor = int(prev) if prev else 0
        except Exception:
            log.warning("sync: сбой чтения курсора", exc_info=True)
            cursor = 0

        active_keys: list[str] = []
        changed: list[dict] = []
        max_ts = cursor
        unchanged = 0

        for raw in self._provider.iter_raw(board, limit):
            active_keys.append(raw.key)
            max_ts = max(max_ts, raw.timestamp)
            if raw.timestamp <= cursor:
                unchanged += 1
                continue
            try:
                changed.append(self._provider.normalize(raw))
            except Exception as e:
                log.warning("sync: сбой нормализации %s", raw.key, exc_info=True)
                warnings.append(f"normalize {raw.key}: {type(e).__name__}: {e}")

        results = self._tasks.index_batch(changed) if changed else []
        embedded = sum(1 for r in results if r.get("embedded"))
        refreshed = sum(1 for r in results
                        if not r.get("embedded") and not r.get("warnings"))
        failed = sum(1 for r in results if r.get("warnings"))
        for r in results:
            warnings.extend(r.get("warnings") or [])

        partial = bool(limit)
        purge_summary = None
        if purge_orphaned and partial:
            warnings.append("purge пропущен: задан limit (active_keys неполный)")
        elif purge_orphaned:
            pr = self._tasks.purge_orphaned_tasks(active_keys,
                                                  keep_with_prs=keep_with_prs)
            purge_summary = {"deleted": pr["deleted_store"] + pr["deleted_graph"],
                             "protected": pr["protected_prs"]}
            warnings.extend(pr.get("warnings") or [])

        cursor_advanced = False
        if partial:
            warnings.append("курсор не продвинут: задан limit (частичный обход)")
        elif max_ts > cursor:
            try:
                self._meta.set_index_meta(_CURSOR_REPO, ref, str(max_ts))
                cursor_advanced = True
            except Exception:
                log.warning("sync: сбой записи курсора", exc_info=True)

        return {
            "enumerated": len(active_keys),
            "changed": len(changed),
            "embedded": embedded,
            "refreshed": refreshed,
            "unchanged": unchanged,
            "failed": failed,
            "purge": purge_summary,
            "warnings": warnings,
            "cursor_advanced": cursor_advanced,
        }
