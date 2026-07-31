"""Оркестратор server-side синка досок: обходит все провайдеры,
enumerate → watermark → normalize → index_batch → purge по объединению active_keys.
Board-агностичен (видит только TaskBoardProvider).

Курсор инкрементальности — в index_meta (repo="", ref="tasks:<type>:<board>"):
legacy integer без фильтра или versioned JSON с filter state. Полный enumerate
всегда нужен для purge active-keys и свежести статусов; normalize/index
пропускаются для неизменившихся задач. Purge выполняется по объединению
eligible active_keys всех провайдеров (задачи глобальны — иначе одна доска
вычистит задачи другой).

force_renormalize=True игнорирует watermark: каждая задача проходит полный
normalize → index_batch. Разовая операция после смены правил нормализации
(PRI-213); дедуп по content_hash сам отсечёт задачи с неизменившимся текстом.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from reviewer.config.task_board import TaskSyncFilter
from reviewer.tasks.boards.base import TaskBoardProvider
from reviewer.tasks.boards.errors import sanitize_provider_text
from reviewer.tasks.sync_cursor import (
    TaskSyncCursor,
    parse_task_sync_cursor,
    serialize_task_sync_cursor,
)
from reviewer.tasks.sync_filter import classify_task, filter_fingerprint, filter_is_active

log = logging.getLogger(__name__)

_CURSOR_REPO = ""  # задачи глобальны (таблица tasks без repo-скоупа)


@dataclass(frozen=True)
class SyncProvider:
    """Provider и его immutable secret set для безопасного sync boundary."""

    provider: TaskBoardProvider
    secrets: frozenset[str] = frozenset()


class SyncService:
    def __init__(self, providers, task_service, meta_store, *, now_ms=None) -> None:
        self._providers = [
            item if isinstance(item, SyncProvider) else SyncProvider(item)
            for item in providers
        ]
        self._tasks = task_service
        self._meta = meta_store
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)

    def _cursor_ref(self, board_type: str, board: str | None) -> str:
        return f"tasks:{board_type}:{board or '*'}"

    def _sync_provider(
        self,
        sync_provider,
        board,
        limit,
        force_renormalize=False,
        *,
        sync_filter: TaskSyncFilter | None = None,
        filter_source: str | None = None,
    ) -> tuple[list[str], dict, bool]:
        """Синк одной доски: enumerate → watermark → normalize → index → курсор.
        purge НЕ делает (он общий по всем доскам — см. run)."""
        provider = sync_provider.provider
        secrets = sync_provider.secrets
        run_now_ms = self._now_ms()
        warnings: list[str] = []
        one = {
            "enumerated": 0,
            "eligible": 0,
            "filtered_by_age": 0,
            "filtered_archived": 0,
            "age_unknown": 0,
            "archive_unknown": 0,
            "filter_applied": filter_is_active(sync_filter),
            "filter_fingerprint": filter_fingerprint(sync_filter),
            "filter_source": filter_source,
            "changed": 0,
            "embedded": 0,
            "refreshed": 0,
            "unchanged": 0,
            "meta_refreshed": 0,
            "failed": 0,
            "warnings": warnings,
            "cursor_advanced": False,
        }
        ref = self._cursor_ref(provider.board_type, board)
        try:
            prev = self._meta.get_index_meta(_CURSOR_REPO, ref)
        except Exception as e:
            safe_error = sanitize_provider_text(f"{type(e).__name__}: {e}", secrets)
            log.warning("sync: сбой чтения cursor: %s", safe_error)
            warnings.append(
                f"sync cursor read failed: {safe_error}; full backfill enabled"
            )
            cursor = TaskSyncCursor(0, None, False, None)
        else:
            parsed = parse_task_sync_cursor(prev)
            cursor = parsed.cursor
            if parsed.warning is not None:
                safe_warning = sanitize_provider_text(parsed.warning, secrets)
                log.warning("sync: %s", safe_warning)
                warnings.append(safe_warning)

        active_keys: list[str] = []
        changed: list[dict] = []
        meta_refresh: list[dict] = []
        max_ts = cursor.watermark
        unchanged = 0
        age_warning_emitted = False
        archive_warning_emitted = False
        delivered_rows = 0
        retry_required = False
        try:
            listing = provider.iter_raw(
                board,
                limit,
                sync_filter=sync_filter if limit is None else None,
                now_ms=run_now_ms if limit is None else None,
            )
            for raw in listing.rows:
                delivered_rows += 1
                if raw.timestamp is not None:
                    max_ts = max(max_ts, raw.timestamp)

                decision = classify_task(sync_filter, raw, run_now_ms)
                age_unknown = bool(
                    sync_filter is not None
                    and sync_filter.max_age_days is not None
                    and raw.timestamp is None
                )
                archive_unknown = bool(
                    decision != "filtered_by_age"
                    and sync_filter is not None
                    and not sync_filter.include_archived
                    and raw.archived is None
                )
                if age_unknown:
                    one["age_unknown"] += 1
                    if not age_warning_emitted:
                        warnings.append(
                            "sync filter: timestamp отсутствует; возраст задачи неизвестен"
                        )
                        age_warning_emitted = True
                if archive_unknown:
                    one["archive_unknown"] += 1
                    if not archive_warning_emitted:
                        warnings.append(
                            "sync filter: archived отсутствует; архивность задачи неизвестна"
                        )
                        archive_warning_emitted = True
                if decision != "eligible":
                    one[decision] += 1
                    continue

                one["eligible"] += 1
                active_keys.append(raw.key)
                old_decision = (
                    classify_task(cursor.old_filter, raw, run_now_ms)
                    if cursor.old_filter_known
                    else None
                )
                full_normalize = bool(
                    force_renormalize
                    or not cursor.old_filter_known
                    or old_decision != "eligible"
                    or (
                        sync_filter is not None
                        and sync_filter.max_age_days is not None
                        and raw.timestamp is None
                    )
                    or (
                        raw.timestamp is not None
                        and raw.timestamp > cursor.watermark
                    )
                )
                if not full_normalize:
                    unchanged += 1
                    try:
                        meta_refresh.append(provider.normalize_meta(raw))
                    except Exception as e:
                        safe_key = sanitize_provider_text(raw.key, secrets)
                        safe_error = sanitize_provider_text(
                            f"{type(e).__name__}: {e}", secrets
                        )
                        log.warning(
                            "sync: сбой normalize_meta %s: %s", safe_key, safe_error
                        )
                        warnings.append(f"normalize_meta {safe_key}: {safe_error}")
                    continue
                try:
                    changed.append(provider.normalize(raw))
                except Exception as e:
                    retry_required = True
                    safe_key = sanitize_provider_text(raw.key, secrets)
                    safe_error = sanitize_provider_text(
                        f"{type(e).__name__}: {e}", secrets
                    )
                    log.warning("sync: сбой нормализации %s: %s", safe_key, safe_error)
                    warnings.append(f"normalize {safe_key}: {safe_error}")

            provider_filtered_by_age = listing.stats.filtered_by_age
            provider_filtered_archived = listing.stats.filtered_archived
            provider_warnings = list(listing.stats.warnings)
        except Exception as e:
            safe_error = sanitize_provider_text(f"{type(e).__name__}: {e}", secrets)
            log.warning("sync: сбой listing %s: %s", provider.board_type, safe_error)
            warnings.append(f"listing {provider.board_type}: {safe_error}")
            one["enumerated"] = delivered_rows
            return [], one, False

        one["filtered_by_age"] += provider_filtered_by_age
        one["filtered_archived"] += provider_filtered_archived
        one["enumerated"] = (
            delivered_rows + provider_filtered_by_age + provider_filtered_archived
        )
        warnings.extend(
            sanitize_provider_text(warning, secrets) for warning in provider_warnings
        )

        results = self._tasks.index_batch(changed) if changed else []
        embedded = sum(1 for r in results if r.get("embedded"))
        refreshed = sum(
            1 for r in results if not r.get("embedded") and not r.get("warnings")
        )
        failed = sum(1 for r in results if r.get("warnings"))
        retry_required = retry_required or any(
            r.get("retry_required") is True for r in results
        )
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

        numeric_advance = max_ts > cursor.watermark
        current_filter_active = filter_is_active(sync_filter)
        old_filter_active = (
            filter_is_active(cursor.old_filter) if cursor.old_filter_known else False
        )
        filter_state_changed = bool(
            current_filter_active
            and (
                not cursor.old_filter_known
                or cursor.old_filter != sync_filter
            )
        )
        filter_state_removed = bool(
            cursor.old_filter_known
            and old_filter_active
            and not current_filter_active
        )
        repair_cursor = not cursor.old_filter_known
        cursor_advanced = False
        if limit is not None:
            warnings.append("курсор не продвинут: задан limit (частичный обход)")
        elif not retry_required and (
            numeric_advance
            or filter_state_changed
            or filter_state_removed
            or repair_cursor
        ):
            desired_cursor = serialize_task_sync_cursor(max_ts, sync_filter)
            try:
                self._meta.set_index_meta(_CURSOR_REPO, ref, desired_cursor)
                cursor_advanced = numeric_advance
            except Exception as e:
                safe_error = sanitize_provider_text(
                    f"{type(e).__name__}: {e}", secrets
                )
                log.warning("sync: сбой записи cursor: %s", safe_error)
                warnings.append(f"sync cursor write failed: {safe_error}")

        one.update({
            "changed": len(changed),
            "embedded": embedded, "refreshed": refreshed, "unchanged": unchanged,
            "meta_refreshed": meta_refreshed,
            "failed": failed, "warnings": warnings, "cursor_advanced": cursor_advanced,
        })
        return active_keys, one, True

    def run(self, board=None, limit=None, purge_orphaned=False,
            keep_with_prs=True, board_type=None, force_renormalize=False, *,
            sync_filter: TaskSyncFilter | None = None,
            filter_source: str | None = None) -> dict:
        agg = {"enumerated": 0, "changed": 0, "embedded": 0, "refreshed": 0,
               "unchanged": 0, "failed": 0, "meta_refreshed": 0,
               "eligible": 0, "filtered_by_age": 0, "filtered_archived": 0,
               "age_unknown": 0, "archive_unknown": 0,
               "filter_applied": filter_is_active(sync_filter),
               "filter_fingerprint": filter_fingerprint(sync_filter),
               "filter_source": filter_source,
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
        all_complete = True
        for sync_provider in providers:
            provider = sync_provider.provider
            active, one, complete = self._sync_provider(
                sync_provider,
                board,
                limit,
                force_renormalize,
                sync_filter=sync_filter,
                filter_source=filter_source,
            )
            all_complete = all_complete and complete
            all_active.extend(active)
            for k in ("enumerated", "changed", "embedded", "refreshed",
                      "unchanged", "failed", "meta_refreshed", "eligible",
                      "filtered_by_age", "filtered_archived", "age_unknown",
                      "archive_unknown"):
                agg[k] += one[k]
            agg["warnings"].extend(one["warnings"])
            agg["cursor_advanced"] = agg["cursor_advanced"] or one["cursor_advanced"]
            by_board.append({
                "board_type": provider.board_type,
                "board": board or "*",
                **{k: one[k] for k in ("enumerated", "changed", "embedded",
                                        "refreshed", "unchanged", "failed",
                                        "meta_refreshed", "eligible",
                                        "filtered_by_age", "filtered_archived",
                                        "age_unknown", "archive_unknown",
                                        "filter_applied", "filter_fingerprint",
                                        "filter_source")},
            })

        partial = limit is not None
        purge_summary = None
        if purge_orphaned and partial:
            agg["warnings"].append("purge пропущен: задан limit (active_keys неполный)")
        elif purge_orphaned and not all_complete:
            agg["warnings"].append(
                "purge пропущен: обход provider завершён не полностью"
            )
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
