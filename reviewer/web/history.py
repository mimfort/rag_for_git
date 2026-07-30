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
            run_row.setdefault("config_sources", {})
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
               usage, config_sources, total_cost, error_text
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
               usage, config_sources, total_cost, error_text
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
        if isinstance(val, Decimal):
            result[key] = float(val)
        elif hasattr(val, "isoformat"):
            result[key] = val.isoformat()
        else:
            result[key] = val
    return result
