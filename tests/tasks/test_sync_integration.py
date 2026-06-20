"""Интеграционный sync_board: идемпотентность (watermark) + PR-рёбра.

Использует фейковый provider (без живого yougile), но реальные
TaskService/ChunkStore/TaskGraph из build_components. Нужны Postgres/Neo4j +
ключ Voyage. Точечная очистка только тестовых ключей — реальный корпус не трогаем.
"""
import pytest

from reviewer.app import build_components
from reviewer.config.settings import Settings
from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.sync import SyncService

pytestmark = pytest.mark.integration

_KEYS = ["ZID-901", "ZID-902"]
_REF = "tasks:ztest"


class FakeProvider:
    def __init__(self, raws):
        self._raws = raws

    def iter_raw(self, board, limit):
        for r in self._raws:
            yield r

    def normalize(self, raw):
        return {"key": raw.key, "aliases": [raw.project_code], "title": raw.title,
                "description": raw.description, "criteria": [], "status": raw.status,
                "url": None, "links": []}


def _raw(key, ts, desc=""):
    return RawTask(key=key, project_code=key.replace("ZID", "ZPRI"), title=key,
                   description=desc, status="S", subtask_ids=[], timestamp=ts)


def _cleanup(comps):
    comps.task_store.delete_tasks(_KEYS)
    if comps.task_graph is not None:
        comps.task_graph.delete_tasks(_KEYS)
    comps.store.set_index_meta("", _REF, "0")


def test_sync_idempotent_and_pr_edge():
    comps = build_components(Settings(), connect=True)
    _cleanup(comps)  # детерминированный старт
    try:
        raws = [_raw("ZID-901", 1000),
                _raw("ZID-902", 1000, desc="impl https://github.com/o/r/pull/7")]
        svc = SyncService(FakeProvider(raws), comps.task_service, comps.store)

        first = svc.run(board="ztest")
        assert first["changed"] == 2 and first["embedded"] == 2
        assert first["cursor_advanced"] is True

        second = svc.run(board="ztest")
        assert second["changed"] == 0 and second["unchanged"] == 2
        assert second["cursor_advanced"] is False

        if comps.task_graph is not None:
            assert "ZID-902" in set(comps.task_graph.keys_with_prs())
    finally:
        _cleanup(comps)
