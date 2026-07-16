"""Интеграционный sync_board: идемпотентность (watermark) + PR-рёбра.

Использует фейковый provider (без живого yougile), но реальные
TaskService/ChunkStore/TaskGraph из build_components. Нужны Postgres/Neo4j +
ключ Voyage. Точечная очистка только тестовых ключей — реальный корпус не трогаем.
"""
from contextlib import ExitStack
from functools import partial

import pytest

from reviewer.app import build_components
from reviewer.config.settings import Settings
from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.graph import PRRef
from reviewer.tasks.sync import SyncService

pytestmark = pytest.mark.integration

_KEYS = ["ZID-901", "ZID-902"]
_PR_REPO = "rag-reviewer-test-fixtures/sync-board-residue"
_PR_NUMBER = 211_001
_PR_URL = f"https://github.com/{_PR_REPO}/pull/{_PR_NUMBER}"
_PR_ID = f"{_PR_REPO}#{_PR_NUMBER}"
_REF = "tasks:fake:ztest"
_SHARED_TASK_KEY = "ZSYNC-PR-RESIDUE-SHARED"
_OWNED_TASK_KEYS = [*_KEYS, _SHARED_TASK_KEY]


class FakeProvider:
    board_type = "fake"

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


def _delete_orphan_pr(comps):
    if comps.graph is not None:
        comps.graph.driver.execute_query(
            "MATCH (p:PR {id: $id}) "
            "WHERE NOT EXISTS { MATCH (:Task)-[:IMPLEMENTED_BY]->(p) } "
            "DETACH DELETE p",
            id=_PR_ID,
        )


def _orphan_pr_count(comps):
    if comps.graph is None:
        return 0
    records, _, _ = comps.graph.driver.execute_query(
        "MATCH (p:PR {id: $id}) "
        "WHERE NOT EXISTS { MATCH (:Task)-[:IMPLEMENTED_BY]->(p) } "
        "RETURN count(p) AS n",
        id=_PR_ID,
    )
    return records[0]["n"]


def _pr_count(comps):
    if comps.graph is None:
        return 0
    records, _, _ = comps.graph.driver.execute_query(
        "MATCH (p:PR {id: $id}) RETURN count(p) AS n",
        id=_PR_ID,
    )
    return records[0]["n"]


def _cleanup(comps, keys):
    with ExitStack() as stack:
        stack.callback(comps.store.set_index_meta, "", _REF, "0")
        if comps.task_graph is not None:
            stack.callback(_delete_orphan_pr, comps)
            stack.callback(comps.task_graph.delete_tasks, keys)
        stack.callback(comps.task_store.delete_tasks, keys)


@pytest.fixture()
def components(request, monkeypatch):
    monkeypatch.setenv("TASK_BOARD_API_KEY", "ambient-legacy-key")
    monkeypatch.setenv("YOUGILE_API_KEY", "ambient-yougile-key")
    monkeypatch.setenv("YOUTRACK_TOKEN", "ambient-youtrack-token")
    monkeypatch.setenv("YOUTRACK_BASE_URL", "https://youtrack.test.invalid")
    settings = Settings(
        task_board_api_key="",
        yougile_api_key="",
        youtrack_token="",
    )
    comps = build_components(settings, connect=True)

    request.addfinalizer(comps.store.close)
    request.addfinalizer(comps.task_store.close)
    request.addfinalizer(comps.summary_store.close)
    if comps.graph is not None:
        request.addfinalizer(comps.graph.close)

    assert comps.sync_service is None
    comps.store.init_schema()
    if comps.graph is not None:
        comps.graph.init_schema()

    request.addfinalizer(partial(comps.store.set_index_meta, "", _REF, "0"))
    if comps.task_graph is not None:
        request.addfinalizer(partial(_delete_orphan_pr, comps))
        request.addfinalizer(partial(comps.task_graph.delete_tasks, _OWNED_TASK_KEYS))
    request.addfinalizer(partial(comps.task_store.delete_tasks, _OWNED_TASK_KEYS))

    _cleanup(comps, _OWNED_TASK_KEYS)
    return comps


def test_pr_identity_is_owned_by_sync_residue_test():
    assert _PR_ID.startswith("rag-reviewer-test-fixtures/sync-board-residue#")
    assert _PR_URL == f"https://github.com/{_PR_REPO}/pull/{_PR_NUMBER}"
    assert _PR_ID == f"{_PR_REPO}#{_PR_NUMBER}"


def test_sync_idempotent_and_pr_edge(components):
    raws = [_raw("ZID-901", 1000),
            _raw("ZID-902", 1000, desc=f"impl {_PR_URL}")]
    svc = SyncService([FakeProvider(raws)], components.task_service, components.store)

    first = svc.run(board="ztest")
    assert first["changed"] == 2 and first["embedded"] == 2
    assert first["cursor_advanced"] is True

    second = svc.run(board="ztest")
    assert second["changed"] == 0 and second["unchanged"] == 2
    assert second["cursor_advanced"] is False

    if components.task_graph is not None:
        assert "ZID-902" in set(components.task_graph.keys_with_prs())

    _cleanup(components, _KEYS)
    assert _orphan_pr_count(components) == 0


def test_cleanup_preserves_pr_linked_to_other_task(components):
    assert components.task_graph is not None
    pr = PRRef(repo=_PR_REPO, number=_PR_NUMBER, url=_PR_URL, sha="")
    for key in (*_KEYS, _SHARED_TASK_KEY):
        components.task_graph.upsert_task(key, [], key, "S", None)
        components.task_graph.link_pr(key, pr, [])

    _cleanup(components, _KEYS)

    assert _pr_count(components) == 1
    assert _orphan_pr_count(components) == 0

    components.task_graph.delete_tasks([_SHARED_TASK_KEY])
    _delete_orphan_pr(components)
    assert _pr_count(components) == 0


def test_cleanup_attempts_remaining_steps_after_store_failure(components, monkeypatch):
    assert components.task_graph is not None
    key = _KEYS[0]
    components.task_graph.upsert_task(key, [], key, "S", None)
    components.task_graph.link_pr(
        key,
        PRRef(repo=_PR_REPO, number=_PR_NUMBER, url=_PR_URL, sha=""),
        [],
    )
    delete_tasks = components.task_store.delete_tasks

    def fail_store_cleanup(keys):
        raise RuntimeError("store cleanup probe")

    monkeypatch.setattr(components.task_store, "delete_tasks", fail_store_cleanup)
    try:
        with pytest.raises(RuntimeError, match="store cleanup probe"):
            _cleanup(components, _KEYS)
    finally:
        monkeypatch.setattr(components.task_store, "delete_tasks", delete_tasks)

    assert key not in components.task_graph.list_keys()
    assert _orphan_pr_count(components) == 0
