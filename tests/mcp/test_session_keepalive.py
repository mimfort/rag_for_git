"""PRI-212: keepalive сессии — обращения продлевают живость.

_session() бампает last_seen_at in-memory на каждом обращении и продлевает
строку в Postgres (SessionStore.touch) не чаще _TOUCH_INTERVAL_S. Сбой БД
не роняет обращение (touch fail-soft на стороне SessionStore); при
выключенном персисте продление остаётся чисто in-memory.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from reviewer.config.settings import Settings
from reviewer.mcp.service import _TOUCH_INTERVAL_S, MCPReviewService, _Session


class _TouchLog:
    """Фейковый стор: журналирует только touch (другие методы не нужны тесту)."""

    def __init__(self) -> None:
        self.touched: list[tuple[str, int]] = []

    def touch(self, repo: str, pr: int) -> None:
        self.touched.append((repo, pr))


def _svc_with_session() -> tuple[MCPReviewService, _Session, _TouchLog]:
    s = Settings()
    s.voyage_api_key = "test"
    s.github_token = "test"
    s.review_session_persist = True
    svc = MCPReviewService(s, MagicMock())
    store = _TouchLog()
    svc._session_store = store
    sess = _Session(prepared=MagicMock(), ctx=MagicMock())
    svc._sessions[("a/x", 7)] = sess
    return svc, sess, store


def test_access_bumps_last_seen_and_touches_db():
    """Обращение к сессии бампает in-memory last_seen_at и делает DB-touch."""
    svc, sess, store = _svc_with_session()
    before = sess.last_seen_at

    svc._session("a/x", 7)

    assert sess.last_seen_at >= before
    assert store.touched == [("a/x", 7)]


def test_db_touch_throttled_within_interval():
    """Два обращения подряд → один DB-touch (троттлинг _TOUCH_INTERVAL_S)."""
    svc, sess, store = _svc_with_session()

    svc._session("a/x", 7)
    svc._session("a/x", 7)

    assert store.touched == [("a/x", 7)]


def test_db_touch_repeats_after_interval():
    """Интервал истёк → следующий DB-touch проходит."""
    svc, sess, store = _svc_with_session()

    svc._session("a/x", 7)
    sess.db_touched_at = datetime.now(timezone.utc) - timedelta(
        seconds=_TOUCH_INTERVAL_S + 1
    )
    svc._session("a/x", 7)

    assert store.touched == [("a/x", 7), ("a/x", 7)]


def test_in_memory_bump_without_store():
    """Персист выключен → продление чисто in-memory, без ошибок."""
    svc, sess, _ = _svc_with_session()
    svc._session_store = None
    svc.settings.review_session_persist = False
    before = sess.last_seen_at

    svc._session("a/x", 7)  # не бросает

    assert sess.last_seen_at >= before
