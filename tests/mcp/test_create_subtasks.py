from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import reviewer.mcp.service as service_module
from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
from reviewer.tasks.boards.base import NativeSubtaskIdentity, RawTask
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.registry import (
    BoardProviderRegistry,
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderOptionSpec,
    ProviderSetupSpec,
)
from tests.provider_access import FAKE_PROVIDER_ACCESS
from reviewer.tasks.subtask_store import OperationConflictError
from reviewer.tasks.subtasks import (
    SubtaskBatchResult,
    SubtaskPreflight,
    SubtaskService,
)

SUBTASK = {
    "title": "Дочерняя задача",
    "problem": "Нужно разделить работу",
    "steps": ["Сделать часть"],
    "criteria": ["Результат проверен"],
    "context": None,
}
_UNSET = object()
RECOVERY_WARNING = "subtask operation failed; durable recovery used a safe fallback"


def _raw(
    key: str,
    board_id: str,
    *,
    title: str,
    subtask_ids: list[str] | None = None,
    project_code: str | None = None,
) -> RawTask:
    return RawTask(
        key=key,
        project_code=project_code or key,
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
        self.fail_complete_checkpoint_once = False

    def load(self, idempotency_key):
        if self.operation is None or self.operation.idempotency_key != idempotency_key:
            return None
        return deepcopy(self.operation)

    def insert(self, operation):
        self.operation = deepcopy(operation)
        return deepcopy(self.operation)

    def checkpoint(self, operation, *, expected_revision):
        assert self.operation.revision == expected_revision
        if operation.status == "complete" and self.fail_complete_checkpoint_once:
            self.fail_complete_checkpoint_once = False
            raise OperationConflictError("complete checkpoint stale runtime-secret")
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
        self.child_point_read_effect = _UNSET
        self.brief_overrides = {}
        self.parent_links_override = _UNSET
        self.identity_uuid_fallback = False
        self.child_sequence = 0
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
        if child is not None and self.child_point_read_effect is not _UNSET:
            effect = self.child_point_read_effect
            if isinstance(effect, BaseException):
                raise effect
            return deepcopy(effect)
        if child is not None and self.missing_child:
            return None
        return deepcopy(child)

    def normalize(self, raw):
        self.normalize_calls.append(raw.board_id)
        if self.normalize_error is not None and raw.board_id != self.parent.board_id:
            raise self.normalize_error
        if raw.board_id in self.brief_overrides:
            return deepcopy(self.brief_overrides[raw.board_id])
        links = (
            [
                {
                    "type": "subtask",
                    "key": self.children[subtask_id].key,
                }
                for subtask_id in raw.subtask_ids
            ]
            if raw.board_id == self.parent.board_id
            else [{"type": "parent", "key": self.parent.key}]
        )
        if raw.board_id == self.parent.board_id and self.parent_links_override is not _UNSET:
            links = deepcopy(self.parent_links_override)
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
        self.child_sequence += 1
        child_number = 224 + self.child_sequence
        child = _raw(
            f"PRI-{child_number}",
            "child-id" if self.child_sequence == 1 else f"child-id-{self.child_sequence}",
            title=title,
        )
        self.children[child.board_id] = child
        self.children[child.key] = child
        return NativeSubtaskIdentity(
            board_id=child.board_id,
            key=child.board_id if self.identity_uuid_fallback else child.key,
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
            setup=ProviderSetupSpec(
                "Fake", "https://fake/help", "Configure.", FAKE_PROVIDER_ACCESS
            ),
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


def _drop_credentials(service):
    service._board_credentials = ProviderCredentialSource(values={})


def _switch_runtime_to_other_provider(service):
    fake_spec = service._board_registry.get("fake")
    service._board_registry = BoardProviderRegistry(
        [
            fake_spec,
            replace(
                fake_spec,
                board_type="other",
                credential_fields=(
                    CredentialFieldSpec("OTHER_TOKEN", "Other token", secret=True),
                ),
            ),
        ]
    )
    service._board_credentials = ProviderCredentialSource(
        values={"OTHER_TOKEN": "other-secret"}
    )
    service.settings = SimpleNamespace(task_board_default=lambda: {"type": "other"})


class _PreflightOnly:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def preflight(self, request):
        self.requests.append(request)
        return SubtaskPreflight(None, self.result)

    def lookup_request(self, idempotency_key):
        return None

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


def test_complete_replay_runs_durable_preflight_without_current_credentials():
    service, _, _, factory, state_machine = _service()
    assert _create(service, board_type="fake")["status"] == "ok"
    preflight = MagicMock(wraps=state_machine.preflight)
    state_machine.preflight = preflight
    _drop_credentials(service)

    out = _create(service, board_type="fake")

    assert out["status"] == "ok"
    assert out["reindexed"] is True
    preflight.assert_called_once()
    assert factory.calls == 1


def test_complete_replay_restores_omitted_config_from_durable_request():
    service, _, _, factory, _ = _service()
    assert isinstance(service.settings, Settings)
    assert _create(service)["status"] == "ok"
    _drop_credentials(service)
    assert service.settings.task_board_default() is None

    out = _create(
        service,
        board_type=None,
        project=None,
        provider_options=None,
    )

    assert out["status"] == "ok"
    assert out["reindexed"] is True
    assert factory.calls == 1


def test_complete_replay_prefers_durable_type_over_changed_runtime_default():
    service, _, _, factory, _ = _service()
    assert _create(service, board_type="fake")["status"] == "ok"
    _switch_runtime_to_other_provider(service)

    replay = _create(
        service,
        board_type=None,
        project=None,
        provider_options=None,
    )
    conflict = _create(
        service,
        board_type="other",
        project=None,
        provider_options=None,
    )

    assert replay["status"] == "ok"
    assert replay["board_type"] == "fake"
    assert conflict["status"] == "error"
    assert conflict["category"] == "conflict"
    assert factory.calls == 1


def test_incomplete_omitted_type_uses_durable_provider_before_credential_check():
    service, provider, _, factory, state_machine = _service()
    provider.missing_child = True
    first = _create(service, board_type="fake")
    assert first["category"] == "reindex_pending"
    writes = provider.board_writes
    _switch_runtime_to_other_provider(service)

    out = _create(
        service,
        board_type=None,
        project=None,
        provider_options=None,
    )

    assert out["status"] == "error"
    assert out["category"] == "configuration"
    assert state_machine._store.operation.status == "board_complete"
    assert provider.board_writes == writes
    assert factory.calls == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"project": "OTHER"},
        {"provider_options": {"lane": "Other"}},
        {"parent_key": "PRI-999"},
        {"subtasks": [{**SUBTASK, "problem": "Другой payload"}]},
    ],
    ids=["project", "provider_options", "parent", "subtasks"],
)
def test_durable_config_does_not_hide_explicit_or_payload_conflicts(overrides):
    service, _, _, factory, _ = _service()
    assert _create(service)["status"] == "ok"
    _drop_credentials(service)
    replay_args = {
        "board_type": None,
        "project": None,
        "provider_options": None,
        **overrides,
    }

    out = _create(service, **replay_args)

    assert out["status"] == "error"
    assert out["category"] == "conflict"
    assert factory.calls == 1


def test_explicit_board_type_wins_over_durable_config_and_conflicts():
    service, _, _, factory, _ = _service()
    assert _create(service)["status"] == "ok"
    fake_spec = service._board_registry.get("fake")
    service._board_registry = BoardProviderRegistry(
        [
            fake_spec,
            replace(
                fake_spec,
                board_type="other",
                credential_fields=(
                    CredentialFieldSpec("OTHER_TOKEN", "Other token", secret=True),
                ),
            ),
        ]
    )
    _drop_credentials(service)

    out = _create(
        service,
        board_type="other",
        project=None,
        provider_options=None,
    )

    assert out["status"] == "error"
    assert out["category"] == "conflict"
    assert factory.calls == 1


def test_malformed_durable_request_never_supplies_omitted_config():
    service, provider, _, factory, state_machine = _service()
    assert _create(service)["status"] == "ok"
    state_machine._store.operation.state["revision"] = -1
    writes = provider.board_writes
    _drop_credentials(service)

    out = _create(
        service,
        board_type=None,
        project=None,
        provider_options=None,
    )

    assert out["status"] == "error"
    assert "revision" in out["reason"]
    assert provider.board_writes == writes
    assert factory.calls == 1


def test_hash_conflict_runs_durable_preflight_without_current_credentials():
    service, _, _, factory, state_machine = _service()
    assert _create(service, board_type="fake")["status"] == "ok"
    preflight = MagicMock(wraps=state_machine.preflight)
    state_machine.preflight = preflight
    _drop_credentials(service)
    changed = {**SUBTASK, "problem": "Другой payload"}

    out = _create(service, board_type="fake", subtasks=[changed])

    assert out["status"] == "error"
    assert out["category"] == "conflict"
    preflight.assert_called_once()
    assert factory.calls == 1


def test_completed_replay_keeps_secret_url_redacted_without_credentials():
    secret = "identity-url-secret"

    class _SecretIdentityProvider(_Provider):
        def create_native_subtask(self, doc_md, *, title, source_column_id, marker):
            identity = super().create_native_subtask(
                doc_md,
                title=title,
                source_column_id=source_column_id,
                marker=marker,
            )
            return replace(
                identity,
                title=f"provider title {secret}",
                url=f"https://board/child-id?token={secret}",
            )

    service, provider, _, factory, state_machine = _service(
        provider=_SecretIdentityProvider(secret)
    )

    first = _create(service, board_type="fake")
    persisted = deepcopy(state_machine._store.operation)
    _drop_credentials(service)
    replay = _create(
        service,
        board_type=None,
        project=None,
        provider_options=None,
    )

    safe_url = "https://board/child-id?token=[REDACTED]"
    assert first["status"] == "ok"
    assert first["attached"][0]["url"] == safe_url
    assert first["attached"][0]["title"] == SUBTASK["title"]
    assert persisted.state["items"][0]["url"] == safe_url
    assert persisted.state["items"][0]["title"] == SUBTASK["title"]
    assert replay["status"] == "ok"
    assert replay["attached"][0]["url"] == safe_url
    assert secret not in repr(first)
    assert secret not in repr(persisted.state)
    assert secret not in repr(replay)
    assert provider.board_writes > 0
    assert factory.calls == 1


def test_new_operation_without_credentials_fails_configuration_before_board_write():
    service, provider, _, factory, state_machine = _service()
    _drop_credentials(service)

    out = _create(
        service,
        board_type=None,
        project=None,
        provider_options=None,
    )

    assert out["status"] == "error"
    assert out["category"] == "configuration"
    assert provider.board_writes == 0
    assert factory.calls == 0
    assert state_machine._store.operation is None


def test_incomplete_operation_without_credentials_cannot_resume_board_work():
    service, provider, _, factory, state_machine = _service()
    provider.missing_child = True
    first = _create(service, board_type="fake")
    assert first["category"] == "reindex_pending"
    writes = provider.board_writes
    preflight = MagicMock(wraps=state_machine.preflight)
    state_machine.preflight = preflight
    _drop_credentials(service)

    out = _create(
        service,
        board_type=None,
        project=None,
        provider_options=None,
    )

    assert out["status"] == "error"
    assert out["category"] == "configuration"
    assert state_machine._store.operation.status == "board_complete"
    assert provider.board_writes == writes
    preflight.assert_called_once()
    assert factory.calls == 1


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


@pytest.mark.parametrize(
    "child_effect",
    [
        _raw("PRI-225", "other-child-id", title="Подменённая задача"),
        _raw("OTHER-1", "child-id", title="Несовместимая задача"),
    ],
    ids=["transport-mismatch", "canonical-mismatch"],
)
def test_child_point_read_identity_mismatch_never_indexes_or_completes(child_effect):
    provider = _Provider()
    provider.child_point_read_effect = child_effect
    service, provider, tasks, _, state_machine = _service(provider=provider)

    out = _create(service)

    assert out["status"] == "partial"
    assert out["category"] == "reindex_pending"
    assert state_machine._store.operation.status == "board_complete"
    assert tasks.calls == []
    assert provider.normalize_calls == []
    assert provider.closed == 1


@pytest.mark.parametrize(
    "identity",
    [
        NativeSubtaskIdentity("", "PRI-225", "Child"),
        NativeSubtaskIdentity("child-id", " ", "Child"),
        NativeSubtaskIdentity("child-id", "PRI-225", "Child", aliases=["TASK-2"]),
        NativeSubtaskIdentity("child-id", "PRI-225", "Child", aliases=(" ",)),
    ],
    ids=["blank-board-id", "blank-key", "aliases-not-tuple", "blank-alias"],
)
def test_malformed_persisted_child_identity_fails_before_index(identity):
    service, provider, tasks, _, _ = _service()
    provider.parent.subtask_ids = [identity.board_id]
    if identity.board_id:
        child = _raw("PRI-225", identity.board_id, title="Child")
        provider.children[identity.board_id] = child

    result = service._write_through_subtasks(
        provider,
        provider.parent,
        (identity,),
        str,
    )

    assert result.success is False
    assert tasks.calls == []
    assert provider.fetch_calls == ["parent-id"]


def test_uuid_fallback_child_identity_enriches_to_canonical_key():
    provider = _Provider()
    provider.identity_uuid_fallback = True
    service, provider, tasks, _, state_machine = _service(provider=provider)

    out = _create(service)

    assert out["status"] == "ok"
    assert state_machine._store.operation.status == "complete"
    assert [brief["key"] for brief in tasks.calls[0]] == ["PRI-224", "PRI-225"]


def test_child_identity_may_match_fetched_project_alias():
    service, provider, tasks, _, _ = _service()
    child = _raw(
        "ID-225",
        "child-id",
        title="Child",
        project_code="PRI-225",
    )
    provider.children[child.board_id] = child
    provider.parent.subtask_ids = [child.board_id]

    result = service._write_through_subtasks(
        provider,
        provider.parent,
        (NativeSubtaskIdentity(child.board_id, "PRI-225", child.title),),
        str,
    )

    assert result.success is True
    assert [brief["key"] for brief in tasks.calls[0]] == ["PRI-224", "ID-225"]


def test_duplicate_transport_still_validates_every_persisted_canonical_identity():
    service, provider, tasks, _, _ = _service()
    child = _raw("PRI-225", "child-id", title="Child")
    provider.children[child.board_id] = child
    provider.parent.subtask_ids = [child.board_id]

    result = service._write_through_subtasks(
        provider,
        provider.parent,
        (
            NativeSubtaskIdentity(child.board_id, child.key, child.title),
            NativeSubtaskIdentity(child.board_id, "OTHER-1", child.title),
        ),
        str,
    )

    assert result.success is False
    assert tasks.calls == []


def test_write_through_rejects_repeated_child_identity_and_parent_collision():
    service, provider, tasks, _, _ = _service()
    child = _raw("PRI-225", "child-id", title="Дочерняя задача")
    provider.children[child.board_id] = child
    provider.parent.subtask_ids = [child.board_id]
    identity = NativeSubtaskIdentity(child.board_id, child.key, child.title)

    def sanitize(value):
        return str(value)

    duplicate = service._write_through_subtasks(
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

    assert duplicate.success is False
    assert collision.success is False
    assert tasks.calls == []


def test_write_through_rejects_overlapping_canonical_child_identities():
    service, provider, tasks, _, _ = _service()
    provider.parent.subtask_ids = ["child-1", "child-2"]

    result = service._write_through_subtasks(
        provider,
        provider.parent,
        (
            NativeSubtaskIdentity(
                "child-1",
                "PRI-225",
                "First",
                aliases=("SHARED-1",),
            ),
            NativeSubtaskIdentity("child-2", "SHARED-1", "Second"),
        ),
        str,
    )

    assert result.success is False
    assert tasks.calls == []
    assert provider.fetch_calls == ["parent-id"]


@pytest.mark.parametrize("links", [_UNSET, None, {}], ids=["missing", "none", "not-list"])
def test_normalized_brief_requires_explicit_well_typed_links(links):
    provider = _Provider()
    brief = {
        "key": "PRI-224",
        "aliases": [],
        "title": "Parent",
        "description": "",
        "criteria": [],
        "status": "New",
        "url": None,
        "project": "PRI",
        "attachments": [],
    }
    if links is not _UNSET:
        brief["links"] = links
    provider.brief_overrides["parent-id"] = brief
    service, provider, tasks, _, state_machine = _service(provider=provider)

    out = _create(service)

    assert out["status"] == "partial"
    assert out["category"] == "reindex_pending"
    assert state_machine._store.operation.status == "board_complete"
    assert tasks.calls == []


@pytest.mark.parametrize(
    "parent_links",
    [
        [],
        [{"type": "relates", "key": "PRI-225"}],
        [{"type": "subtask", "key": "OTHER-1"}],
        [
            {"type": "subtask", "key": "PRI-225"},
            {"type": 42, "key": "PRI-100"},
        ],
    ],
    ids=["empty", "wrong-type", "wrong-key", "malformed-type"],
)
def test_parent_links_must_cover_normalized_child_as_native_subtask(parent_links):
    provider = _Provider()
    provider.parent_links_override = parent_links
    service, provider, tasks, _, state_machine = _service(provider=provider)

    out = _create(service)

    assert out["status"] == "partial"
    assert out["category"] == "reindex_pending"
    assert state_machine._store.operation.status == "board_complete"
    assert tasks.calls == []


def test_parent_subtask_links_cover_multiple_normalized_children():
    provider = _Provider()
    service, provider, tasks, _, state_machine = _service(provider=provider)
    second = {**SUBTASK, "title": "Вторая дочерняя задача"}

    out = _create(service, subtasks=[SUBTASK, second])

    assert out["status"] == "ok"
    assert state_machine._store.operation.status == "complete"
    assert [brief["key"] for brief in tasks.calls[0]] == [
        "PRI-224",
        "PRI-225",
        "PRI-226",
    ]


def test_parent_subtask_links_allow_valid_extra_relations():
    provider = _Provider()
    provider.parent_links_override = [
        {"type": "relates", "key": "PRI-100"},
        {"type": "subtask", "key": "PRI-225"},
    ]
    service, provider, tasks, _, state_machine = _service(provider=provider)

    out = _create(service)

    assert out["status"] == "ok"
    assert state_machine._store.operation.status == "complete"
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
        (
            "result_keys_reordered",
            [
                {"key": "PRI-225", "warnings": [], "links_stored": True},
                {"key": "PRI-224", "warnings": [], "links_stored": True},
            ],
        ),
        (
            "result_key_missing",
            [
                {"warnings": [], "links_stored": True},
                {"key": "PRI-225", "warnings": [], "links_stored": True},
            ],
        ),
        (
            "warnings_malformed",
            [
                {"key": "PRI-224", "warnings": None, "links_stored": True},
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
    def lookup_request(self, idempotency_key):
        return None

    def preflight(self, request):
        return SubtaskPreflight(None, None)

    def run(self, *args, **kwargs):
        raise RuntimeError("state machine exploded runtime-secret")

    def recover_result(self, request):
        return None


def test_consumer_exception_before_durable_row_returns_unknown_outcome():
    service, provider, _, _, _ = _service(
        subtask_service=_ExplodingSubtaskService()
    )

    out = _create(service)

    assert out["status"] == "partial"
    assert out["category"] == "unknown_outcome"
    assert out["retryable"] is True
    assert out["board_type"] == "fake"
    assert out["parent_key"] == "PRI-224"
    assert out["idempotency_key"] == "attempt-1"
    assert "runtime-secret" not in repr(out)
    assert out["category"] != "unsupported"
    assert provider.closed == 1


class _ReloadFailingSubtaskService(_ExplodingSubtaskService):
    def recover_result(self, request):
        raise RuntimeError("durable reload failed runtime-secret")


def test_durable_reload_failure_returns_safe_unknown_outcome():
    service, provider, _, _, _ = _service(
        subtask_service=_ReloadFailingSubtaskService()
    )

    out = _create(service, board_type="fake")

    assert out["status"] == "partial"
    assert out["category"] == "unknown_outcome"
    assert out["retryable"] is True
    assert RECOVERY_WARNING in out["warnings"]
    assert "runtime-secret" not in repr(out)
    assert out["category"] != "unsupported"
    assert provider.closed == 1


class _UnrenderableError(RuntimeError):
    def __str__(self):
        raise RuntimeError("error rendering failed runtime-secret")


class _UnrenderableErrorSubtaskService(_ExplodingSubtaskService):
    def run(self, *args, **kwargs):
        raise _UnrenderableError()


def test_recovery_survives_exception_with_broken_str():
    service, provider, _, _, _ = _service(
        subtask_service=_UnrenderableErrorSubtaskService()
    )

    out = _create(service)

    assert out["status"] == "partial"
    assert out["category"] == "unknown_outcome"
    assert out["retryable"] is True
    assert out["warnings"] == [RECOVERY_WARNING]
    assert "runtime-secret" not in repr(out)
    assert provider.closed == 1


def test_recovery_survives_sanitizer_failure_and_keeps_durable_result(monkeypatch):
    service, provider, _, _, state_machine = _service()
    state_machine._store.fail_complete_checkpoint_once = True

    def explode_sanitizer(value, *_args, **_kwargs):
        if isinstance(value, OperationConflictError):
            raise TypeError("sanitizer failed runtime-secret")
        return str(value).replace("runtime-secret", "[REDACTED]")

    monkeypatch.setattr(service_module, "sanitize_provider_text", explode_sanitizer)

    out = _create(service, board_type="fake")

    assert out["status"] == "partial"
    assert out["category"] == "reindex_pending"
    assert out["retryable"] is True
    assert out["warnings"] == [RECOVERY_WARNING]
    assert "runtime-secret" not in repr(out)
    assert provider.closed == 1


class _BoardExplodingSubtaskService(_ExplodingSubtaskService):
    def run(self, *args, **kwargs):
        raise BoardProviderError(
            "authentication",
            "state machine rejected runtime-secret",
            retryable=False,
        )


def test_explicit_board_error_from_state_machine_preserves_category():
    service, provider, _, _, _ = _service(
        subtask_service=_BoardExplodingSubtaskService()
    )

    out = _create(service)

    assert out["status"] == "error"
    assert out["category"] == "authentication"
    assert out["retryable"] is False
    assert "runtime-secret" not in repr(out)
    assert provider.closed == 1


def test_complete_checkpoint_cas_failure_recovers_board_complete_and_retry_finishes():
    service, provider, _, _, state_machine = _service()
    state_machine._store.fail_complete_checkpoint_once = True

    first = _create(service, board_type="fake")

    assert first["status"] == "partial"
    assert first["category"] == "reindex_pending"
    assert first["retryable"] is True
    assert state_machine._store.operation.status == "board_complete"
    assert "runtime-secret" not in repr(first)
    writes = provider.board_writes

    second = _create(service, board_type="fake")

    assert second["status"] == "ok"
    assert second["reindexed"] is True
    assert state_machine._store.operation.status == "complete"
    assert provider.board_writes == writes
