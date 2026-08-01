from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
from reviewer.tasks.boards.base import NativeSubtaskIdentity, RawTask
from reviewer.tasks.boards.registry import (
    BoardProviderRegistry,
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderOptionSpec,
    ProviderSetupSpec,
)
from reviewer.tasks.subtasks import (
    SubtaskBatchResult,
    SubtaskPreflight,
    SubtaskService,
    WriteThroughResult,
)

SUBTASK = {
    "title": "Дочерняя задача",
    "problem": "Нужно разделить работу",
    "steps": ["Сделать часть"],
    "criteria": ["Результат проверен"],
    "context": None,
}
_UNSET = object()


def _raw(
    key: str,
    board_id: str,
    *,
    title: str,
    subtask_ids: list[str] | None = None,
) -> RawTask:
    return RawTask(
        key=key,
        project_code=key,
        title=title,
        description="Описание",
        status="Новые",
        subtask_ids=list(subtask_ids or []),
        timestamp=1,
        board_id=board_id,
        provider_data={
            "source_board_id": "board-1",
            "source_column_id": "column-1",
        },
    )


class _Lock:
    def ensure_alive(self):
        return None


class _MemoryLedger:
    def __init__(self):
        self.operation = None

    def load(self, idempotency_key):
        if self.operation is None or self.operation.idempotency_key != idempotency_key:
            return None
        return deepcopy(self.operation)

    def insert(self, operation):
        self.operation = deepcopy(operation)
        return deepcopy(self.operation)

    def checkpoint(self, operation, *, expected_revision):
        assert self.operation.revision == expected_revision
        state = deepcopy(operation.state)
        state["revision"] = expected_revision + 1
        self.operation = replace(operation, state=state)
        return deepcopy(self.operation)

    @contextmanager
    def try_parent_lock(self, board_type, parent_task_id):
        yield _Lock()


class _TaskService:
    def __init__(self):
        self.calls = []
        self.effects = []

    def index_batch(self, briefs):
        self.calls.append(deepcopy(briefs))
        if self.effects:
            effect = self.effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            return effect
        return [
            {
                "key": brief["key"],
                "warnings": [],
                "links_stored": True if "links" in brief else None,
            }
            for brief in briefs
        ]


class _Provider:
    board_type = "fake"
    capabilities = frozenset({"native_subtasks"})

    def __init__(self, secret="runtime-secret"):
        self.secret = secret
        self.parent = _raw("PRI-224", "parent-id", title="Родитель")
        self.children = {}
        self.closed = 0
        self.board_writes = 0
        self.fetch_calls = []
        self.parent_fetches = 0
        self.parent_point_read_effect = _UNSET
        self.normalize_calls = []
        self.missing_child = False
        self.normalize_error = None

    def validate_connection(self, project=None):
        return {}

    def iter_raw(self, board, limit):
        return []

    def normalize_meta(self, raw):
        return self.normalize(raw)

    def list_targets(self, project):
        return {"targets": [], "options": [], "warnings": []}

    def create(self, doc_md, *, title, target, project):
        return {}

    def finish(self, key, pr_url, *, note=None, mark_done=True, target=None):
        return {}

    def close(self):
        self.closed += 1

    def fetch_one(self, key):
        self.fetch_calls.append(key)
        if key in {self.parent.key, self.parent.board_id}:
            self.parent_fetches += 1
            if self.parent_fetches == 4 and self.parent_point_read_effect is not _UNSET:
                effect = self.parent_point_read_effect
                if isinstance(effect, BaseException):
                    raise effect
                return deepcopy(effect)
            return deepcopy(self.parent)
        child = self.children.get(key)
        if child is not None and self.missing_child:
            return None
        return deepcopy(child)

    def normalize(self, raw):
        self.normalize_calls.append(raw.board_id)
        if self.normalize_error is not None and raw.board_id != self.parent.board_id:
            raise self.normalize_error
        links = (
            [{"key": self.children[subtask_id].key} for subtask_id in raw.subtask_ids]
            if raw.board_id == self.parent.board_id
            else [{"key": self.parent.key}]
        )
        return {
            "key": raw.key,
            "aliases": [],
            "title": raw.title,
            "description": raw.description,
            "criteria": [],
            "status": raw.status,
            "url": f"https://board/{raw.board_id}",
            "project": "PRI",
            "attachments": [],
            "links": links,
        }

    def reconcile_native_subtasks(self, source_board_id, markers):
        return []

    def create_native_subtask(self, doc_md, *, title, source_column_id, marker):
        self.board_writes += 1
        child = _raw("PRI-225", "child-id", title=title)
        self.children[child.board_id] = child
        self.children[child.key] = child
        return NativeSubtaskIdentity(
            board_id=child.board_id,
            key=child.key,
            title=child.title,
            url="https://board/child-id",
        )

    def replace_native_subtasks(self, parent_task_id, subtask_ids):
        self.board_writes += 1
        self.parent.subtask_ids = list(subtask_ids)


class _FactoryState:
    def __init__(self, provider):
        self.provider = provider
        self.calls = 0

    def factory(self, context: ProviderBuildContext):
        self.calls += 1
        return self.provider


def _service(
    *,
    provider=None,
    capabilities=frozenset({"native_subtasks"}),
    subtask_service=None,
    task_service=None,
):
    provider = provider or _Provider()
    factory = _FactoryState(provider)
    registry = BoardProviderRegistry([
        BoardProviderSpec(
            board_type="fake",
            factory=factory.factory,
            credential_fields=(CredentialFieldSpec("FAKE_TOKEN", "Token", secret=True),),
            setup=ProviderSetupSpec("Fake", "https://fake/help", "Configure."),
            option_fields=(ProviderOptionSpec("lane", "Lane"),),
            capabilities=capabilities,
        )
    ])
    tasks = task_service or _TaskService()
    state_machine = subtask_service or SubtaskService(_MemoryLedger())
    service = MCPReviewService.__new__(MCPReviewService)
    service.settings = Settings(_env_file=None)
    service.components = SimpleNamespace(
        task_service=tasks,
        subtask_service=state_machine,
    )
    service._board_registry = registry
    service._board_credentials = ProviderCredentialSource(
        values={"FAKE_TOKEN": provider.secret}
    )
    return service, provider, tasks, factory, state_machine


def _create(service, **overrides):
    values = {
        "parent_key": "PRI-224",
        "subtasks": [SUBTASK],
        "idempotency_key": "attempt-1",
        "board_type": None,
        "project": "PRI",
        "provider_options": {"lane": "Backend"},
    }
    values.update(overrides)
    return service.create_subtasks(**values)


class _PreflightOnly:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def preflight(self, request):
        self.requests.append(request)
        return SubtaskPreflight(None, self.result)

    def run(self, *args, **kwargs):
        raise AssertionError("run не должен вызываться для terminal preflight")


@pytest.mark.parametrize(
    "result",
    [
        SubtaskBatchResult(
            status="ok",
            board_type="fake",
            parent_key="PRI-224",
            idempotency_key="attempt-1",
            resumed=True,
            reindexed=True,
        ),
        SubtaskBatchResult(
            status="error",
            board_type="fake",
            parent_key="PRI-224",
            idempotency_key="attempt-1",
            resumed=True,
            category="conflict",
            retryable=False,
        ),
    ],
)
def test_terminal_preflight_returns_before_provider_factory(result):
    preflight = _PreflightOnly(result)
    service, _, _, factory, _ = _service(subtask_service=preflight)

    out = _create(service)

    assert out["status"] == result.status
    assert out.get("category") == result.category
    assert factory.calls == 0
    request = preflight.requests[0]
    assert request.board_type == "fake"
    assert request.project == "PRI"
    assert request.provider_options == {"lane": "Backend"}


def test_unsupported_registry_capability_does_not_write_and_closes_provider():
    provider = _Provider()
    service, _, tasks, factory, state_machine = _service(
        provider=provider,
        capabilities=frozenset(),
    )

    out = _create(service)

    assert out["status"] == "error"
    assert out["category"] == "unsupported"
    assert provider.board_writes == 0
    assert tasks.calls == []
    assert state_machine._store.operation is None
    assert factory.calls == 1
    assert provider.closed == 1


def test_provider_capability_attribute_cannot_spoof_registry_metadata():
    provider = _Provider()
    provider.capabilities = frozenset({"native_subtasks"})
    service, _, _, _, _ = _service(provider=provider, capabilities=frozenset())

    out = _create(service)

    assert out["category"] == "unsupported"
    assert provider.board_writes == 0
    assert provider.closed == 1


def test_strict_write_through_indexes_parent_then_children_once_and_completes():
    provider = _Provider()
    provider.parent_point_read_effect = _raw(
        "PRI-224",
        "parent-id",
        title="Родитель обновлён на доске",
        subtask_ids=["child-id"],
    )
    service, provider, tasks, _, state_machine = _service(provider=provider)

    out = _create(service)

    assert out["status"] == "ok"
    assert out["reindexed"] is True
    assert state_machine._store.operation.status == "complete"
    assert len(tasks.calls) == 1
    assert [brief["key"] for brief in tasks.calls[0]] == ["PRI-224", "PRI-225"]
    assert tasks.calls[0][0]["title"] == "Родитель обновлён на доске"
    assert provider.fetch_calls[-2:] == ["parent-id", "child-id"]
    assert provider.normalize_calls == ["parent-id", "child-id"]
    assert provider.closed == 1


@pytest.mark.parametrize(
    "parent_effect",
    [
        None,
        _raw(
            "PRI-999",
            "other-parent-id",
            title="Другой parent",
            subtask_ids=["child-id"],
        ),
        _raw("PRI-224", "parent-id", title="Связь удалена", subtask_ids=[]),
        RuntimeError("parent read failed with runtime-secret"),
    ],
    ids=["deleted", "identity-changed", "attachment-changed", "read-failed"],
)
def test_parent_point_read_failure_remains_reindex_pending_without_stale_index(
    parent_effect,
):
    provider = _Provider()
    provider.parent_point_read_effect = parent_effect
    service, provider, tasks, _, state_machine = _service(provider=provider)

    out = _create(service)

    assert out["status"] == "partial"
    assert out["category"] == "reindex_pending"
    assert out["reindexed"] is False
    assert state_machine._store.operation.status == "board_complete"
    assert tasks.calls == []
    assert provider.fetch_calls[-1] == "parent-id"
    assert provider.normalize_calls == []
    assert "runtime-secret" not in repr(out)
    assert "runtime-secret" not in repr(state_machine._store.operation.state)
    assert provider.closed == 1


def test_write_through_deduplicates_repeated_child_identity_but_rejects_parent_collision():
    service, provider, tasks, _, _ = _service()
    child = _raw("PRI-225", "child-id", title="Дочерняя задача")
    provider.children[child.board_id] = child
    provider.parent.subtask_ids = [child.board_id]
    identity = NativeSubtaskIdentity(child.board_id, child.key, child.title)

    def sanitize(value):
        return str(value)

    deduplicated = service._write_through_subtasks(
        provider,
        provider.parent,
        (identity, identity),
        sanitize,
    )
    collision = service._write_through_subtasks(
        provider,
        provider.parent,
        (NativeSubtaskIdentity(provider.parent.board_id, "PRI-999", "Wrong"),),
        sanitize,
    )

    assert deduplicated == WriteThroughResult(True)
    assert [brief["key"] for brief in tasks.calls[0]] == ["PRI-224", "PRI-225"]
    assert collision.success is False
    assert len(tasks.calls) == 1


@pytest.mark.parametrize(
    ("case", "effect"),
    [
        ("missing_child", None),
        ("normalize_error", RuntimeError("normalize failed")),
        ("count_mismatch", [{"key": "PRI-224", "warnings": [], "links_stored": True}]),
        (
            "warnings",
            [
                {"key": "PRI-224", "warnings": ["graph failed"], "links_stored": True},
                {"key": "PRI-225", "warnings": [], "links_stored": True},
            ],
        ),
        (
            "links_false",
            [
                {"key": "PRI-224", "warnings": [], "links_stored": False},
                {"key": "PRI-225", "warnings": [], "links_stored": True},
            ],
        ),
        (
            "links_none",
            [
                {"key": "PRI-224", "warnings": [], "links_stored": None},
                {"key": "PRI-225", "warnings": [], "links_stored": True},
            ],
        ),
    ],
)
def test_strict_write_through_failures_remain_reindex_pending(case, effect):
    service, provider, tasks, _, state_machine = _service()
    if case == "missing_child":
        provider.missing_child = True
    elif case == "normalize_error":
        provider.normalize_error = effect
    else:
        tasks.effects.append(effect)

    out = _create(service)

    assert out["status"] == "partial"
    assert out["category"] == "reindex_pending"
    assert out["reindexed"] is False
    assert state_machine._store.operation.status == "board_complete"
    assert len(tasks.calls) == (0 if case in {"missing_child", "normalize_error"} else 1)
    assert provider.closed == 1


def test_index_batch_exception_is_safe_partial_and_closes_provider():
    secret = "runtime-secret"
    service, provider, tasks, _, state_machine = _service(provider=_Provider(secret))
    tasks.effects.append(RuntimeError(f"store rejected {secret}"))

    out = _create(service)

    assert out["status"] == "partial"
    assert out["category"] == "reindex_pending"
    assert state_machine._store.operation.status == "board_complete"
    assert secret not in repr(out)
    assert secret not in repr(state_machine._store.operation.state)
    assert provider.closed == 1


def test_partial_provider_warning_is_scrubbed_from_checkpoint_and_result():
    secret = "runtime-secret"

    class _PartialProvider(_Provider):
        def create_native_subtask(self, doc_md, *, title, source_column_id, marker):
            raise RuntimeError(f"provider failed with {secret}")

    service, provider, _, _, state_machine = _service(
        provider=_PartialProvider(secret)
    )

    out = _create(service)

    assert out["status"] == "partial"
    assert secret not in repr(out)
    assert secret not in repr(state_machine._store.operation.state)
    assert provider.closed == 1


class _ExplodingSubtaskService:
    def preflight(self, request):
        return SubtaskPreflight(None, None)

    def run(self, *args, **kwargs):
        raise RuntimeError("state machine exploded runtime-secret")


def test_state_machine_exception_returns_safe_error_and_closes_provider():
    service, provider, _, _, _ = _service(
        subtask_service=_ExplodingSubtaskService()
    )

    out = _create(service)

    assert out["status"] == "error"
    assert "runtime-secret" not in repr(out)
    assert provider.closed == 1
