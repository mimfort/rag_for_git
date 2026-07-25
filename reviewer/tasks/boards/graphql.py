"""Тонкий GraphQL-фасад поверх безопасного board HTTP client."""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping
from typing import Any, Literal

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.http import BoardHttpClient

_CODE_CATEGORY = {
    "AUTHENTICATION_ERROR": "authentication",
    "UNAUTHENTICATED": "authentication",
    "FORBIDDEN": "permission",
    "FEATURE_NOT_ACCESSIBLE": "permission",
    "RATELIMITED": "rate_limit",
    "RATE_LIMITED": "rate_limit",
    "NOT_FOUND": "not_found",
    "ENTITY_NOT_FOUND": "not_found",
}


class GraphQLClient:
    """POST одного endpoint'а: транспортные ретраи берёт из BoardHttpClient,
    прикладные ошибки GraphQL переводит в BoardProviderError."""

    def __init__(
        self,
        http: BoardHttpClient,
        *,
        path: str = "/graphql",
        secrets: Collection[str] = (),
    ) -> None:
        self._http = http
        self._path = path
        self._secrets = frozenset(value for value in secrets if value)

    def execute(
        self,
        query: str,
        variables: Mapping[str, Any] | None = None,
        *,
        operation: Literal["read", "write"] = "read",
    ) -> dict:
        payload = self._http.request_json(
            "POST",
            self._path,
            operation=operation,
            json={"query": query, "variables": dict(variables or {})},
        )
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            raise self._error_for(errors)
        data = payload.get("data") if isinstance(payload, dict) else None
        return data or {}

    def paginate(
        self,
        query: str,
        variables: Mapping[str, Any],
        *,
        connection: Callable[[dict], dict],
        operation: Literal["read", "write"] = "read",
        max_pages: int = 1000,
    ) -> Iterator[dict]:
        """Обход connection по pageInfo.endCursor; переменная курсора — `after`."""
        cursor: str | None = None
        for _ in range(max_pages):
            data = self.execute(query, {**dict(variables), "after": cursor}, operation=operation)
            block = connection(data) or {}
            yield from block.get("nodes") or []
            info = block.get("pageInfo") or {}
            cursor = info.get("endCursor") if info.get("hasNextPage") else None
            if not cursor:
                return
        raise BoardProviderError(
            "unsupported",
            "Board GraphQL pagination exceeded the maximum page count.",
            secrets=self._secrets,
        )

    def _error_for(self, errors: Any) -> BoardProviderError:
        code = ""
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            extensions = errors[0].get("extensions") or {}
            code = str(extensions.get("code") or "")
        category = _CODE_CATEGORY.get(code, "unsupported")
        return BoardProviderError(
            category,
            "Board GraphQL request returned errors.",
            hint="Check the board GraphQL query, credentials and permissions.",
            retryable=category == "rate_limit",
            secrets=self._secrets,
        )
