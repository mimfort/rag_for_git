"""Сервисный слой create_task (PRI-213)."""
import logging

import pytest

from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
from reviewer.tasks.boards.base import TaskListing
from reviewer.tasks.boards.registry import (
    BoardProviderRegistry,
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderSetupSpec,
)


class _Settings:
    def __init__(self, types):
        self._types = types

    def configured_board_types(self):
        return list(self._types)


class _Provider:
    board_type = "fake"

    def __init__(self):
        self.created = None
        self.closed = False

    def validate_connection(self, project=None):
        return {}

    def iter_raw(self, board, limit, *, sync_filter=None, now_ms=None):
        return TaskListing(rows=())

    def create(self, doc_md, *, title, target, project):
        self.created = {"doc_md": doc_md, "title": title, "target": target,
                        "project": project}
        return {"key": "PRI-42", "url": "https://b/#PRI-42", "board_id": "u1",
                "target_resolved": target, "warnings": []}

    def fetch_one(self, key):
        return {"raw": key}

    def normalize(self, raw):
        return {"key": "PRI-42", "description": "## Проблема\n\nтекст"}

    def normalize_meta(self, raw):
        return self.normalize(raw)

    def list_targets(self, project):
        return {"targets": [], "options": [], "warnings": []}

    def finish(self, key, pr_url, *, note=None, mark_done=True, target=None):
        return {}

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
def service():
    def _make(types=("fake",), provider=None):
        provider = provider or _Provider()
        tasks = _TaskService()
        svc = MCPReviewService.__new__(MCPReviewService)
        svc.settings = Settings(_env_file=None)
        svc.components = _Components(tasks)
        specs = []
        values = {}
        for board_type in types:
            def factory(context: ProviderBuildContext, type_=board_type):
                provider.board_type = type_
                return provider

            env = f"{board_type.upper()}_TOKEN"
            specs.append(BoardProviderSpec(
                board_type=board_type,
                factory=factory,
                credential_fields=(CredentialFieldSpec(env, "Token", secret=True),),
                setup=ProviderSetupSpec(board_type, "https://fake/help", "Configure."),
            ))
            values[env] = "secret"
        svc._board_registry = BoardProviderRegistry(specs)
        svc._board_credentials = ProviderCredentialSource(values=values)
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
    svc, _, _ = service(types=("first", "second"))
    res = svc.create_task(title="t", problem="p", project="PRI")
    assert res["status"] == "error"
    assert "board_type" in res["reason"]


def test_create_task_rejects_unconfigured_board(service):
    svc, _, _ = service(types=("fake",))
    res = svc.create_task(title="t", problem="p", project="PRI",
                          board_type="other")
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
