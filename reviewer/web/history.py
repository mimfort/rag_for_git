"""Хранилище истории прогонов ревью (Postgres).

Изолировано за собственным интерфейсом — подключается к той же БД (PG_DSN),
что и ChunkStore, но использует отдельные таблицы review_runs / review_findings.
"""
from __future__ import annotations

import json
import logging
import threading
from decimal import Decimal
from pathlib import Path

from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

_SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")

# Тариф бакетов относительно input-токена (спайк PRI-246) — тот же, что в
# plugin/hooks/_transcript.py (клиентский хук) и eval/solve_task_metrics/cost.py
# (офлайн-метрика). Единица результата — условные единицы, НЕ доллары.
_STAGE_COST_WEIGHTS = {"fresh_in": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.1}


def aggregate_stages(steps: list) -> list:
    """Разрез трейса по стадиям: число шагов и размеры payload.

    Токенов и стоимости здесь нет: сервер не видит LLM-вызовов. Расход по
    стадиям приходит отдельным каналом — из usage.by_stage прогона
    (см. merge_stage_costs).
    """
    order: list = []
    acc: dict = {}
    for step in steps:
        stage = step.get("stage") or "unknown"
        row = acc.get(stage)
        if row is None:
            row = {"stage": stage, "steps": 0, "args_bytes": 0, "result_bytes": 0}
            acc[stage] = row
            order.append(stage)
        row["steps"] += 1
        calls = step.get("tool_calls") or []
        for call in calls:
            if not isinstance(call, dict):
                continue
            row["args_bytes"] += int(call.get("args_bytes") or 0)
            row["result_bytes"] += int(call.get("result_bytes") or 0)
    return [acc[s] for s in order]


def _weigh_bucket(bucket: dict) -> float:
    """Взвешенная стоимость бакета токенов в условных единицах."""
    return round(
        sum(_STAGE_COST_WEIGHTS[k] * float(bucket.get(k) or 0) for k in _STAGE_COST_WEIGHTS), 6
    )


def merge_stage_costs(rows: list, usage_by_stage: dict | None) -> list:
    """Слить разрез трейса (aggregate_stages) с расходом по стадиям (usage.by_stage).

    Множества стадий пересекаются, но не совпадают: серверный трейс знает
    analyze/verify/synthesize/client, клиентский хук — более мелкие стадии
    (orchestrator/risk/blast_radius/...). Результат — объединение: строка,
    видимая только одной стороне, не теряется. У стадии без данных о расходе
    ``cost`` — ``None``, а не 0: пустая ячейка честнее нуля.
    """
    by_stage = usage_by_stage or {}
    acc = {r["stage"]: dict(r) for r in rows}
    order = [r["stage"] for r in rows]
    for stage in by_stage:
        if stage not in acc:
            acc[stage] = {"stage": stage, "steps": 0, "args_bytes": 0, "result_bytes": 0}
            order.append(stage)
    result = []
    for stage in order:
        row = dict(acc[stage])
        bucket = by_stage.get(stage)
        row["cost"] = _weigh_bucket(bucket) if bucket else None
        result.append(row)
    return result


class ReviewHistory:
    """Персистирует историю прогонов ревью и предоставляет API для её чтения.

    Использует пул соединений :class:`psycopg_pool.ConnectionPool`;
    пул создаётся лениво при первом запросе.
    """

    def __init__(self, pg_dsn: str, *, min_size: int = 1, max_size: int = 4) -> None:
        self.pg_dsn = pg_dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: ConnectionPool | None = None
        self._init_lock = threading.Lock()
        self._schema_ready = False

    def _ensure_pool(self) -> ConnectionPool:
        """Создать и открыть пул при первом обращении (thread-safe)."""
        if self._pool is None:
            with self._init_lock:
                if self._pool is None:
                    self._pool = ConnectionPool(
                        self.pg_dsn,
                        min_size=self._min_size,
                        max_size=self._max_size,
                        open=False,
                    )
                    self._pool.open()
        return self._pool

    def _connect(self):
        """Вернуть соединение из пула, гарантировав наличие/актуальность схемы.

        Схема применяется лениво при первом обращении (по образцу
        ``mcp/session_store``): так запись истории на MCP-пути ``publish_review``
        сама домигрирует БД (идемпотентно), не завися от запуска ``reviewer serve``.

        Гонка флага без блокировки допустима намеренно (как в ``session_store``):
        ``init_schema`` идемпотентна (``IF NOT EXISTS`` + бэкфилл по
        ``WHERE outcome IS NULL``). Единственный доброкачественный риск —
        одновременный первый ``CREATE TABLE`` на совсем пустой БД у двух процессов;
        проигравший ловится fail-soft ``record_run`` и самоизлечивается на
        следующей записи (``_schema_ready`` остаётся ``False``). Реальный сценарий
        апгрейда (таблица есть, нет колонок) сериализуется через ``ALTER TABLE``.
        """
        if not self._schema_ready:
            self.init_schema()
        return self._ensure_pool().connection()

    def close(self) -> None:
        """Закрыть пул соединений, если он был создан."""
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    # ------------------------------------------------------------------
    # Инициализация схемы
    # ------------------------------------------------------------------

    def init_schema(self) -> None:
        """Создать/домигрировать таблицы истории (идемпотентно).

        Берёт соединение из пула напрямую (не через ``_connect``), чтобы избежать
        рекурсии с ленивым гардом ``_schema_ready``.
        """
        with self._ensure_pool().connection() as conn:
            conn.execute(_SCHEMA)
            conn.commit()
        self._schema_ready = True

    # ------------------------------------------------------------------
    # Запись прогона
    # ------------------------------------------------------------------

    def record_run(
        self, run: dict, findings: list[dict], steps: list[dict] | None = None
    ) -> int | None:
        """Вставить прогон, его находки и (опционально) шаги трейса одной транзакцией.

        Args:
            run:      словарь с полями, соответствующими колонкам review_runs.
            findings: список словарей (поля review_findings, без run_id).
            steps:    список шагов трейса (формат строк review_steps, без run_id).
                      Если None или пустой — шаги не вставляются.

        Returns:
            id вставленной записи или None при сбое (fail-soft).
        """
        try:
            run_sql = """
            INSERT INTO review_runs (
                repo, pr_number, base_sha, head_sha,
                model, model_verify, dry_run,
                started_at, finished_at, duration_ms, status,
                files_reviewed, files_skipped, files_failed,
                findings_analyzed, findings_kept, verify_rejected,
                comments_inline, comments_summary,
                usage, config_sources, total_cost, error_text
            ) VALUES (
                %(repo)s, %(pr_number)s, %(base_sha)s, %(head_sha)s,
                %(model)s, %(model_verify)s, %(dry_run)s,
                %(started_at)s, %(finished_at)s, %(duration_ms)s, %(status)s,
                %(files_reviewed)s, %(files_skipped)s, %(files_failed)s,
                %(findings_analyzed)s, %(findings_kept)s, %(verify_rejected)s,
                %(comments_inline)s, %(comments_summary)s,
                %(usage)s, %(config_sources)s, %(total_cost)s, %(error_text)s
            ) RETURNING id
            """
            finding_sql = """
            INSERT INTO review_findings (
                run_id, file, line, category, severity, confidence,
                is_real, published, inline, fingerprint, message,
                outcome, reject_reason
            ) VALUES (
                %(run_id)s, %(file)s, %(line)s, %(category)s, %(severity)s, %(confidence)s,
                %(is_real)s, %(published)s, %(inline)s, %(fingerprint)s, %(message)s,
                %(outcome)s, %(reject_reason)s
            )
            """
            step_sql = """
            INSERT INTO review_steps (
                run_id, stage, unit, seq, kind, name, text, tool_calls, tokens, cost
            ) VALUES (
                %(run_id)s, %(stage)s, %(unit)s, %(seq)s, %(kind)s, %(name)s,
                %(text)s, %(tool_calls)s, %(tokens)s, %(cost)s
            )
            """
            # JSONB-поля передаём строками; старые вызовы без provenance получают {}.
            run_row = dict(run)
            if run_row.get("config_sources") is None:
                run_row["config_sources"] = {}
            for field in ("usage", "config_sources"):
                if not isinstance(run_row.get(field), str):
                    run_row[field] = json.dumps(run_row.get(field), ensure_ascii=False)

            with self._connect() as conn:
                row = conn.execute(run_sql, run_row).fetchone()
                run_id: int = row[0]
                if findings:
                    # Дефолты outcome/reject_reason для строк без этих ключей
                    # (старые вызовы / тестовые фикстуры) — back-compat.
                    rows = [
                        {"outcome": None, "reject_reason": None, **f, "run_id": run_id}
                        for f in findings
                    ]
                    with conn.cursor() as cur:
                        cur.executemany(finding_sql, rows)
                if steps:
                    step_rows = []
                    for s in steps:
                        tool_calls_val = s.get("tool_calls")
                        step_rows.append({
                            "run_id": run_id,
                            "stage": s.get("stage", ""),
                            "unit": s.get("unit", ""),
                            "seq": s.get("seq", 0),
                            "kind": s.get("kind", ""),
                            "name": s.get("name"),
                            "text": s.get("text"),
                            "tool_calls": (
                                json.dumps(tool_calls_val, ensure_ascii=False)
                                if tool_calls_val is not None else None
                            ),
                            "tokens": s.get("tokens", 0),
                            "cost": s.get("cost", 0.0),
                        })
                    with conn.cursor() as cur:
                        cur.executemany(step_sql, step_rows)
                conn.commit()
            return run_id
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось сохранить историю прогона: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------

    def list_runs(
        self,
        repo: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Вернуть список прогонов без находок (последние по created_at).

        Args:
            repo:   фильтр по репозиторию (exact match), None = без фильтра.
            status: фильтр по статусу (ok/error/draft_skip), None = без фильтра.
            limit:  максимальное количество записей.
            offset: смещение (пагинация).
        """
        conditions: list[str] = []
        params: dict = {"limit": limit, "offset": offset}
        if repo is not None:
            conditions.append("repo = %(repo)s")
            params["repo"] = repo
        if status is not None:
            conditions.append("status = %(status)s")
            params["status"] = status

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"""
        SELECT id, created_at, repo, pr_number, base_sha, head_sha,
               model, model_verify, dry_run,
               started_at, finished_at, duration_ms, status,
               files_reviewed, files_skipped, files_failed,
               findings_analyzed, findings_kept, verify_rejected,
               comments_inline, comments_summary,
               usage, COALESCE(config_sources, '{{}}'::jsonb) AS config_sources,
               total_cost, error_text
        FROM review_runs
        {where}
        ORDER BY created_at DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [d.name for d in cur.description]
                rows = cur.fetchall()
        return [_row_to_dict(cols, r) for r in rows]

    def get_run(self, run_id: int) -> dict | None:
        """Вернуть прогон вместе с находками или None, если не найден."""
        run_sql = """
        SELECT id, created_at, repo, pr_number, base_sha, head_sha,
               model, model_verify, dry_run,
               started_at, finished_at, duration_ms, status,
               files_reviewed, files_skipped, files_failed,
               findings_analyzed, findings_kept, verify_rejected,
               comments_inline, comments_summary,
               usage, COALESCE(config_sources, '{}'::jsonb) AS config_sources,
               total_cost, error_text
        FROM review_runs WHERE id = %(id)s
        """
        finding_sql = """
        SELECT id, file, line, category, severity, confidence,
               is_real, published, inline, fingerprint, message,
               outcome, reject_reason
        FROM review_findings WHERE run_id = %(run_id)s ORDER BY id
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(run_sql, {"id": run_id})
                run_cols = [d.name for d in cur.description]
                run_row = cur.fetchone()
                if run_row is None:
                    return None
                result = _row_to_dict(run_cols, run_row)

                cur.execute(finding_sql, {"run_id": run_id})
                f_cols = [d.name for d in cur.description]
                result["findings"] = [_row_to_dict(f_cols, r) for r in cur.fetchall()]
        return result

    def get_trace(self, run_id: int) -> list[dict]:
        """Вернуть шаги трейса прогона, упорядоченные по seq.

        Args:
            run_id: идентификатор прогона.

        Returns:
            Список словарей с полями шага или пустой список, если шагов нет
            (прогон без трейса, неизвестный run_id, или ошибка БД — fail-soft).
        """
        try:
            sql = """
            SELECT id, run_id, stage, unit, seq, kind, name, text, tool_calls, tokens, cost,
                   created_at
            FROM review_steps
            WHERE run_id = %(run_id)s
            ORDER BY seq
            """
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, {"run_id": run_id})
                    cols = [d.name for d in cur.description]
                    rows = cur.fetchall()
            return [_row_to_dict(cols, r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось получить трейс прогона %s: %s", run_id, exc)
            return []

    def _usage_by_stage(self, run_id: int) -> dict:
        """Бакет usage.by_stage прогона (fail-soft: ошибка/отсутствие → {})."""
        try:
            sql = "SELECT usage FROM review_runs WHERE id = %(run_id)s"
            with self._connect() as conn:
                row = conn.execute(sql, {"run_id": run_id}).fetchone()
            if not row or not row[0]:
                return {}
            usage = row[0]
            if isinstance(usage, str):
                usage = json.loads(usage)
            return (usage or {}).get("by_stage") or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось получить usage.by_stage прогона %s: %s", run_id, exc)
            return {}

    def stage_breakdown(self, run_id: int) -> list[dict]:
        """Разрез трейса по стадиям, объединённый с расходом из usage.by_stage.

        Steps/payload — из серверного трейса (review_steps), cost — из
        usage.by_stage прогона (снят клиентским хуком в sidecar, см.
        reviewer/services/cost_sidecar.py). Каналы разные и множества стадий
        не совпадают, поэтому строится объединение (merge_stage_costs).
        """
        rows = aggregate_stages(self.get_trace(run_id))
        return merge_stage_costs(rows, self._usage_by_stage(run_id))

    def days_since_last_run(self, repo: str) -> int | None:
        """Вернуть количество дней с последнего прогона для репозитория.

        Args:
            repo: полное имя репозитория (owner/name).

        Returns:
            Целое число дней или ``None``, если прогонов не было или запрос
            к БД завершился ошибкой (fail-soft).
        """
        try:
            sql = """
            SELECT DATE_PART('day', now() - MAX(created_at))
            FROM review_runs
            WHERE repo = %(repo)s
            """
            with self._connect() as conn:
                row = conn.execute(sql, {"repo": repo}).fetchone()
            if row is None or row[0] is None:
                return None
            return int(row[0])
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Не удалось получить дату последнего прогона для %s: %s", repo, exc
            )
            return None

    def stats(self, days: int = 30) -> dict:
        """Агрегированная статистика за последние ``days`` дней.

        Возвращает::

            {
                "total_runs": int,
                "total_cost": float,
                "avg_cost_per_run": float,
                "total_findings": int,
                "verify_reject_rate": float,      # 0..1
                "cost_over_time": [{"date": "YYYY-MM-DD", "cost": float}, ...],
                "runs_over_time": [{"date": "YYYY-MM-DD", "count": int}, ...],
                "findings_by_category": [{"category": str, "count": int}, ...],
                "findings_by_severity": [{"severity": str, "count": int}, ...],
            }
        """
        with self._connect() as conn:
            # Суммарные показатели
            agg = conn.execute("""
                SELECT
                    COUNT(*)                                                    AS total_runs,
                    COALESCE(SUM(total_cost), 0)                               AS total_cost,
                    COALESCE(AVG(total_cost), 0)                               AS avg_cost_per_run,
                    COALESCE(SUM(findings_kept), 0)                            AS total_findings,
                    COALESCE(SUM(findings_analyzed), 0)                        AS total_analyzed,
                    COALESCE(SUM(verify_rejected), 0)                          AS total_rejected
                FROM review_runs
                WHERE created_at >= now() - %(days)s * INTERVAL '1 day'
            """, {"days": days}).fetchone()

            total_runs, total_cost, avg_cost, total_findings, total_analyzed, total_rejected = agg
            total_runs = int(total_runs or 0)
            total_cost = float(total_cost or 0)
            avg_cost = float(avg_cost or 0)
            total_findings = int(total_findings or 0)
            total_analyzed = int(total_analyzed or 0)
            total_rejected = int(total_rejected or 0)
            verify_reject_rate = (
                round(total_rejected / total_analyzed, 4) if total_analyzed > 0 else 0.0
            )

            # Стоимость по дням
            cost_rows = conn.execute("""
                SELECT
                    DATE(created_at AT TIME ZONE 'UTC') AS day,
                    COALESCE(SUM(total_cost), 0)         AS cost
                FROM review_runs
                WHERE created_at >= now() - %(days)s * INTERVAL '1 day'
                GROUP BY day ORDER BY day
            """, {"days": days}).fetchall()

            # Прогоны по дням
            runs_rows = conn.execute("""
                SELECT
                    DATE(created_at AT TIME ZONE 'UTC') AS day,
                    COUNT(*)                             AS cnt
                FROM review_runs
                WHERE created_at >= now() - %(days)s * INTERVAL '1 day'
                GROUP BY day ORDER BY day
            """, {"days": days}).fetchall()

            # Находки по категориям
            cat_rows = conn.execute("""
                SELECT f.category, COUNT(*) AS cnt
                FROM review_findings f
                JOIN review_runs r ON r.id = f.run_id
                WHERE r.created_at >= now() - %(days)s * INTERVAL '1 day'
                GROUP BY f.category ORDER BY cnt DESC
            """, {"days": days}).fetchall()

            # Находки по severity
            sev_rows = conn.execute("""
                SELECT f.severity, COUNT(*) AS cnt
                FROM review_findings f
                JOIN review_runs r ON r.id = f.run_id
                WHERE r.created_at >= now() - %(days)s * INTERVAL '1 day'
                GROUP BY f.severity ORDER BY cnt DESC
            """, {"days": days}).fetchall()

        return {
            "total_runs": total_runs,
            "total_cost": round(total_cost, 6),
            "avg_cost_per_run": round(avg_cost, 6),
            "total_findings": total_findings,
            "verify_reject_rate": verify_reject_rate,
            "cost_over_time": [
                {"date": str(r[0]), "cost": float(r[1] or 0)} for r in cost_rows
            ],
            "runs_over_time": [
                {"date": str(r[0]), "count": int(r[1] or 0)} for r in runs_rows
            ],
            "findings_by_category": [
                {"category": r[0], "count": int(r[1] or 0)} for r in cat_rows
            ],
            "findings_by_severity": [
                {"severity": r[0], "count": int(r[1] or 0)} for r in sev_rows
            ],
        }

    # ------------------------------------------------------------------
    # Качество брифа solve-task (PRI-249)
    # ------------------------------------------------------------------

    def record_brief_quality(
        self,
        run_id: int,
        repo: str,
        pr_number: int,
        head_sha: str | None,
        measurement,
    ) -> int | None:
        """Записать измерение качества брифа (fail-soft, как record_run).

        Строки со status != 'measured' пишутся намеренно: «точки измерения не
        было и вот почему» — диагностический сигнал, а молчание неотличимо от
        сломанной метрики.
        """
        sql = """
        INSERT INTO brief_quality (
            run_id, repo, pr_number, task_key, head_sha, status, brief_path,
            expected, expected_core, predicted, hit_core,
            core_recall, raw_recall, precision,
            misses, predicted_paths, expected_core_paths, hit_core_paths
        ) VALUES (
            %(run_id)s, %(repo)s, %(pr_number)s, %(task_key)s, %(head_sha)s,
            %(status)s, %(brief_path)s,
            %(expected)s, %(expected_core)s, %(predicted)s, %(hit_core)s,
            %(core_recall)s, %(raw_recall)s, %(precision)s,
            %(misses)s, %(predicted_paths)s, %(expected_core_paths)s, %(hit_core_paths)s
        ) RETURNING id
        """
        try:
            params = {
                "run_id": run_id,
                "repo": repo,
                "pr_number": pr_number,
                "task_key": measurement.task_key,
                "head_sha": head_sha,
                "status": measurement.status,
                "brief_path": measurement.brief_path,
                "expected": measurement.expected,
                "expected_core": measurement.expected_core,
                "predicted": measurement.predicted,
                "hit_core": measurement.hit_core,
                "core_recall": measurement.core_recall,
                "raw_recall": measurement.raw_recall,
                "precision": measurement.precision,
                "misses": json.dumps(measurement.misses, ensure_ascii=False),
                "predicted_paths": json.dumps(list(measurement.predicted_paths)),
                "expected_core_paths": json.dumps(list(measurement.expected_core_paths)),
                "hit_core_paths": json.dumps(list(measurement.hit_core_paths)),
            }
            with self._connect() as conn:
                row = conn.execute(sql, params).fetchone()
                conn.commit()
            return int(row[0]) if row else None
        except Exception as exc:  # noqa: BLE001 — метрика не смеет ронять ревью
            log.warning("Не удалось записать качество брифа для прогона %s: %s", run_id, exc)
            return None

    def brief_quality_trend(self, days: int = 90, repo: str | None = None) -> dict:
        """Динамика качества брифа за окно, агрегированная ПО ЗАДАЧЕ.

        Несколько PR одной задачи объединяются в одну точку (union множеств) —
        ровно так считает офлайн-харнесс, чей baseline служит точкой «до» для
        критерия 4 PRI-251. Считать по PR значило бы мерить другой линейкой.

        Исключение — ключ `misses`: таксономия промахов считается ПО СТРОКАМ
        (по PR) за окно, а не по задаче. Промах — свойство конкретного диффа,
        и union путей его бы затёр; поэтому `misses` намеренно не сопоставим
        по знаменателю с `aggregate.n_measured` (задачи).
        """
        from reviewer.metrics.brief_quality.recall import (
            BULK_CORE_THRESHOLD,
            TaskQuality,
            aggregate,
        )

        empty = {
            "trend": [],
            "aggregate": {"n_measured": 0, "no_measurement": 0},
            "bulk": {"n_measured": 0, "core_recall_median": None},
            "misses": [],
            "bulk_threshold": BULK_CORE_THRESHOLD,
            "no_measurement_by_status": {},
        }
        sql = """
        SELECT created_at, task_key, pr_number, status,
               predicted_paths, expected_core_paths, hit_core_paths, misses
        FROM brief_quality
        WHERE created_at >= now() - %(days)s * INTERVAL '1 day'
          AND (%(repo)s::text IS NULL OR repo = %(repo)s::text)
        ORDER BY created_at
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, {"days": days, "repo": repo}).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось получить динамику качества брифа: %s", exc)
            return empty

        by_task: dict = {}
        no_measurement: dict = {}
        misses_total: dict = {}
        for created_at, task_key, pr_number, status, predicted, core, hits, misses in rows:
            for category, count in (misses or {}).items():
                misses_total[category] = misses_total.get(category, 0) + int(count)
            if status != "measured" or not task_key:
                key = status or "unknown"
                no_measurement[key] = no_measurement.get(key, 0) + 1
                continue
            entry = by_task.setdefault(
                task_key,
                {"created_at": created_at, "prs": [], "predicted": set(), "core": set(), "hits": set()},
            )
            entry["created_at"] = max(entry["created_at"], created_at)
            entry["prs"].append(int(pr_number))
            entry["predicted"].update(predicted or [])
            entry["core"].update(core or [])
            entry["hits"].update(hits or [])

        trend: list = []
        quality_rows: list = []
        for task_key, entry in by_task.items():
            core_recall = len(entry["hits"]) / len(entry["core"]) if entry["core"] else None
            trend.append({
                "date": entry["created_at"].isoformat(),
                "task_key": task_key,
                "prs": sorted(entry["prs"]),
                "expected_core": len(entry["core"]),
                "predicted": len(entry["predicted"]),
                "hit_core": len(entry["hits"]),
                "core_recall": core_recall,
                "precision": (
                    len(entry["hits"]) / len(entry["predicted"]) if entry["predicted"] else None
                ),
            })
            quality_rows.append(TaskQuality(
                task_key=task_key,
                expected=len(entry["core"]),
                expected_core=len(entry["core"]),
                predicted=len(entry["predicted"]),
                hit_core=len(entry["hits"]),
                core_recall=core_recall,
            ))
        trend.sort(key=lambda point: point["date"])
        agg = aggregate(quality_rows)
        return {
            "trend": trend,
            "aggregate": {
                "n_measured": agg.n_measured,
                "no_measurement": sum(no_measurement.values()),
                "core_recall_median": agg.core_recall_median,
                "core_recall_mean": agg.core_recall_mean,
                "denominator_median": agg.denominator_median,
            },
            "bulk": {
                "n_measured": agg.bulk_n_measured,
                "core_recall_median": agg.bulk_core_recall_median,
            },
            "misses": [
                {"category": category, "count": count}
                for category, count in sorted(
                    misses_total.items(), key=lambda item: item[1], reverse=True
                )
            ],
            "bulk_threshold": BULK_CORE_THRESHOLD,
            "no_measurement_by_status": no_measurement,
        }


# ------------------------------------------------------------------
# Вспомогательная функция
# ------------------------------------------------------------------

def _row_to_dict(cols: list[str], row: tuple) -> dict:
    """Преобразовать кортеж строки БД в словарь с JSON-сериализуемыми типами.

    Postgres ``numeric`` приходит как :class:`decimal.Decimal` — конвертируем в
    float, иначе ``JSONResponse`` (json.dumps) упадёт на реальных данных
    (``total_cost``). Дата/время — в ISO-строку.
    """
    result: dict = {}
    for key, val in zip(cols, row):
        if key == "config_sources" and val is None:
            result[key] = {}
        elif isinstance(val, Decimal):
            result[key] = float(val)
        elif hasattr(val, "isoformat"):
            result[key] = val.isoformat()
        else:
            result[key] = val
    return result
