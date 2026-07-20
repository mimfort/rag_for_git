"""Ядро бага: prepare_review без publish_review оставлял overlay навсегда.

Сценарий: ревью PR 7 подготовили, но публикация не состоялась (пользователь
отменил / оркестрирующая сессия упала) — _cleanup не вызвался. Следующий
prepare_review (ДРУГОГО PR) обязан подчистить осиротевший overlay pr:7.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
from reviewer.services.review_service import BranchNotTrackedError

# prepare_review персистит сессию через to_payload(prepared) (session_serde.py) —
# он вызывает dataclasses.asdict() на prepared.prq/policy/units, а здесь
# _review_service мокнут целиком ("нас интересует только GC, не подготовка
# ревью"), поэтому prepare() отдаёт голый MagicMock, а не настоящий
# PreparedReview. asdict() требует реальный dataclass и падает на Mock'е —
# это про сериализацию сессии, не про GC, так что глушим to_payload здесь.
_PATCH_TO_PAYLOAD = patch("reviewer.mcp.service.to_payload", return_value={})


def _settings() -> Settings:
    s = Settings()
    s.voyage_api_key = "test"
    s.github_token = "test"
    # review_session_persist=True — мы ХОТИМ, чтобы _ensure_session_store() отдавал
    # инжектированный _FakeSessionStore по флагу, а не только потому, что
    # _session_store уже не None (это раньше маскировало проверку флага —
    # _ensure_session_store возвращает уже заданный self._session_store ДО чтения
    # settings.review_session_persist). Реальный Postgres всё равно не трогаем:
    # _session_store инжектирован ниже, конструктор SessionStore не вызывается.
    s.review_session_persist = True
    return s


def _components() -> MagicMock:
    c = MagicMock()
    c.store = MagicMock()
    c.store.deleted_refs = []
    c.store.delete_ref.side_effect = lambda repo, ref: c.store.deleted_refs.append(ref)
    c.store.list_overlay_refs.return_value = [("a/x", "pr:7")]
    return c


class _FakeSessionStore:
    """Строк сессий нет: сессия PR 7 умерла вместе с оркестрирующим клиентом.

    save() нужен потому, что prepare_review персистит новую сессию
    (reviewer/mcp/service.py:133-135) — без него тест упал бы на AttributeError.

    calls — журнал вызовов save()/delete() в порядке обращения ("save"/"delete",
    repo, pr). Нужен тестам на снятие резервации ключа сессии (C2): без него
    пришлось бы городить отдельную MagicMock-обвязку в каждом тесте.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def save(self, repo: str, pr: int, payload: dict) -> None:
        self.calls.append(("save", repo, pr))

    def delete(self, repo: str, pr: int) -> None:
        self.calls.append(("delete", repo, pr))

    def touch(self, repo: str, pr: int) -> None:
        self.calls.append(("touch", repo, pr))

    def live_keys(self, ttl_hours: int) -> set[tuple[str, int]]:
        return set()

    def delete_expired(self, ttl_hours: int) -> int:
        return 0


def _service(components: MagicMock) -> MCPReviewService:
    svc = MCPReviewService(_settings(), components)
    svc._session_store = _FakeSessionStore()
    # prepare() мокаем: нас интересует только GC, не подготовка ревью.
    svc._review_service = MagicMock()
    svc._prepared_payload = MagicMock(return_value={"status": "ok"})
    return svc


@_PATCH_TO_PAYLOAD
def test_prepare_purges_orphaned_overlay_of_abandoned_review(_to_payload):
    """Overlay брошенного ревью (pr:7) удаляется при следующем prepare_review."""
    c = _components()
    svc = _service(c)

    svc.prepare_review("a/x", 8)

    assert "pr:7" in c.store.deleted_refs


@_PATCH_TO_PAYLOAD
def test_prepare_keeps_overlay_of_active_in_memory_session(_to_payload):
    """Overlay ревью, живущего в памяти процесса, не трогаем (параллельное ревью)."""
    c = _components()
    svc = _service(c)
    live_session = MagicMock()
    live_session.started_at = datetime.now(timezone.utc)   # ревью PR 7 идёт прямо сейчас
    live_session.last_seen_at = datetime.now(timezone.utc)  # и активность свежая
    svc._sessions[("a/x", 7)] = live_session

    svc.prepare_review("a/x", 8)

    assert "pr:7" not in c.store.deleted_refs


@_PATCH_TO_PAYLOAD
def test_prepare_purges_overlay_of_stale_in_memory_session_past_ttl(_to_payload):
    """C4: in-memory-сессия без TTL-отсечки делает overlay бессмертным.

    _sessions чистится ТОЛЬКО в _cleanup (путь publish) — самый частый сценарий
    брошенного ревью (пользователь отменил скилл, процесс reviewer-mcp жив)
    никогда там не оказывается. Запись держится в _sessions, но её started_at
    старше TTL — GC обязан признать overlay сиротой, а не хранить вечно.
    """
    c = _components()
    svc = _service(c)
    stale_session = MagicMock()
    stale_session.started_at = (
        datetime.now(timezone.utc)
        - timedelta(hours=svc.settings.review_session_ttl_hours + 1)
    )
    stale_session.last_seen_at = stale_session.started_at  # активности не было
    svc._sessions[("a/x", 7)] = stale_session   # брошено больше TTL назад

    svc.prepare_review("a/x", 8)

    assert "pr:7" in c.store.deleted_refs


@_PATCH_TO_PAYLOAD
def test_gc_failure_does_not_break_prepare(_to_payload):
    """Сбой GC не роняет подготовку ревью (fail-soft)."""
    c = _components()
    c.store.list_overlay_refs.side_effect = RuntimeError("db down")
    svc = _service(c)

    assert svc.prepare_review("a/x", 8) == {"status": "ok"}   # не бросил


@_PATCH_TO_PAYLOAD
def test_prepare_reserves_session_before_building_overlay(_to_payload):
    """C2: резервация session_store.save() должна произойти ДО ReviewService.prepare.

    prepare() строит overlay изнутри (через несколько секунд GitHub round-trip'ов).
    Если резервация переедет ПОСЛЕ prepare (или пропадёт), окно между появлением
    overlay и записью «свидетельства о жизни» остаётся незащищённым — GC другого
    процесса reviewer-mcp сочтёт overlay сиротой и снесёт его посреди идущего ревью.
    """
    c = _components()
    svc = _service(c)
    calls: list[str] = []
    svc._session_store.save = MagicMock(
        side_effect=lambda *a, **k: calls.append("session_store.save")
    )
    svc._review_service.prepare = MagicMock(
        side_effect=lambda *a, **k: calls.append("review_service.prepare") or MagicMock()
    )

    svc.prepare_review("a/x", 9)

    assert calls, "ни save, ни prepare не были вызваны"
    assert calls[0] == "session_store.save"
    assert "review_service.prepare" in calls


@_PATCH_TO_PAYLOAD
def test_prepare_releases_reservation_on_branch_not_tracked(_to_payload):
    """C2: резервация снимается на раннем return при BranchNotTrackedError.

    Без session_store.delete() на этом пути пустая строка-резервация
    (repo, pr) провисела бы в review_sessions до TTL, хотя overlay даже не
    начинал строиться (ReviewService.prepare упал раньше).
    """
    c = _components()
    svc = _service(c)
    svc._review_service.prepare = MagicMock(side_effect=BranchNotTrackedError("feature/zzz"))

    out = svc.prepare_review("a/x", 9)

    assert out["status"] == "skipped"
    assert svc._session_store.calls == [("save", "a/x", 9), ("delete", "a/x", 9)]


@_PATCH_TO_PAYLOAD
def test_prepare_releases_reservation_and_reraises_on_prepare_failure(_to_payload):
    """C2: резервация снимается при любом сбое prepare(), исходное исключение не глотается.

    Иначе (а) пустая строка-резервация провиснет в review_sessions до TTL, и
    (б) вызывающий MCP-клиент не узнает о реальной причине сбоя ReviewService.prepare.
    """
    c = _components()
    svc = _service(c)
    svc._review_service.prepare = MagicMock(side_effect=RuntimeError("github недоступен"))

    with pytest.raises(RuntimeError, match="github недоступен"):
        svc.prepare_review("a/x", 9)

    assert svc._session_store.calls == [("save", "a/x", 9), ("delete", "a/x", 9)]


@_PATCH_TO_PAYLOAD
def test_prepare_keeps_overlay_of_long_running_active_session(_to_payload):
    """PRI-212 (ядро задачи): активное ревью дольше TTL не теряет свой overlay.

    started_at старше TTL (ревью началось давно), но last_seen_at свежий
    (обращения к тулам продолжаются) — GC обязан считать сессию живой и не
    трогать pr:7. До keepalive фильтр шёл по started_at и сносил overlay
    прямо из-под работающего анализа.
    """
    c = _components()
    svc = _service(c)
    long_session = MagicMock()
    long_session.started_at = (
        datetime.now(timezone.utc)
        - timedelta(hours=svc.settings.review_session_ttl_hours + 1)
    )
    long_session.last_seen_at = datetime.now(timezone.utc)  # активность прямо сейчас
    svc._sessions[("a/x", 7)] = long_session

    svc.prepare_review("a/x", 8)

    assert "pr:7" not in c.store.deleted_refs
