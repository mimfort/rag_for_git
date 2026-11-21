"""search_tasks остаётся рендером поверх структурных хитов (PRI-257)."""
import pytest

from reviewer.tasks.store import TaskHit


from reviewer.tasks.service import TaskService


class _Svc:
    """Носитель методов TaskService без его зависимостей: рендер их не требует."""

    render_hits = TaskService.render_hits
    search_tasks = TaskService.search_tasks

    def __init__(self, hits):
        self._hits = hits

    def search_hits(self, query, top_k=None, project=None):
        return self._hits


def test_ceiling_rail_note_kept():
    hits = [TaskHit(key=f"ID-{i}", title="T", status=None, score=0.03, aliases=[])
            for i in range(12)]
    assert "показано 8 из 12 (рельса ceiling)" in _Svc(hits).search_tasks("q")


def test_render_format_unchanged():
    svc = _Svc([TaskHit(key="ID-1", title="Заголовок", status="done", score=0.0321,
                        aliases=["PRI-1"])])
    assert svc.search_tasks("q") == "1. ID-1 [done] Заголовок (score 0.0321)"


def test_empty_hits_and_unavailable_source_differ():
    assert _Svc([]).search_tasks("q") == "(no similar tasks found)"
    assert _Svc(None).search_tasks("q") == "(task search unavailable)"


class _EmbedderRaising:
    def __init__(self, exc):
        self._exc = exc

    def embed_query(self, query):
        raise self._exc


def _svc(exc):
    return TaskService(None, None, _EmbedderRaising(exc))


def test_search_hits_swallows_embedder_failure_by_default():
    """Публичный путь остаётся немым: /ask и грунтовка на этом стоят."""
    from voyageai.error import APIError
    assert _svc(APIError("HTTP code 403")).search_hits("q") is None


def test_search_hits_reraises_embedder_failure_when_strict():
    from voyageai.error import APIError
    with pytest.raises(APIError):
        _svc(APIError("HTTP code 403")).search_hits("q", strict=True)


def test_search_hits_stays_soft_on_other_failures_when_strict():
    """Строгость адресная: непонятный сбой по-прежнему гасится."""
    assert _svc(RuntimeError("boom")).search_hits("q", strict=True) is None
