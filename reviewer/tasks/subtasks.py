"""Чистые контракты запроса на декомпозицию задачи и его идемпотентности."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

MAX_SUBTASKS = 20
SUBTASK_MARKER_RE = re.compile(r"reviewer-subtask:[0-9a-f]{64}(?![0-9A-Fa-f])")

SubtaskPhase = Literal["pending", "in_flight", "created", "attached"]
OperationStatus = Literal["running", "partial", "board_complete", "complete"]


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

    child_hash = hashlib.sha256(_canonical_json(draft.payload()).encode("utf-8")).hexdigest()
    marker_payload = "\0".join((*input_components, str(index), child_hash))
    return "reviewer-subtask:" + hashlib.sha256(marker_payload.encode("utf-8")).hexdigest()
