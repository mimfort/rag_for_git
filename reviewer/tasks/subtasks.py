"""Чистые контракты запроса на декомпозицию задачи и его идемпотентности."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, cast

from reviewer.tasks.boards.base import NativeSubtaskIdentity, NativeSubtaskProvider, RawTask
from reviewer.tasks.subtask_store import (
    LedgerUnavailableError,
    SubtaskOperation,
    SubtaskOperationStore,
)
from reviewer.tasks.taskdoc import TaskDoc, render_markdown

MAX_SUBTASKS = 20
SUBTASK_MARKER_RE = re.compile(r"reviewer-subtask:[0-9a-f]{64}(?![0-9A-Fa-f])")

SubtaskPhase = Literal["pending", "in_flight", "created", "attached"]
OperationStatus = Literal["running", "partial", "board_complete", "complete"]


def merge_subtask_ids(existing: list[str], created: list[str]) -> list[str]:
    """Merge board UUIDs in first-seen order, omitting empty values and duplicates."""
    merged: list[str] = []
    seen: set[str] = set()
    for subtask_id in (*existing, *created):
        if subtask_id and subtask_id not in seen:
            seen.add(subtask_id)
            merged.append(subtask_id)
    return merged


@dataclass(frozen=True)
class SubtaskChildResult:
    index: int
    title: str
    key: str | None
    aliases: tuple[str, ...]
    board_id: str | None
    url: str | None
    phase: SubtaskPhase
    manual_required: bool = False


@dataclass(frozen=True)
class WriteThroughResult:
    success: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubtaskBatchResult:
    status: Literal["ok", "partial", "error"]
    board_type: str
    parent_key: str
    idempotency_key: str
    resumed: bool
    created: tuple[SubtaskChildResult, ...] = ()
    attached: tuple[SubtaskChildResult, ...] = ()
    unattached: tuple[SubtaskChildResult, ...] = ()
    pending: tuple[SubtaskChildResult, ...] = ()
    warnings: tuple[str, ...] = ()
    reindexed: bool = False
    category: str | None = None
    retryable: bool | None = None

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SubtaskPreflight:
    operation: SubtaskOperation | None
    result: SubtaskBatchResult | None


@dataclass(frozen=True)
class _ValidatedOperation:
    drafts: tuple[SubtaskDraft, ...]
    items: tuple[dict[str, Any], ...]


def _persisted_phase(value: object) -> SubtaskPhase:
    if value not in ("pending", "in_flight", "created", "attached"):
        raise LedgerUnavailableError("Ledger содержит неизвестную фазу подзадачи")
    return cast(SubtaskPhase, value)


def _persisted_warnings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise LedgerUnavailableError("Ledger содержит некорректные предупреждения")
    return tuple(value)


def _child_from_state(value: object) -> tuple[SubtaskChildResult, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise LedgerUnavailableError("Ledger содержит некорректное состояние подзадачи")
    index = value.get("index")
    title = value.get("title")
    if not isinstance(index, int) or not isinstance(title, str):
        raise LedgerUnavailableError("Ledger содержит неполную подзадачу")
    aliases = value.get("aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
        raise LedgerUnavailableError("Ledger содержит некорректные aliases подзадачи")
    optional_text = (value.get("key"), value.get("board_id"), value.get("url"))
    if any(item is not None and not isinstance(item, str) for item in optional_text):
        raise LedgerUnavailableError("Ledger содержит некорректную identity подзадачи")
    manual_required = value.get("manual_required", False)
    if not isinstance(manual_required, bool):
        raise LedgerUnavailableError("Ledger содержит некорректный manual_required")
    warnings = _persisted_warnings(value.get("warnings"))
    return (
        SubtaskChildResult(
            index=index,
            title=title,
            key=value.get("key"),
            aliases=tuple(aliases),
            board_id=value.get("board_id"),
            url=value.get("url"),
            phase=_persisted_phase(value.get("phase")),
            manual_required=manual_required,
        ),
        warnings,
    )


def _result_from_operation(
    operation: SubtaskOperation,
    *,
    resumed: bool,
    validated: _ValidatedOperation | None = None,
) -> SubtaskBatchResult:
    validated = validated or _validate_persisted_operation(operation)

    created: list[SubtaskChildResult] = []
    attached: list[SubtaskChildResult] = []
    unattached: list[SubtaskChildResult] = []
    pending: list[SubtaskChildResult] = []
    warnings: list[str] = []
    for raw_item in validated.items:
        child, child_warnings = _child_from_state(raw_item)
        warnings.extend(child_warnings)
        if child.phase == "created":
            created.append(child)
            unattached.append(child)
        elif child.phase == "attached":
            created.append(child)
            attached.append(child)
        else:
            pending.append(child)
    warnings.extend(_persisted_warnings(operation.state.get("warnings")))

    if operation.status == "complete" and not pending and not unattached:
        status: Literal["ok", "partial", "error"] = "ok"
        category = None
        retryable = None
    elif operation.status == "board_complete":
        status = "partial"
        category = "reindex_pending"
        retryable = True
    else:
        status = "partial"
        category = (
            "manual_required"
            if any(item.manual_required for item in pending)
            else "incomplete"
        )
        retryable = True

    return SubtaskBatchResult(
        status=status,
        board_type=operation.board_type,
        parent_key=operation.parent_input_key,
        idempotency_key=operation.idempotency_key,
        resumed=resumed,
        created=tuple(created),
        attached=tuple(attached),
        unattached=tuple(unattached),
        pending=tuple(pending),
        warnings=tuple(warnings),
        reindexed=bool(operation.state.get("reindexed", False)),
        category=category,
        retryable=retryable,
    )


def _request_payload(request: SubtaskRequest) -> dict[str, Any]:
    return {
        "parent_key": request.parent_key,
        "subtasks": [draft.payload() for draft in request.subtasks],
        "idempotency_key": request.idempotency_key,
        "board_type": request.board_type,
        "project": request.project,
        "provider_options": deepcopy(request.provider_options),
    }


def _draft_input_hash(draft: SubtaskDraft) -> str:
    return hashlib.sha256(_canonical_json(draft.payload()).encode("utf-8")).hexdigest()


def _persisted_draft(value: object) -> SubtaskDraft:
    if not isinstance(value, dict):
        raise LedgerUnavailableError("Ledger содержит некорректный draft подзадачи")
    title = value.get("title")
    problem = value.get("problem")
    steps = value.get("steps")
    criteria = value.get("criteria")
    context = value.get("context")
    if not isinstance(title, str) or not title or title != title.strip():
        raise LedgerUnavailableError("Ledger содержит некорректный title draft")
    if not isinstance(problem, str) or not problem or problem != problem.strip():
        raise LedgerUnavailableError("Ledger содержит некорректный problem draft")
    if (
        not isinstance(steps, list)
        or not steps
        or not all(isinstance(item, str) and item and item == item.strip() for item in steps)
    ):
        raise LedgerUnavailableError("Ledger содержит некорректные steps draft")
    if (
        not isinstance(criteria, list)
        or not criteria
        or not all(
            isinstance(item, str) and item and item == item.strip() for item in criteria
        )
    ):
        raise LedgerUnavailableError("Ledger содержит некорректные criteria draft")
    if context is not None and (
        not isinstance(context, str) or not context or context != context.strip()
    ):
        raise LedgerUnavailableError("Ledger содержит некорректный context draft")
    return SubtaskDraft(
        title=title,
        problem=problem,
        steps=tuple(steps),
        criteria=tuple(criteria),
        context=context,
    )


def _usable_identity_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_persisted_operation(operation: SubtaskOperation) -> _ValidatedOperation:
    state = operation.state
    payload = operation.request_payload
    if not isinstance(state, dict) or not isinstance(payload, dict):
        raise LedgerUnavailableError("Ledger operation state/request_payload должен быть object")

    revision = state.get("revision")
    if type(revision) is not int or revision < 0:
        raise LedgerUnavailableError("Ledger содержит некорректную revision")
    raw_items = state.get("items")
    raw_drafts = payload.get("subtasks")
    if not isinstance(raw_items, list) or not isinstance(raw_drafts, list):
        raise LedgerUnavailableError("Ledger содержит некорректные items/subtasks")
    if not 1 <= len(raw_drafts) <= MAX_SUBTASKS or len(raw_items) != len(raw_drafts):
        raise LedgerUnavailableError("Ledger содержит неверное число подзадач")

    if payload.get("parent_key") != operation.parent_input_key:
        raise LedgerUnavailableError("Ledger содержит другой parent_key")
    if payload.get("idempotency_key") != operation.idempotency_key:
        raise LedgerUnavailableError("Ledger содержит другой idempotency_key")
    if not isinstance(payload.get("provider_options"), dict):
        raise LedgerUnavailableError("Ledger содержит некорректные provider_options")
    if payload.get("board_type") is not None and not isinstance(
        payload.get("board_type"), str
    ):
        raise LedgerUnavailableError("Ledger содержит некорректный board_type payload")
    if payload.get("board_type") is not None and payload.get("board_type") != operation.board_type:
        raise LedgerUnavailableError("Ledger содержит другой board_type")
    if payload.get("project") is not None and not isinstance(payload.get("project"), str):
        raise LedgerUnavailableError("Ledger содержит некорректный project payload")
    if not all(
        _usable_identity_text(value)
        for value in (
            operation.idempotency_key,
            operation.board_type,
            operation.parent_input_key,
            operation.parent_task_id,
            operation.source_board_id,
            operation.source_column_id,
            operation.request_hash,
        )
    ):
        raise LedgerUnavailableError("Ledger содержит неполную identity операции")
    try:
        expected_request_hash = hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError) as error:
        raise LedgerUnavailableError("Ledger содержит неканонический request_payload") from error
    if operation.request_hash != expected_request_hash:
        raise LedgerUnavailableError("Ledger request_hash не соответствует request_payload")

    state_warnings = state.get("warnings", [])
    if not isinstance(state_warnings, list) or not all(
        isinstance(item, str) for item in state_warnings
    ):
        raise LedgerUnavailableError("Ledger содержит некорректные warnings операции")
    if "reindexed" in state and not isinstance(state["reindexed"], bool):
        raise LedgerUnavailableError("Ledger содержит некорректный reindexed")

    drafts = tuple(_persisted_draft(value) for value in raw_drafts)
    items: list[dict[str, Any]] = []
    phases: list[SubtaskPhase] = []
    for index, (item, draft) in enumerate(zip(raw_items, drafts)):
        if not isinstance(item, dict):
            raise LedgerUnavailableError("Ledger содержит некорректную подзадачу")
        if type(item.get("index")) is not int or item.get("index") != index:
            raise LedgerUnavailableError("Ledger содержит некорректные индексы подзадач")
        if item.get("title") != draft.title:
            raise LedgerUnavailableError("Ledger содержит некорректный title подзадачи")
        if item.get("input_hash") != _draft_input_hash(draft):
            raise LedgerUnavailableError("Ledger содержит некорректный input_hash")
        expected_marker = marker_for(
            operation.board_type,
            operation.parent_task_id,
            operation.idempotency_key,
            index,
            draft,
        )
        if item.get("marker") != expected_marker:
            raise LedgerUnavailableError("Ledger содержит некорректный marker")

        phase = _persisted_phase(item.get("phase"))
        manual_required = item.get("manual_required")
        warnings = item.get("warnings")
        aliases = item.get("aliases")
        url = item.get("url")
        key = item.get("key")
        board_id = item.get("board_id")
        if not isinstance(manual_required, bool):
            raise LedgerUnavailableError("Ledger содержит некорректный manual_required")
        if not isinstance(warnings, list) or not all(
            isinstance(warning, str) for warning in warnings
        ):
            raise LedgerUnavailableError("Ledger содержит некорректные warnings подзадачи")
        if not isinstance(aliases, list) or not all(
            _usable_identity_text(alias) for alias in aliases
        ):
            raise LedgerUnavailableError("Ledger содержит некорректные aliases подзадачи")
        if url is not None and not _usable_identity_text(url):
            raise LedgerUnavailableError("Ledger содержит некорректный url подзадачи")

        if phase == "pending":
            if (
                key is not None
                or board_id is not None
                or aliases
                or url is not None
                or manual_required
                or warnings
            ):
                raise LedgerUnavailableError("Pending подзадача содержит side-effect state")
        elif phase in ("created", "attached"):
            if not _usable_identity_text(key) or not _usable_identity_text(board_id):
                raise LedgerUnavailableError("Созданная подзадача не содержит identity")
            if manual_required:
                raise LedgerUnavailableError("Confirmed подзадача требует manual review")
        elif key is not None or board_id is not None or aliases or url is not None:
            raise LedgerUnavailableError("In-flight подзадача содержит identity")

        phases.append(phase)
        items.append(item)

    if operation.status not in ("running", "partial", "board_complete", "complete"):
        raise LedgerUnavailableError("Ledger содержит неизвестный operation status")
    if operation.status == "running" and any(phase != "pending" for phase in phases):
        raise LedgerUnavailableError("Running operation содержит side-effect state")
    if operation.status == "partial" and all(phase == "pending" for phase in phases):
        raise LedgerUnavailableError("Partial operation не содержит progress")
    if operation.status in ("board_complete", "complete") and any(
        phase != "attached" for phase in phases
    ):
        raise LedgerUnavailableError("Terminal operation содержит unattached подзадачу")

    return _ValidatedOperation(drafts=drafts, items=tuple(items))


def _safe_warning(sanitize: Callable[[object], str], value: object) -> str:
    warning = sanitize(value)
    if not isinstance(warning, str):
        raise LedgerUnavailableError("Sanitizer не вернул строку")
    return warning


def _operation_status(items: list[dict[str, Any]]) -> OperationStatus:
    phases = {item.get("phase") for item in items}
    if phases & {"in_flight", "created", "attached"}:
        return "partial"
    return "running"


def _operation_with_item(
    operation: SubtaskOperation,
    item_index: int,
    **changes: object,
) -> SubtaskOperation:
    state = deepcopy(operation.state)
    raw_items = state.get("items")
    if not isinstance(raw_items, list) or not 0 <= item_index < len(raw_items):
        raise LedgerUnavailableError("Ledger не содержит ожидаемую подзадачу")
    raw_item = raw_items[item_index]
    if not isinstance(raw_item, dict):
        raise LedgerUnavailableError("Ledger содержит некорректную подзадачу")
    raw_items[item_index] = {**raw_item, **deepcopy(changes)}
    return replace(operation, state=state, status=_operation_status(raw_items))


def _operation_with_state(
    operation: SubtaskOperation,
    *,
    status: OperationStatus | None = None,
    warnings: tuple[str, ...] = (),
    reindexed: bool | None = None,
) -> SubtaskOperation:
    state = deepcopy(operation.state)
    persisted_warnings = state.setdefault("warnings", [])
    if not isinstance(persisted_warnings, list):
        raise LedgerUnavailableError("Ledger содержит некорректные warnings операции")
    for warning in warnings:
        if warning not in persisted_warnings:
            persisted_warnings.append(warning)
    if reindexed is not None:
        state["reindexed"] = reindexed
    return replace(operation, state=state, status=status or operation.status)


def _confirmed_identities(
    operation: SubtaskOperation,
) -> tuple[NativeSubtaskIdentity, ...]:
    identities: list[NativeSubtaskIdentity] = []
    raw_items = operation.state.get("items")
    if not isinstance(raw_items, list):
        raise LedgerUnavailableError("Ledger содержит некорректные items")
    for raw_item in raw_items:
        child, warnings = _child_from_state(raw_item)
        if child.phase not in ("created", "attached"):
            continue
        if child.board_id is None or child.key is None:
            raise LedgerUnavailableError("Созданная подзадача не содержит identity")
        identities.append(
            NativeSubtaskIdentity(
                board_id=child.board_id,
                key=child.key,
                title=child.title,
                aliases=child.aliases,
                url=child.url,
                warnings=warnings,
            )
        )
    return tuple(identities)


def _fresh_operation(
    request: SubtaskRequest,
    *,
    board_type: str,
    parent_task_id: str,
    source_board_id: str,
    source_column_id: str,
) -> SubtaskOperation:
    items = []
    for index, draft in enumerate(request.subtasks):
        items.append(
            {
                "index": index,
                "title": draft.title,
                "input_hash": _draft_input_hash(draft),
                "marker": marker_for(
                    board_type,
                    parent_task_id,
                    request.idempotency_key,
                    index,
                    draft,
                ),
                "phase": "pending",
                "key": None,
                "aliases": [],
                "board_id": None,
                "url": None,
                "manual_required": False,
                "warnings": [],
            }
        )
    return SubtaskOperation(
        idempotency_key=request.idempotency_key,
        board_type=board_type,
        parent_input_key=request.parent_key,
        parent_task_id=parent_task_id,
        source_board_id=source_board_id,
        source_column_id=source_column_id,
        request_hash=request.request_hash,
        request_payload=_request_payload(request),
        state={"revision": 0, "items": items},
        status="running",
    )


def _identity_changes(
    identity: object,
    sanitize: Callable[[object], str],
) -> dict[str, object]:
    if not isinstance(identity, NativeSubtaskIdentity):
        raise LedgerUnavailableError("Provider вернул некорректную identity")
    if (
        not _usable_identity_text(identity.board_id)
        or not _usable_identity_text(identity.key)
        or not _usable_identity_text(identity.title)
        or not isinstance(identity.aliases, tuple)
        or not all(_usable_identity_text(alias) for alias in identity.aliases)
        or (identity.url is not None and not _usable_identity_text(identity.url))
        or not isinstance(identity.warnings, tuple)
        or not all(isinstance(warning, str) for warning in identity.warnings)
    ):
        raise LedgerUnavailableError("Provider вернул неполную identity")
    return {
        "phase": "created",
        "key": identity.key,
        "aliases": list(identity.aliases),
        "board_id": identity.board_id,
        "url": identity.url,
        "manual_required": False,
        "warnings": [_safe_warning(sanitize, warning) for warning in identity.warnings],
    }


def _render_child_doc(draft: SubtaskDraft) -> str:
    doc_md = render_markdown(
        TaskDoc(
            title=draft.title,
            problem=draft.problem,
            steps=list(draft.steps),
            criteria=list(draft.criteria),
            context=draft.context,
        )
    )
    if not isinstance(doc_md, str) or not doc_md.strip():
        raise LedgerUnavailableError("Canonical child document пуст или некорректен")
    return doc_md


def _evaluate_loaded_operation(
    operation: SubtaskOperation,
    request: SubtaskRequest,
) -> SubtaskPreflight:
    validated = _validate_persisted_operation(operation)
    if operation.request_hash != request.request_hash:
        return SubtaskPreflight(
            operation,
            SubtaskBatchResult(
                status="error",
                board_type=operation.board_type,
                parent_key=request.parent_key,
                idempotency_key=request.idempotency_key,
                resumed=True,
                category="conflict",
                retryable=False,
            ),
        )
    if operation.status == "complete":
        return SubtaskPreflight(
            operation,
            _result_from_operation(
                operation,
                resumed=True,
                validated=validated,
            ),
        )
    return SubtaskPreflight(operation, None)


class SubtaskService:
    def __init__(self, store: SubtaskOperationStore) -> None:
        self._store = store

    def preflight(self, request: SubtaskRequest) -> SubtaskPreflight:
        operation = self._store.load(request.idempotency_key)
        if operation is None:
            return SubtaskPreflight(None, None)
        return _evaluate_loaded_operation(operation, request)

    def run(
        self,
        request: SubtaskRequest,
        *,
        operation: SubtaskOperation | None,
        provider: NativeSubtaskProvider,
        board_type: str,
        write_through: Callable[
            [RawTask, tuple[NativeSubtaskIdentity, ...]], WriteThroughResult
        ],
        sanitize: Callable[[object], str],
    ) -> SubtaskBatchResult:
        supplied_operation = operation
        latest = self._store.load(request.idempotency_key)
        if latest is None:
            if supplied_operation is not None:
                raise LedgerUnavailableError("Ledger потерял операцию возобновления")
            operation = None
        else:
            initial = _evaluate_loaded_operation(latest, request)
            if initial.result is not None:
                return initial.result
            operation = initial.operation

        resumed = operation is not None
        fresh = None
        if operation is None:
            try:
                parent = provider.fetch_one(request.parent_key)  # type: ignore[attr-defined]
            except Exception as error:  # noqa: BLE001 - provider boundary
                return SubtaskBatchResult(
                    status="partial",
                    board_type=board_type,
                    parent_key=request.parent_key,
                    idempotency_key=request.idempotency_key,
                    resumed=False,
                    warnings=(_safe_warning(sanitize, error),),
                    category="incomplete",
                    retryable=True,
                )
            if parent is None:
                no_write_result = SubtaskBatchResult(
                    status="error",
                    board_type=board_type,
                    parent_key=request.parent_key,
                    idempotency_key=request.idempotency_key,
                    resumed=False,
                    category="parent_not_found",
                    retryable=False,
                )
            else:
                parent_task_id = parent.board_id
                provider_data = parent.provider_data
                source_board_id = (
                    provider_data.get("source_board_id")
                    if isinstance(provider_data, dict)
                    else None
                )
                source_column_id = (
                    provider_data.get("source_column_id")
                    if isinstance(provider_data, dict)
                    else None
                )
                if not all(
                    isinstance(value, str) and value.strip()
                    for value in (parent_task_id, source_board_id, source_column_id)
                ):
                    no_write_result = SubtaskBatchResult(
                        status="error",
                        board_type=board_type,
                        parent_key=request.parent_key,
                        idempotency_key=request.idempotency_key,
                        resumed=False,
                        category="source_metadata_missing",
                        retryable=False,
                    )
                else:
                    no_write_result = None

            if no_write_result is not None:
                late = self._store.load(request.idempotency_key)
                if late is None:
                    return no_write_result
                late_preflight = _evaluate_loaded_operation(late, request)
                if late_preflight.result is not None:
                    return late_preflight.result
                operation = late_preflight.operation
                resumed = True
            else:
                fresh = _fresh_operation(
                    request,
                    board_type=board_type,
                    parent_task_id=parent_task_id,
                    source_board_id=source_board_id,
                    source_column_id=source_column_id,
                )

        if operation is not None:
            parent_task_id = operation.parent_task_id

        with self._store.try_parent_lock(board_type, parent_task_id) as lock:
            if lock is None:
                return SubtaskBatchResult(
                    status="error",
                    board_type=board_type,
                    parent_key=request.parent_key,
                    idempotency_key=request.idempotency_key,
                    resumed=resumed,
                    category="in_progress",
                    retryable=True,
                )

            current = self._store.load(request.idempotency_key)
            if current is None:
                if fresh is None:
                    raise LedgerUnavailableError("Ledger потерял операцию возобновления")
                try:
                    current = self._store.insert(fresh)
                except Exception:
                    current = self._store.load(request.idempotency_key)
                    if current is None:
                        raise
                    resumed = True
            else:
                resumed = True
            validated = _validate_persisted_operation(current)
            if current.request_hash != request.request_hash:
                return SubtaskBatchResult(
                    status="error",
                    board_type=current.board_type,
                    parent_key=request.parent_key,
                    idempotency_key=request.idempotency_key,
                    resumed=True,
                    category="conflict",
                    retryable=False,
                )
            if current.status == "complete":
                return _result_from_operation(
                    current,
                    resumed=True,
                    validated=validated,
                )
            if current.parent_task_id != parent_task_id or current.board_type != board_type:
                raise LedgerUnavailableError("Операция загружена под другой parent lock")

            parent_after_attachment: RawTask | None = None
            if current.status != "board_complete":
                raw_items = validated.items
                in_flight: list[tuple[int, str]] = []
                for index, item in enumerate(raw_items):
                    if _persisted_phase(item.get("phase")) == "in_flight":
                        in_flight.append((index, item["marker"]))

                if in_flight:
                    markers = frozenset(marker for _, marker in in_flight)
                    grouped: dict[str, list[NativeSubtaskIdentity]] = {
                        marker: [] for marker in markers
                    }
                    try:
                        reconciled = provider.reconcile_native_subtasks(
                            current.source_board_id,
                            markers,
                        )
                    except Exception as error:  # noqa: BLE001 - provider boundary
                        warning = _safe_warning(sanitize, error)
                        failed = _operation_with_state(current, warnings=(warning,))
                        current = self._store.checkpoint(
                            failed,
                            expected_revision=current.revision,
                        )
                        return _result_from_operation(current, resumed=resumed)
                    for match in reconciled:
                        if match.marker in grouped:
                            grouped[match.marker].append(match.identity)

                    for index, marker in in_flight:
                        identities = grouped[marker]
                        if len(identities) == 1:
                            candidate = _operation_with_item(
                                current,
                                index,
                                **_identity_changes(identities[0], sanitize),
                            )
                        else:
                            warning = (
                                "multiple board cards contain the same idempotency marker"
                                if identities
                                else "board card with idempotency marker was not found; "
                                "manual verification required"
                            )
                            candidate = _operation_with_item(
                                current,
                                index,
                                manual_required=True,
                                warnings=[_safe_warning(sanitize, warning)],
                            )
                        current = self._store.checkpoint(
                            candidate,
                            expected_revision=current.revision,
                        )

                for index, draft in enumerate(validated.drafts):
                    item = current.state["items"][index]
                    if _persisted_phase(item.get("phase")) != "pending":
                        continue
                    doc_md = _render_child_doc(draft)
                    in_flight_item = _operation_with_item(current, index, phase="in_flight")
                    current = self._store.checkpoint(
                        in_flight_item,
                        expected_revision=current.revision,
                    )
                    lock.ensure_alive()
                    try:
                        identity = provider.create_native_subtask(
                            doc_md,
                            title=draft.title,
                            source_column_id=current.source_column_id,
                            marker=item["marker"],
                        )
                    except Exception as error:  # noqa: BLE001 - provider boundary
                        warning = _safe_warning(sanitize, error)
                        failed = _operation_with_item(
                            current,
                            index,
                            phase="in_flight",
                            manual_required=True,
                            warnings=[warning],
                        )
                        current = self._store.checkpoint(
                            failed,
                            expected_revision=current.revision,
                        )
                        continue
                    identity_changes = _identity_changes(identity, sanitize)
                    created = _operation_with_item(
                        current,
                        index,
                        **identity_changes,
                    )
                    current = self._store.checkpoint(
                        created,
                        expected_revision=current.revision,
                    )

                confirmed = _confirmed_identities(current)
                if confirmed:
                    try:
                        parent_before_attachment = provider.fetch_one(  # type: ignore[attr-defined]
                            current.parent_task_id
                        )
                    except Exception as error:  # noqa: BLE001 - provider boundary
                        warning = _safe_warning(sanitize, error)
                        failed = _operation_with_state(current, warnings=(warning,))
                        current = self._store.checkpoint(
                            failed,
                            expected_revision=current.revision,
                        )
                        return _result_from_operation(current, resumed=resumed)
                    if not isinstance(parent_before_attachment, RawTask):
                        warning = _safe_warning(sanitize, "parent board state unavailable")
                        failed = _operation_with_state(current, warnings=(warning,))
                        current = self._store.checkpoint(
                            failed,
                            expected_revision=current.revision,
                        )
                        return _result_from_operation(current, resumed=resumed)

                    confirmed_ids = [identity.board_id for identity in confirmed]
                    exact_union = merge_subtask_ids(
                        parent_before_attachment.subtask_ids,
                        confirmed_ids,
                    )
                    if parent_before_attachment.subtask_ids != exact_union:
                        lock.ensure_alive()
                        try:
                            provider.replace_native_subtasks(
                                current.parent_task_id,
                                exact_union,
                            )
                        except Exception as error:  # noqa: BLE001 - provider boundary
                            warning = _safe_warning(sanitize, error)
                            failed = _operation_with_state(current, warnings=(warning,))
                            current = self._store.checkpoint(
                                failed,
                                expected_revision=current.revision,
                            )
                            return _result_from_operation(current, resumed=resumed)
                        try:
                            parent_after_attachment = provider.fetch_one(  # type: ignore[attr-defined]
                                current.parent_task_id
                            )
                        except Exception as error:  # noqa: BLE001 - provider boundary
                            warning = _safe_warning(sanitize, error)
                            failed = _operation_with_state(current, warnings=(warning,))
                            current = self._store.checkpoint(
                                failed,
                                expected_revision=current.revision,
                            )
                            return _result_from_operation(current, resumed=resumed)
                        if not isinstance(parent_after_attachment, RawTask):
                            warning = _safe_warning(
                                sanitize,
                                "parent attachment verification unavailable",
                            )
                            failed = _operation_with_state(current, warnings=(warning,))
                            current = self._store.checkpoint(
                                failed,
                                expected_revision=current.revision,
                            )
                            return _result_from_operation(current, resumed=resumed)
                    else:
                        parent_after_attachment = parent_before_attachment

                    attached_ids = frozenset(parent_after_attachment.subtask_ids)
                    for index, item in enumerate(current.state["items"]):
                        phase = _persisted_phase(item.get("phase"))
                        if phase not in ("created", "attached"):
                            continue
                        desired_phase: SubtaskPhase = (
                            "attached" if item.get("board_id") in attached_ids else "created"
                        )
                        if phase == desired_phase:
                            continue
                        changed = _operation_with_item(
                            current,
                            index,
                            phase=desired_phase,
                        )
                        current = self._store.checkpoint(
                            changed,
                            expected_revision=current.revision,
                        )

                phases = {
                    _persisted_phase(item.get("phase"))
                    for item in current.state["items"]
                }
                if phases != {"attached"}:
                    return _result_from_operation(current, resumed=resumed)
                board_complete = _operation_with_state(
                    current,
                    status="board_complete",
                    reindexed=False,
                )
                current = self._store.checkpoint(
                    board_complete,
                    expected_revision=current.revision,
                )

            if parent_after_attachment is None:
                try:
                    parent_after_attachment = provider.fetch_one(  # type: ignore[attr-defined]
                        current.parent_task_id
                    )
                except Exception as error:  # noqa: BLE001 - provider boundary
                    warning = _safe_warning(sanitize, error)
                    failed = _operation_with_state(current, warnings=(warning,))
                    current = self._store.checkpoint(
                        failed,
                        expected_revision=current.revision,
                    )
                    return _result_from_operation(current, resumed=resumed)
                if not isinstance(parent_after_attachment, RawTask):
                    warning = _safe_warning(sanitize, "parent board state unavailable")
                    failed = _operation_with_state(current, warnings=(warning,))
                    current = self._store.checkpoint(
                        failed,
                        expected_revision=current.revision,
                    )
                    return _result_from_operation(current, resumed=resumed)

            confirmed = _confirmed_identities(current)
            try:
                write_result = write_through(parent_after_attachment, confirmed)
            except Exception as error:  # noqa: BLE001 - callback boundary
                warning = _safe_warning(sanitize, error)
                failed = _operation_with_state(current, warnings=(warning,), reindexed=False)
                current = self._store.checkpoint(
                    failed,
                    expected_revision=current.revision,
                )
                return _result_from_operation(current, resumed=resumed)
            if not isinstance(write_result, WriteThroughResult):
                raise LedgerUnavailableError("Write-through вернул некорректный результат")
            callback_warnings = tuple(
                _safe_warning(sanitize, warning) for warning in write_result.warnings
            )
            if not write_result.success:
                pending_reindex = _operation_with_state(
                    current,
                    warnings=callback_warnings,
                    reindexed=False,
                )
                current = self._store.checkpoint(
                    pending_reindex,
                    expected_revision=current.revision,
                )
                return _result_from_operation(current, resumed=resumed)

            complete = _operation_with_state(
                current,
                status="complete",
                warnings=callback_warnings,
                reindexed=True,
            )
            current = self._store.checkpoint(
                complete,
                expected_revision=current.revision,
            )
            return _result_from_operation(current, resumed=resumed)


@dataclass(frozen=True)
class SubtaskDraft:
    """Нормализованный черновик дочерней задачи."""

    title: str
    problem: str
    steps: tuple[str, ...]
    criteria: tuple[str, ...]
    context: str | None

    def payload(self) -> dict[str, Any]:
        """Представление для JSON и API доски."""
        return {
            "title": self.title,
            "problem": self.problem,
            "steps": list(self.steps),
            "criteria": list(self.criteria),
            "context": self.context,
        }


@dataclass(frozen=True)
class SubtaskRequest:
    """Проверенный запрос с хешем его полного канонического содержимого."""

    parent_key: str
    subtasks: tuple[SubtaskDraft, ...]
    idempotency_key: str
    board_type: str | None
    project: str | None
    provider_options: dict[str, Any]
    request_hash: str


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} должен быть непустой строкой")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} должен быть строкой или null")
    return value.strip() or None


def _reject_nul(value: str | None, field: str) -> None:
    if value is not None and "\0" in value:
        raise ValueError(f"{field} не должен содержать NUL")


def _required_items(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{field} должен быть списком строк")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{field} должен содержать только строки")
        if text := item.strip():
            cleaned.append(text)
    if not cleaned:
        raise ValueError(f"{field} должен содержать хотя бы один непустой элемент")
    return tuple(cleaned)


def _context(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("context должен быть строкой или null")
    return value.strip() or None


def validate_subtask_request(
    parent_key: object,
    subtasks: object,
    idempotency_key: object,
    board_type: str | None,
    project: str | None,
    provider_options: dict[str, Any] | None = None,
) -> SubtaskRequest:
    """Проверить и нормализовать запрос, затем вычислить его стабильный хеш."""
    normalized_parent = _required_text(parent_key, "parent_key")
    normalized_key = _required_text(idempotency_key, "idempotency_key")
    normalized_board_type = _optional_text(board_type, "board_type")
    normalized_project = _optional_text(project, "project")
    for field, value in (
        ("parent_key", normalized_parent),
        ("idempotency_key", normalized_key),
        ("board_type", normalized_board_type),
        ("project", normalized_project),
    ):
        _reject_nul(value, field)
    if not isinstance(subtasks, (list, tuple)) or not 1 <= len(subtasks) <= MAX_SUBTASKS:
        raise ValueError(f"subtasks должен содержать от 1 до {MAX_SUBTASKS} элементов")

    drafts: list[SubtaskDraft] = []
    for child in subtasks:
        if not isinstance(child, dict):
            raise TypeError("каждая дочерняя задача должна быть объектом")
        drafts.append(
            SubtaskDraft(
                title=_required_text(child.get("title"), "title"),
                problem=_required_text(child.get("problem"), "problem"),
                steps=_required_items(child.get("steps"), "steps"),
                criteria=_required_items(child.get("criteria"), "criteria"),
                context=_context(child.get("context")),
            )
        )

    if provider_options is not None and not isinstance(provider_options, dict):
        raise TypeError("provider_options должен быть объектом или null")
    options = json.loads(_canonical_json(provider_options or {}))
    hash_payload = {
        "parent_key": normalized_parent,
        "subtasks": [draft.payload() for draft in drafts],
        "idempotency_key": normalized_key,
        "board_type": normalized_board_type,
        "project": normalized_project,
        "provider_options": options,
    }
    request_hash = hashlib.sha256(_canonical_json(hash_payload).encode("utf-8")).hexdigest()
    return SubtaskRequest(
        parent_key=normalized_parent,
        subtasks=tuple(drafts),
        idempotency_key=normalized_key,
        board_type=normalized_board_type,
        project=normalized_project,
        provider_options=options,
        request_hash=request_hash,
    )


def marker_for(
    board_type: str,
    parent_task_id: str,
    idempotency_key: str,
    index: int,
    draft: SubtaskDraft,
) -> str:
    """Стабильный маркер одной дочерней задачи для поиска на доске."""
    input_components = (board_type, parent_task_id, idempotency_key)
    if any("\0" in component for component in input_components):
        raise ValueError("компоненты marker не должны содержать NUL")

    child_hash = _draft_input_hash(draft)
    marker_payload = "\0".join((*input_components, str(index), child_hash))
    return "reviewer-subtask:" + hashlib.sha256(marker_payload.encode("utf-8")).hexdigest()
