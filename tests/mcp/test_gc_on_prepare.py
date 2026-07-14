"""Ядро бага: prepare_review без publish_review оставлял overlay навсегда.

Сценарий: ревью PR 7 подготовили, но публикация не состоялась (пользователь
отменил / оркестрирующая сессия упала) — _cleanup не вызвался. Следующий
prepare_review (ДРУГОГО PR) обязан подчистить осиротевший overlay pr:7.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService

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
    s.review_session_persist = False   # не трогаем реальный Postgres
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
    """

    def save(self, repo: str, pr: int, payload: dict) -> None:
        pass

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
    svc._sessions[("a/x", 7)] = MagicMock()   # ревью PR 7 идёт прямо сейчас

    svc.prepare_review("a/x", 8)

    assert "pr:7" not in c.store.deleted_refs


@_PATCH_TO_PAYLOAD
def test_gc_failure_does_not_break_prepare(_to_payload):
    """Сбой GC не роняет подготовку ревью (fail-soft)."""
    c = _components()
    c.store.list_overlay_refs.side_effect = RuntimeError("db down")
    svc = _service(c)

    assert svc.prepare_review("a/x", 8) == {"status": "ok"}   # не бросил
