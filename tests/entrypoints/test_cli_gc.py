"""Unit-тест CLI-команды `reviewer gc` (без Postgres: патчим стор и GC)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from reviewer.entrypoints.cli import cli


def test_gc_prints_report():
    """Команда печатает, что удалила и что оставила живым."""
    report = {"purged": ["a/x pr:94"], "kept": 2, "sessions_deleted": 3}
    with patch("reviewer.entrypoints.cli.ChunkStore", MagicMock()), \
         patch("reviewer.entrypoints.cli.SessionStore", MagicMock()), \
         patch("reviewer.entrypoints.cli.purge_orphaned_overlays",
               return_value=report) as gc_fn:
        result = CliRunner().invoke(cli, ["gc"])

    assert result.exit_code == 0
    assert "a/x pr:94" in result.output
    assert "оставлено живых 2" in result.output
    assert "3" in result.output
    assert gc_fn.called
