"""Оркестратор server-side синка досок: обходит все провайдеры,
enumerate → watermark → normalize → index_batch → purge по объединению active_keys.
Board-агностичен (видит только TaskBoardProvider).

Курсор инкрементальности — в index_meta (repo="", ref="tasks:<type>:<board>"),
значение = str(max timestamp ms). Полный enumerate всегда (нужно для purge
active-keys и свежести статусов); normalize/index пропускаются для
timestamp <= cursor. Purge выполняется по объединению active_keys всех провайдеров
(задачи глобальны — иначе одна доска вычистит задачи другой).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_CURSOR_REPO = ""  # задачи глобальны (таблица tasks без repo-скоупа)


class SyncService:
    def __init__(self, providers, task_service, meta_store) -> None:
        self._providers = list(providers)
        self._tasks = task_service
        self._meta = meta_store

    def _cursor_ref(self, board_type: str, board: str | None) -> str:
        return f"tasks:{board_type}:{board or '*'}"

    def _sync_provider(self, provider, board, limit) -> tuple[list[str], dict]:
        """Синк одной доски: enumerate → watermark → normalize → index → курсор.
        purge НЕ делает (он общий по всем доскам — см. run)."""
        warnings: list[str] = []
        ref = self._cursor_ref(provider.board_type, board)
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
        for raw in provider.iter_raw(board, limit):
            active_keys.append(raw.key)
            max_ts = max(max_ts, raw.timestamp)
            if raw.timestamp <= cursor:
                unchanged += 1
                continue
            try:
                changed.append(provider.normalize(raw))
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

        cursor_advanced = False
        if limit:
            warnings.append("курсор не продвинут: задан limit (частичный обход)")
        elif max_ts > cursor:
            try:
                self._meta.set_index_meta(_CURSOR_REPO, ref, str(max_ts))
                cursor_advanced = True
            except Exception:
                log.warning("sync: сбой записи курсора", exc_info=True)

        return active_keys, {
            "enumerated": len(active_keys), "changed": len(changed),
            "embedded": embedded, "refreshed": refreshed, "unchanged": unchanged,
            "failed": failed, "warnings": warnings, "cursor_advanced": cursor_advanced,
        }

    def run(self, board=None, limit=None, purge_orphaned=False,
            keep_with_prs=True, board_type=None) -> dict:
        agg = {"enumerated": 0, "changed": 0, "embedded": 0, "refreshed": 0,
               "unchanged": 0, "failed": 0, "warnings": [], "cursor_advanced": False}
        # PRI-170: scoped-синк из репо — только один тип доски (board_type), а не все.
        providers = self._providers
        if board_type is not None:
            providers = [p for p in self._providers if p.board_type == board_type]
            if not providers:
                agg["warnings"].append(
                    f"тип доски '{board_type}' не настроен на сервере")
        all_active: list[str] = []
        for provider in providers:
            active, one = self._sync_provider(provider, board, limit)
            all_active.extend(active)
            for k in ("enumerated", "changed", "embedded", "refreshed",
                      "unchanged", "failed"):
                agg[k] += one[k]
            agg["warnings"].extend(one["warnings"])
            agg["cursor_advanced"] = agg["cursor_advanced"] or one["cursor_advanced"]

        partial = bool(limit)
        purge_summary = None
        if purge_orphaned and partial:
            agg["warnings"].append("purge пропущен: задан limit (active_keys неполный)")
        elif purge_orphaned:
            # scoped-синк (board_type задан) → purge только своего проекта (board);
            # deploy-wide → project=None, purge по объединению всех досок (как раньше).
            project = board if board_type is not None else None
            pr = self._tasks.purge_orphaned_tasks(
                all_active, keep_with_prs=keep_with_prs, project=project)
            purge_summary = {"deleted": pr["deleted_store"] + pr["deleted_graph"],
                             "protected": pr["protected_prs"]}
            agg["warnings"].extend(pr.get("warnings") or [])
        agg["purge"] = purge_summary
        return agg
