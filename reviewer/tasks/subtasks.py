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
) -> SubtaskBatchResult:
    raw_items = operation.state.get("items", [])
    if not isinstance(raw_items, list):
        raise LedgerUnavailableError("Ledger содержит некорректный список подзадач")

    created: list[SubtaskChildResult] = []
    attached: list[SubtaskChildResult] = []
    unattached: list[SubtaskChildResult] = []
    pending: list[SubtaskChildResult] = []
    warnings: list[str] = []
    for raw_item in raw_items:
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


def _validated_items(
    operation: SubtaskOperation,
    request: SubtaskRequest,
) -> list[dict[str, Any]]:
    raw_items = operation.state.get("items")
    if not isinstance(raw_items, list):
        raise LedgerUnavailableError("Ledger содержит некорректный список подзадач")
    if len(raw_items) != len(request.subtasks):
        raise LedgerUnavailableError("Ledger содержит неверное число подзадач")

    items: list[dict[str, Any]] = []
    for index, (item, draft) in enumerate(zip(raw_items, request.subtasks)):
        if not isinstance(item, dict):
            raise LedgerUnavailableError("Ledger содержит некорректную подзадачу")
        if item.get("input_hash") != _draft_input_hash(draft):
            raise LedgerUnavailableError("Ledger содержит некорректный input_hash")
        expected_marker = marker_for(
            operation.board_type,
            operation.parent_task_id,
            operation.idempotency_key,
            index,
            draft,
        )
        if item.get("index") != index or item.get("marker") != expected_marker:
            raise LedgerUnavailableError("Ledger содержит некорректный marker")
        _persisted_phase(item.get("phase"))
        items.append(item)
    return items


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
    identity: NativeSubtaskIdentity,
    sanitize: Callable[[object], str],
) -> dict[str, object]:
    return {
        "phase": "created",
        "title": identity.title,
        "key": identity.key,
        "aliases": list(identity.aliases),
        "board_id": identity.board_id,
        "url": identity.url,
        "manual_required": False,
        "warnings": [_safe_warning(sanitize, warning) for warning in identity.warnings],
    }


class SubtaskService:
    def __init__(self, store: SubtaskOperationStore) -> None:
        self._store = store

    def preflight(self, request: SubtaskRequest) -> SubtaskPreflight:
        operation = self._store.load(request.idempotency_key)
        if operation is None:
            return SubtaskPreflight(None, None)
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
            _validated_items(operation, request)
            return SubtaskPreflight(
                operation,
                _result_from_operation(operation, resumed=True),
            )
        return SubtaskPreflight(operation, None)

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
        resumed = operation is not None
        if operation is None:
            parent = provider.fetch_one(request.parent_key)  # type: ignore[attr-defined]
            if parent is None:
                return SubtaskBatchResult(
                    status="error",
                    board_type=board_type,
                    parent_key=request.parent_key,
                    idempotency_key=request.idempotency_key,
                    resumed=False,
                    category="parent_not_found",
                    retryable=False,
                )
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
                return SubtaskBatchResult(
                    status="error",
                    board_type=board_type,
                    parent_key=request.parent_key,
                    idempotency_key=request.idempotency_key,
                    resumed=False,
                    category="source_metadata_missing",
                    retryable=False,
                )
            fresh = _fresh_operation(
                request,
                board_type=board_type,
                parent_task_id=parent_task_id,
                source_board_id=source_board_id,
                source_column_id=source_column_id,
            )
        else:
            parent_task_id = operation.parent_task_id
            fresh = None

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
                _validated_items(current, request)
                return _result_from_operation(current, resumed=True)
            if current.parent_task_id != parent_task_id or current.board_type != board_type:
                raise LedgerUnavailableError("Операция загружена под другой parent lock")

            raw_items = _validated_items(current, request)
            in_flight: list[tuple[int, str]] = []
            for index, item in enumerate(raw_items):
                if _persisted_phase(item.get("phase")) == "in_flight":
                    in_flight.append((index, item["marker"]))

            if in_flight:
                markers = frozenset(marker for _, marker in in_flight)
                grouped: dict[str, list[NativeSubtaskIdentity]] = {
                    marker: [] for marker in markers
                }
                for match in provider.reconcile_native_subtasks(
                    current.source_board_id,
                    markers,
                ):
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

            for index, draft in enumerate(request.subtasks):
                item = current.state["items"][index]
                if _persisted_phase(item.get("phase")) != "pending":
                    continue
                in_flight = _operation_with_item(current, index, phase="in_flight")
                current = self._store.checkpoint(
                    in_flight,
                    expected_revision=current.revision,
                )
                lock.ensure_alive()
                try:
                    identity = provider.create_native_subtask(
                        render_markdown(
                            TaskDoc(
                                title=draft.title,
                                problem=draft.problem,
                                steps=list(draft.steps),
                                criteria=list(draft.criteria),
                                context=draft.context,
                            )
                        ),
                        title=draft.title,
                        source_column_id=current.source_column_id,
                        marker=item["marker"],
                    )
                    identity_changes = _identity_changes(identity, sanitize)
                except Exception as error:  # noqa: BLE001 - provider boundary
                    failed = _operation_with_item(
                        current,
                        index,
                        phase="in_flight",
                        manual_required=True,
                        warnings=[_safe_warning(sanitize, error)],
                    )
                    current = self._store.checkpoint(
                        failed,
                        expected_revision=current.revision,
                    )
                    continue
                created = _operation_with_item(
                    current,
                    index,
                    **identity_changes,
                )
                current = self._store.checkpoint(
                    created,
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
