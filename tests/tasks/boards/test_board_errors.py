from __future__ import annotations

import logging

import pytest

from reviewer.tasks.boards.errors import (
    BoardProviderError,
    sanitize_provider_payload,
    sanitize_provider_text,
)


def test_sanitizer_redacts_longest_literal_first_in_urls_and_headers():
    rendered = sanitize_provider_text(
        "url?token=abc123 Authorization: Bearer abc", {"abc", "abc123"},
    )

    assert rendered == "url?token=[REDACTED] Authorization: Bearer [REDACTED]"


def test_board_error_never_exposes_secret(caplog):
    error = BoardProviderError(
        "authentication",
        "Jira rejected token top-secret",
        hint="rotate top-secret",
        retryable=False,
        secrets={"top-secret"},
    )

    logging.getLogger("reviewer.test").warning("%s", error)
    rendered = f"{error!s} {error!r} {caplog.text}"
    assert "top-secret" not in rendered
    assert "[REDACTED]" in rendered
    assert error.category == "authentication"
    assert error.hint == "rotate [REDACTED]"
    assert error.retryable is False


def test_provider_payload_sanitizer_redacts_nested_results_and_warnings():
    secret = "provider-secret"
    result = {
        "warnings": [
            f"request rejected token={secret}",
            {"detail": secret},
        ],
        "nested": {"items": [secret, "safe"]},
    }

    sanitized = sanitize_provider_payload(result, {secret})

    assert secret not in repr(sanitized)
    assert sanitized["warnings"][0] == "request rejected token=[REDACTED]"
    assert sanitized["nested"]["items"] == ["[REDACTED]", "safe"]


def test_board_error_rejects_unknown_category_and_does_not_keep_exception_chain():
    with pytest.raises(ValueError, match="category"):
        BoardProviderError("unknown", "message")

    try:
        raise RuntimeError("Authorization: Bearer top-secret")
    except RuntimeError:
        try:
            raise BoardProviderError("transient", "request failed", secrets={"top-secret"}) from None
        except BoardProviderError as error:
            assert error.__cause__ is None
            assert error.__suppress_context__ is True
