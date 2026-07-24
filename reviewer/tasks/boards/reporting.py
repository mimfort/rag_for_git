"""Fail-closed boundary для недоверенных board validation reports."""
from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from urllib.parse import urlsplit

from reviewer.tasks.boards.errors import sanitize_provider_text

_TOP_LEVEL_FIELDS = frozenset(
    {"status", "identity", "project", "capabilities", "warnings"}
)
_IDENTITY_FIELDS = frozenset({"id", "account_id", "login", "name", "display_name"})
_PROJECT_FIELDS = frozenset({"id", "key", "code", "name", "title"})
_CAPABILITY_FIELDS = frozenset(
    {"read", "sync", "create", "finish", "transition", "attachments"}
)
_CREDENTIAL_KEYS = frozenset(
    {
        "token",
        "refreshtoken",
        "apitoken",
        "apikey",
        "password",
        "secret",
        "authorization",
        "cookie",
        "credential",
    }
)
_CREDENTIAL_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+")
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"""
    (?<![a-z0-9])
    ["']?
    (?:
        (?:refresh|access)[_-]*token
        | api[_-]*(?:token|key)
        | token
        | password
        | secret
        | authorization
        | cookie
        | credential
    )
    ["']?
    \s*[:=]\s*
    (?:
        "(?:\\.|[^"\\])*"
        | '(?:\\.|[^'\\])*'
        | [^,;\r\n]+
    )
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)


def _is_credential_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = re.sub(r"[_\-\s]+", "", key).casefold()
    return (
        normalized in _CREDENTIAL_KEYS
        or normalized.endswith(("token", "apikey", "password", "secret", "credential"))
    )


def _drop_credential_keys(value: object, active: set[int] | None = None) -> object:
    """Рекурсивно удалить credential fields до schema projection."""
    if active is None:
        active = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            return {}
        active.add(identity)
        try:
            return {
                key: _drop_credential_keys(item, active)
                for key, item in value.items()
                if not _is_credential_key(key)
            }
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            return []
        active.add(identity)
        try:
            return [_drop_credential_keys(item, active) for item in value]
        finally:
            active.remove(identity)
    return value


def _sanitize_text(value: str, secrets: Collection[str]) -> str:
    rendered = sanitize_provider_text(value, secrets)
    for match in reversed(list(_CREDENTIAL_URL.finditer(rendered))):
        url = match.group(0)
        try:
            parsed = urlsplit(url)
            unsafe = parsed.username is not None or parsed.password is not None
        except (TypeError, ValueError):
            unsafe = True
        if unsafe:
            rendered = rendered[:match.start()] + "[REDACTED]" + rendered[match.end():]
    return _CREDENTIAL_ASSIGNMENT.sub("[REDACTED]", rendered)


def _safe_scalar(value: object, secrets: Collection[str]) -> object:
    if value is None:
        return None
    if isinstance(value, str):
        return _sanitize_text(value, secrets)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return _MISSING


def _safe_fields(
    value: object,
    allowed_fields: frozenset[str],
    secrets: Collection[str],
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, object] = {}
    for key in allowed_fields:
        if key not in value or _is_credential_key(key):
            continue
        item = _safe_scalar(value[key], secrets)
        if item is not _MISSING:
            safe[key] = item
    return safe


_MISSING = object()


def sanitize_validation_report(
    report: object,
    secrets: Collection[str] = (),
) -> dict[str, object]:
    """Спроецировать provider report на безопасную стабильную схему."""
    try:
        stripped = _drop_credential_keys(report)
    except Exception:
        return {}
    if not isinstance(stripped, Mapping):
        return {}

    safe: dict[str, object] = {}
    if "status" in stripped:
        status = _safe_scalar(stripped["status"], secrets)
        if status is not _MISSING:
            safe["status"] = status

    if "identity" in stripped:
        identity = _safe_fields(stripped["identity"], _IDENTITY_FIELDS, secrets)
        if identity:
            safe["identity"] = identity

    if "project" in stripped:
        project = stripped["project"]
        if isinstance(project, Mapping):
            safe_project = _safe_fields(project, _PROJECT_FIELDS, secrets)
            if safe_project:
                safe["project"] = safe_project
        else:
            safe_project = _safe_scalar(project, secrets)
            if safe_project is not _MISSING:
                safe["project"] = safe_project

    if "capabilities" in stripped:
        capabilities = stripped["capabilities"]
        safe_capabilities: dict[str, bool] = {}
        if isinstance(capabilities, Mapping):
            safe_capabilities = {
                key: value
                for key, value in capabilities.items()
                if (
                    key in _CAPABILITY_FIELDS
                    and not _is_credential_key(key)
                    and isinstance(value, bool)
                )
            }
        elif isinstance(capabilities, (list, tuple)):
            safe_capabilities = {
                item: True
                for item in capabilities
                if isinstance(item, str) and item in _CAPABILITY_FIELDS
            }
        safe["capabilities"] = safe_capabilities

    if "warnings" in stripped and isinstance(stripped["warnings"], (list, tuple)):
        safe["warnings"] = [
            _sanitize_text(warning, secrets)
            for warning in stripped["warnings"]
            if isinstance(warning, str)
        ]

    return {key: safe[key] for key in _TOP_LEVEL_FIELDS if key in safe}
