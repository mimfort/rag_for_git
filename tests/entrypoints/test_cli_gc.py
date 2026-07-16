"""Unit-тест CLI-команды `reviewer gc` (без Postgres: патчим стор и GC)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import psycopg
from click.testing import CliRunner

from reviewer.entrypoints.cli import cli


def _fake_settings(*, persist: bool = True) -> MagicMock:
    """Фейковые настройки: pg_dsn/pool не используются (ChunkStore/SessionStore
    патчатся отдельно), важен только review_session_persist (C3) и ttl."""
    s = MagicMock()
    s.review_session_persist = persist
    s.review_session_ttl_hours = 24
    s.pg_dsn = "postgresql://localhost:5433/testdb"
    s.pg_pool_min_size = 1
    s.pg_pool_max_size = 4
    return s


def test_gc_prints_report():
    """Команда печатает, что удалила, что оставила живым и что не распознала."""
    report = {"purged": ["a/x pr:94"], "kept": 2, "skipped": 1, "sessions_deleted": 3}
    with patch("reviewer.entrypoints.cli.Settings", return_value=_fake_settings()), \
         patch("reviewer.entrypoints.cli.ChunkStore", MagicMock()), \
         patch("reviewer.entrypoints.cli.SessionStore", MagicMock()), \
         patch("reviewer.entrypoints.cli.purge_orphaned_overlays",
               return_value=report) as gc_fn:
        result = CliRunner().invoke(cli, ["gc"])

    assert result.exit_code == 0, result.output
    assert "a/x pr:94" in result.output
    assert "оставлено живых 2" in result.output
    assert "нераспознано 1" in result.output
    assert "3" in result.output
    assert gc_fn.called


def test_gc_requires_session_persist():
    """C3: персист сессий выключен → GC отказано внятной ошибкой, а не сносит ВСЕ overlay.

    SessionStore._connect сам создаёт пустую схему review_sessions, поэтому
    live_keys() тихо вернула бы пустое множество — все overlay деплоя выглядели
    бы сиротами, включая overlay идущих прямо сейчас ревью.
    """
    with patch("reviewer.entrypoints.cli.Settings", return_value=_fake_settings(persist=False)), \
         patch("reviewer.entrypoints.cli.ChunkStore", MagicMock()) as chunk_cls, \
         patch("reviewer.entrypoints.cli.SessionStore", MagicMock()) as sess_cls, \
         patch("reviewer.entrypoints.cli.purge_orphaned_overlays") as gc_fn:
        result = CliRunner().invoke(cli, ["gc"])

    assert result.exit_code != 0
    assert "REVIEW_SESSION_PERSIST" in result.output
    assert not gc_fn.called
    chunk_cls.assert_not_called()
    sess_cls.assert_not_called()


def test_gc_reports_postgres_unavailable():
    """psycopg.OperationalError → внятная ClickException, а не сырой трейсбек."""
    with patch("reviewer.entrypoints.cli.Settings", return_value=_fake_settings()), \
         patch("reviewer.entrypoints.cli.ChunkStore", MagicMock()), \
         patch("reviewer.entrypoints.cli.SessionStore", MagicMock()), \
         patch("reviewer.entrypoints.cli.purge_orphaned_overlays",
               side_effect=psycopg.OperationalError("connection refused")):
        result = CliRunner().invoke(cli, ["gc"])

    assert result.exit_code != 0
    assert "Postgres недоступен" in result.output


def test_gc_reports_missing_chunks_table():
    """R3: свежий деплой без единого `reviewer index` (UndefinedTable) → внятная
    ошибка вместо сырого трейсбека. Потери данных тут нет — чисто UX."""
    with patch("reviewer.entrypoints.cli.Settings", return_value=_fake_settings()), \
         patch("reviewer.entrypoints.cli.ChunkStore", MagicMock()), \
         patch("reviewer.entrypoints.cli.SessionStore", MagicMock()), \
         patch("reviewer.entrypoints.cli.purge_orphaned_overlays",
               side_effect=psycopg.errors.UndefinedTable('relation "chunks" does not exist')):
        result = CliRunner().invoke(cli, ["gc"])

    assert result.exit_code != 0
    assert "reviewer index" in result.output
