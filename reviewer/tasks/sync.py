"""Оркестратор server-side синка досок: обходит все провайдеры,
enumerate → watermark → normalize → index_batch → purge по объединению active_keys.
Board-агностичен (видит только TaskBoardProvider).

Курсор инкрементальности — в index_meta (repo="", ref="tasks:<type>:<board>"),
значение = str(max timestamp ms). Полный enumerate всегда (нужно для purge
active-keys и свежести статусов); normalize/index пропускаются для
timestamp <= cursor. Purge выполняется по объединению active_keys всех провайдеров
(задачи глобальны — иначе одна доска вычистит задачи другой).

force_renormalize=True игнорирует watermark: каждая задача проходит полный
normalize → index_batch. Разовая операция после смены правил нормализации
(PRI-213); дедуп по content_hash сам отсечёт задачи с неизменившимся текстом.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from reviewer.tasks.boards.base import TaskBoardProvider
from reviewer.tasks.boards.errors import sanitize_provider_text

log = logging.getLogger(__name__)

_CURSOR_REPO = ""  # задачи глобальны (таблица tasks без repo-скоупа)


@dataclass(frozen=True)
class SyncProvider:
    """Provider и его immutable secret set для безопасного sync boundary."""

    provider: TaskBoardProvider
    secrets: frozenset[str] = frozenset()
    owned: bool = False


class SyncService:
    def __init__(self, providers, task_service, meta_store) -> None:
        self._providers = [
            item if isinstance(item, SyncProvider) else SyncProvider(item)
            for item in providers
        ]
        self._tasks = task_service
        self._meta = meta_store
        self._closed_provider_ids: set[int] = set()

    def close(self) -> None:
        """Идемпотентно закрыть только owned providers, не владея shared stores."""
        first_error: Exception | None = None
        for item in self._providers:
            provider_id = id(item.provider)
            if not item.owned or provider_id in self._closed_provider_ids:
                continue
            self._closed_provider_ids.add(provider_id)
            try:
                item.provider.close()
            except Exception as error:  # noqa: BLE001 - закрываем остальных providers
                first_error = first_error or error
        if first_error is not None:
            raise first_error

    def _cursor_ref(self, board_type: str, board: str | None) -> str:
        return f"tasks:{board_type}:{board or '*'}"

    def _sync_provider(
        self, sync_provider, board, limit, force_renormalize=False
    ) -> tuple[list[str], dict]:
        """Синк одной доски: enumerate → watermark → normalize → index → курсор.
        purge НЕ делает (он общий по всем доскам — см. run)."""
        provider = sync_provider.provider
        secrets = sync_provider.secrets
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
        meta_refresh: list[dict] = []
        max_ts = cursor
        unchanged = 0
        for raw in provider.iter_raw(board, limit):
            active_keys.append(raw.key)
            max_ts = max(max_ts, raw.timestamp)
            if raw.timestamp <= cursor and not force_renormalize:
                unchanged += 1
                try:
                    meta_refresh.append(provider.normalize_meta(raw))
                except Exception as e:
                    safe_key = sanitize_provider_text(raw.key, secrets)
                    safe_error = sanitize_provider_text(
                        f"{type(e).__name__}: {e}", secrets
                    )
                    log.warning("sync: сбой normalize_meta %s: %s", safe_key, safe_error)
                    warnings.append(f"normalize_meta {safe_key}: {safe_error}")
                continue
            try:
                changed.append(provider.normalize(raw))
            except Exception as e:
                safe_key = sanitize_provider_text(raw.key, secrets)
                safe_error = sanitize_provider_text(
                    f"{type(e).__name__}: {e}", secrets
                )
                log.warning("sync: сбой нормализации %s: %s", safe_key, safe_error)
                warnings.append(f"normalize {safe_key}: {safe_error}")

        results = self._tasks.index_batch(changed) if changed else []
        embedded = sum(1 for r in results if r.get("embedded"))
        refreshed = sum(1 for r in results
                        if not r.get("embedded") and not r.get("warnings"))
        failed = sum(1 for r in results if r.get("warnings"))
        for r in results:
            warnings.extend(
                sanitize_provider_text(warning, secrets)
                for warning in (r.get("warnings") or [])
            )

        meta_refreshed = 0
        if meta_refresh:
            mr = self._tasks.refresh_meta_batch(meta_refresh)
            meta_refreshed = mr.get("meta_refreshed", 0)
            warnings.extend(
                sanitize_provider_text(warning, secrets)
                for warning in (mr.get("warnings") or [])
            )

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
            "meta_refreshed": meta_refreshed,
            "failed": failed, "warnings": warnings, "cursor_advanced": cursor_advanced,
        }

    def run(self, board=None, limit=None, purge_orphaned=False,
            keep_with_prs=True, board_type=None, force_renormalize=False) -> dict:
        agg = {"enumerated": 0, "changed": 0, "embedded": 0, "refreshed": 0,
               "unchanged": 0, "failed": 0, "meta_refreshed": 0,
               "warnings": [], "cursor_advanced": False}
        # PRI-170: scoped-синк из репо — только один тип доски (board_type), а не все.
        providers = self._providers
        if board_type is not None:
            providers = [
                item
                for item in self._providers
                if item.provider.board_type == board_type
            ]
            if not providers:
                agg["warnings"].append(
                    f"тип доски '{board_type}' не настроен на сервере")
        all_active: list[str] = []
        by_board: list[dict] = []
        for sync_provider in providers:
            provider = sync_provider.provider
            active, one = self._sync_provider(
                sync_provider, board, limit, force_renormalize
            )
            all_active.extend(active)
            for k in ("enumerated", "changed", "embedded", "refreshed",
                      "unchanged", "failed", "meta_refreshed"):
                agg[k] += one[k]
            agg["warnings"].extend(one["warnings"])
            agg["cursor_advanced"] = agg["cursor_advanced"] or one["cursor_advanced"]
            by_board.append({
                "board_type": provider.board_type,
                "board": board or "*",
                **{k: one[k] for k in ("enumerated", "changed", "embedded",
                                        "refreshed", "unchanged", "failed",
                                        "meta_refreshed")},
            })

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
        agg["by_board"] = by_board
        return agg
