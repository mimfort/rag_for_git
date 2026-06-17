"""Персистентность/регидрация сессии MCPReviewService (PRI-99).

Эмулируем рестарт процесса очисткой _sessions между prepare_review и
publish_review; проверяем регидрацию из (фейкового) SessionStore, промах →
ValueError с recovery hint, и fail-soft записи.
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import MagicMock, patch

from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService

SOURCE_A = "y = 0\nx = 1\n"
PATCH_A = "@@ -1,1 +1,2 @@\n y = 0\n+x = 1"
RAW = {
    "category": "correctness", "severity": "high", "file": "a.py", "line": 2,
    "code_quote": "x = 1", "message": "bug here", "suggestion": None,
    "fix": None, "confidence": 0.9,
}


class _FakeSessionStore:
    """In-memory подложка: эмулирует JSONB через json round-trip."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, int], dict] = {}

    def save(self, repo, pr, payload):
        self.rows[(repo, pr)] = json.loads(json.dumps(payload, ensure_ascii=False))

    def load(self, repo, pr, ttl_hours):
        return self.rows.get((repo, pr))

    def delete(self, repo, pr):
        self.rows.pop((repo, pr), None)


class _FakeChangedFile:
    def __init__(self, path, status, patch):
        self.path, self.status, self.patch = path, status, patch


class _FakeVCS:
    def __init__(self, number=7):
        self._number = number
        self.published = []
        self.close_calls = 0

    def get_pull_request(self, number):
        from reviewer.vcs.base import PullRequest
        return PullRequest(number=self._number, base_sha="base123", head_sha="head456",
                           base_ref="main", title="T", body="", draft=False)

    def get_changed_files(self, number):
        return [_FakeChangedFile("a.py", "modified", PATCH_A)]

    def get_file_at_ref(self, path, ref):
        return SOURCE_A if path == "a.py" else None

    def list_existing_fingerprints(self, number):
        return set()

    def publish_review(self, number, head_sha, summary, comments):
        self.published.append({"summary": summary, "comments": list(comments)})

    def compare_files(self, base_sha, head_sha):
        return []

    def close(self):
        self.close_calls += 1


def _settings() -> Settings:
    s = Settings()
    s.review_history = False
    s.review_skip_drafts = True
    s.review_max_files = 50
    s.review_session_persist = True           # включаем персист в этом тесте
    s.review_session_ttl_hours = 24
    s.voyage_api_key = "test"
    s.github_token = "test"
    return s


def _components() -> MagicMock:
    c = MagicMock()
    c.store = MagicMock()
    c.embedder = MagicMock()
    c.retriever = MagicMock()
    c.retriever.retrieve.return_value.as_context.return_value = "(результат)"
    c.graph = MagicMock()
    c.graph.expand.return_value = set()
    c.graph.callers.return_value = set()
    c.graph.find_symbol.return_value = []
    return c


class _FakeChunk:
    def __init__(self, node_id):
        self.node_id = node_id


def _fake_chunk(path, source):
    return [_FakeChunk(f"{path}#foo")]


def _make_service(store):
    """Сервис с vcs_factory=None (персист включён); _create_vcs_provider пропатчен."""
    svc = MCPReviewService(_settings(), _components(), vcs_factory=None)
    svc._session_store = store                 # внедряем фейковую подложку
    return svc


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_after_restart_rehydrates_session(_ov, _ch) -> None:
    store = _FakeSessionStore()
    svc = _make_service(store)
    vcs = _FakeVCS()
    with patch.object(svc._review_service, "_create_vcs_provider", return_value=vcs):
        svc.prepare_review("o/r", 7)
        assert ("o/r", 7) in store.rows               # сессия персистнута
        svc._sessions.clear()                          # эмуляция рестарта процесса
        report = svc.publish_review("o/r", 7, summary="s", findings=[RAW], dry_run=True)
    assert report["inline"][0]["line"] == 2            # регидрация дала рабочую сессию
    assert ("o/r", 7) not in store.rows                # cleanup удалил персист


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_tool_after_restart_rehydrates_session(_ov, _ch) -> None:
    store = _FakeSessionStore()
    svc = _make_service(store)
    vcs = _FakeVCS()
    with patch.object(svc._review_service, "_create_vcs_provider", return_value=vcs):
        svc.prepare_review("o/r", 7)
        svc._sessions.clear()
        out = svc.read_file("o/r", 7, "a.py", 1, 10)   # тул через _session → регидрация
    assert "x = 1" in out
    assert ("o/r", 7) in svc._sessions                 # кэш прогрет регидрацией


def test_publish_miss_raises_with_recovery_hint() -> None:
    store = _FakeSessionStore()                          # пусто: prepare не вызывался
    svc = _make_service(store)
    with pytest.raises(ValueError, match="prepare_review"):
        svc.publish_review("o/r", 7, summary="s", findings=[RAW], dry_run=True)


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_persist_disabled_no_rehydration(_ov, _ch) -> None:
    """review_session_persist=False: store не задействуется, после рестарта — ValueError."""
    s = _settings()
    s.review_session_persist = False
    svc = MCPReviewService(s, _components(), vcs_factory=None)
    vcs = _FakeVCS()
    with patch.object(svc._review_service, "_create_vcs_provider", return_value=vcs):
        svc.prepare_review("o/r", 7)                       # _ensure_session_store() → None, без save
        svc._sessions.clear()                              # эмуляция рестарта
        with pytest.raises(ValueError, match="prepare_review"):
            svc.publish_review("o/r", 7, summary="s", findings=[RAW], dry_run=True)
