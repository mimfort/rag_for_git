"""Тесты для reviewer/llm/verdicts.py."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from reviewer.llm.verdicts import VerdictLog
from reviewer.vcs.base import Finding


# ---------------------------------------------------------------------------
# фикстуры
# ---------------------------------------------------------------------------


def _make_finding(
    *,
    file: str = "src/foo.py",
    line: int | None = 42,
    category: str = "correctness",
    severity: str = "high",
    confidence: float = 0.9,
    message: str = "Тестовое сообщение",
    suggestion: str | None = None,
) -> Finding:
    return Finding(
        category=category,
        severity=severity,  # type: ignore[arg-type]
        file=file,
        line=line,
        side="RIGHT",
        message=message,
        suggestion=suggestion,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# тесты
# ---------------------------------------------------------------------------


def test_log_verdict_записывает_поля(tmp_path):
    """log_verdict создаёт файл с корректными полями."""
    log_path = str(tmp_path / "verdicts.jsonl")
    vlog = VerdictLog(log_path, pr=99)
    finding = _make_finding()

    vlog.log_verdict(finding, is_real=True, source="agentic")

    lines = (tmp_path / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    rec = json.loads(lines[0])
    assert rec["event"] == "verdict"
    assert rec["pr"] == 99
    assert rec["is_real"] is True
    assert rec["source"] == "agentic"
    assert rec["file"] == "src/foo.py"
    assert rec["line"] == 42
    assert rec["category"] == "correctness"
    assert rec["severity"] == "high"
    assert rec["confidence"] == pytest.approx(0.9)
    assert rec["message"] == "Тестовое сообщение"
    assert rec["fingerprint"] == finding.fingerprint()
    assert "ts" in rec


def test_log_published_записывает_поля(tmp_path):
    """log_published создаёт запись с полем inline."""
    log_path = str(tmp_path / "verdicts.jsonl")
    vlog = VerdictLog(log_path, pr=7)
    finding = _make_finding(line=10, category="security")

    vlog.log_published(finding, inline=False)

    rec = json.loads((tmp_path / "verdicts.jsonl").read_text(encoding="utf-8").strip())
    assert rec["event"] == "published"
    assert rec["inline"] is False
    assert rec["category"] == "security"
    assert "is_real" not in rec


def test_message_обрезается_до_500(tmp_path):
    """Длинное сообщение обрезается до 500 символов."""
    long_msg = "А" * 600
    log_path = str(tmp_path / "verdicts.jsonl")
    vlog = VerdictLog(log_path, pr=0)
    finding = _make_finding(message=long_msg)

    vlog.log_verdict(finding, is_real=True)

    rec = json.loads((tmp_path / "verdicts.jsonl").read_text(encoding="utf-8").strip())
    assert len(rec["message"]) == 500


def test_две_записи_две_строки(tmp_path):
    """Два вызова дают ровно две строки в файле."""
    log_path = str(tmp_path / "verdicts.jsonl")
    vlog = VerdictLog(log_path, pr=1)
    f1 = _make_finding(file="a.py", line=1)
    f2 = _make_finding(file="b.py", line=2)

    vlog.log_verdict(f1, is_real=True)
    vlog.log_published(f2, inline=True)

    lines = (tmp_path / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "verdict"
    assert json.loads(lines[1])["event"] == "published"


def test_ошибка_записи_не_пробрасывается(tmp_path):
    """Если open бросает — VerdictLog молча проглатывает ошибку."""
    log_path = str(tmp_path / "verdicts.jsonl")
    vlog = VerdictLog(log_path, pr=0)
    finding = _make_finding()

    with patch("builtins.open", side_effect=OSError("нет доступа")):
        # не должно бросить исключение
        vlog.log_verdict(finding, is_real=False)
        vlog.log_published(finding, inline=True)


def test_создаётся_вложенная_директория(tmp_path):
    """VerdictLog создаёт вложенные директории при инициализации."""
    nested = tmp_path / "a" / "b" / "c"
    log_path = str(nested / "verdicts.jsonl")

    vlog = VerdictLog(log_path, pr=0)
    finding = _make_finding()
    vlog.log_verdict(finding, is_real=True)

    assert nested.exists()
    assert (nested / "verdicts.jsonl").exists()


def test_log_verdict_с_pr_по_умолчанию(tmp_path):
    """pr по умолчанию = 0."""
    log_path = str(tmp_path / "v.jsonl")
    vlog = VerdictLog(log_path)
    finding = _make_finding()
    vlog.log_verdict(finding, is_real=False)

    rec = json.loads((tmp_path / "v.jsonl").read_text(encoding="utf-8").strip())
    assert rec["pr"] == 0


def test_log_verdict_line_none(tmp_path):
    """line=None корректно сериализуется."""
    log_path = str(tmp_path / "v.jsonl")
    vlog = VerdictLog(log_path, pr=5)
    finding = _make_finding(line=None)
    vlog.log_verdict(finding, is_real=True)

    rec = json.loads((tmp_path / "v.jsonl").read_text(encoding="utf-8").strip())
    assert rec["line"] is None
