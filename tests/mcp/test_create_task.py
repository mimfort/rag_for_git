"""Сервисный слой create_task (PRI-213)."""
import logging

import pytest

from reviewer.mcp.service import MCPReviewService


class _Settings:
    def __init__(self, types):
        self._types = types

    def configured_board_types(self):
        return list(self._types)


class _Provider:
    def __init__(self):
        self.created = None
        self.closed = False

    def create(self, doc_md, *, title, target, project):
        self.created = {"doc_md": doc_md, "title": title, "target": target,
                        "project": project}
        return {"key": "PRI-42", "url": "https://b/#PRI-42", "board_id": "u1",
                "target_resolved": target, "warnings": []}

    def fetch_one(self, key):
        return {"raw": key}

    def normalize(self, raw):
        return {"key": "PRI-42", "description": "## Проблема\n\nтекст"}

    def close(self):
        self.closed = True


class _TaskService:
    def __init__(self):
        self.indexed = []

    def index_task(self, task):
        self.indexed.append(task)
        return {"key": task.get("key"), "embedded": True}


class _Components:
    def __init__(self, task_service):
        self.task_service = task_service


@pytest.fixture
def service(monkeypatch):
    def _make(types=("yougile",), provider=None):
        provider = provider or _Provider()
        tasks = _TaskService()
        svc = MCPReviewService.__new__(MCPReviewService)
        svc.settings = _Settings(types)
        svc.components = _Components(tasks)
        monkeypatch.setattr("reviewer.mcp.service.make_board_provider",
                            lambda *a, **kw: provider)
        return svc, provider, tasks
    return _make


def test_create_task_renders_canonical_markdown(service):
    svc, provider, _ = service()
    res = svc.create_task(title="Заголовок", problem="Суть",
                          steps=["Шаг"], criteria=["Критерий"], project="PRI",
                          target="Движок")
    assert res["status"] == "ok"
    assert res["key"] == "PRI-42"
    assert res["url"] == "https://b/#PRI-42"
    doc = provider.created["doc_md"]
    assert doc.startswith("## Проблема")
    assert "## Что сделать" in doc and "1. Шаг" in doc
    assert "## Критерии приёмки" in doc
    assert provider.created["title"] == "Заголовок"     # заголовок отдельным полем
    assert "Заголовок" not in doc


def test_create_task_write_through_indexes_task(service):
    svc, _, tasks = service()
    res = svc.create_task(title="t", problem="p", project="PRI")
    assert res["reindexed"] is True
    assert tasks.indexed and tasks.indexed[0]["key"] == "PRI-42"


def test_create_task_closes_provider(service):
    svc, provider, _ = service()
    svc.create_task(title="t", problem="p", project="PRI")
    assert provider.closed is True


def test_create_task_requires_board_type_when_ambiguous(service):
    svc, _, _ = service(types=("yougile", "youtrack"))
    res = svc.create_task(title="t", problem="p", project="PRI")
    assert res["status"] == "error"
    assert "board_type" in res["reason"]


def test_create_task_rejects_unconfigured_board(service):
    svc, _, _ = service(types=("yougile",))
    res = svc.create_task(title="t", problem="p", project="PRI",
                          board_type="youtrack")
    assert res["status"] == "error"


def test_create_task_returns_error_dict_on_provider_failure(service):
    class _Boom(_Provider):
        def create(self, doc_md, *, title, target, project):
            raise ValueError("проект не найден")

    svc, provider, _ = service(provider=_Boom())
    res = svc.create_task(title="t", problem="p", project="NOPE")
    assert res["status"] == "error"
    assert "проект не найден" in res["reason"]
    assert provider.closed is True


def test_create_task_writethrough_skipped_when_key_missing(service, caplog):
    # Деградированный ключ (пустая строка — типично для YouTrack без idReadable):
    # write-through должен пропускаться без сетевого fetch_one, а не тихо съедаться
    # общим except.
    class _NoKeyProvider(_Provider):
        def create(self, doc_md, *, title, target, project):
            return {"key": "", "url": None, "board_id": "u1",
                    "target_resolved": target,
                    "warnings": ["ответ YouTrack не содержит idReadable — "
                                 "задача создана, но её ключ не определён"]}

        def fetch_one(self, key):
            raise AssertionError("fetch_one не должен вызываться при пустом ключе")

    svc, provider, tasks = service(provider=_NoKeyProvider())
    with caplog.at_level(logging.WARNING):
        res = svc.create_task(title="t", problem="p", project="PRI")

    assert res["status"] == "ok"        # факт создания задачи не откатывается
    assert res["reindexed"] is False
    assert res["key"] == ""
    assert tasks.indexed == []
    assert any(
        "write-through" in r.message and "пропущен" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]


def test_create_task_writethrough_failsoft_when_fetch_one_raises(service, caplog):
    # fetch_one упал при нормальном ключе (сбой доски) — задача уже создана,
    # это не должно откатывать успех create_task, только reindexed=False.
    class _BoomFetch(_Provider):
        def fetch_one(self, key):
            raise RuntimeError("boom")

    svc, provider, tasks = service(provider=_BoomFetch())
    with caplog.at_level(logging.WARNING):
        res = svc.create_task(title="t", problem="p", project="PRI")

    assert res["status"] == "ok"
    assert res["reindexed"] is False
    assert res["key"] == "PRI-42"
    assert tasks.indexed == []
    assert any(
        "write-through" in r.message and "не удался" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]
