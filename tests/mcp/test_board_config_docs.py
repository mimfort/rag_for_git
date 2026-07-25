"""Публичная документация текущего board-config fallback."""

from reviewer.mcp.service import MCPReviewService


def test_board_config_docstring_uses_registry_credentials_not_legacy_env_hint():
    docstring = MCPReviewService.board_config.__doc__ or ""

    assert "TASK_BOARD_*" not in docstring
    assert "registry-declared provider credentials" in docstring
    assert "configured registry credentials" in docstring
    assert "common non-secret metadata" in docstring
