"""Слияние генерируемой и ручной частей отчёта (PRI-265)."""
from __future__ import annotations

import pathlib

import pytest

from eval.solve_task_metrics import report_merge

MANUAL = "## Приёмка PRI-262\n\nЧисла, оговорки, ручной разбор.\n"


def _with_marker(generated: str, manual: str) -> str:
    return f"{generated}\n{report_merge.MARKER}\n\n{manual}"


def test_manual_tail_starts_at_marker_and_is_byte_exact():
    existing = _with_marker("# Отчёт\n\nстарые числа\n", MANUAL)
    tail = report_merge.manual_tail(existing)
    assert tail.startswith(report_merge.MARKER)
    assert tail.endswith(MANUAL)
    # хвост — срез исходного текста, а не пересборка
    assert tail == existing[existing.index(report_merge.MARKER):]


def test_manual_tail_of_empty_text_is_empty():
    assert report_merge.manual_tail("") == ""


def test_merge_keeps_manual_tail_byte_for_byte():
    existing = _with_marker("# Отчёт\n\nстарые числа\n", MANUAL)
    merged = report_merge.merge("# Отчёт\n\nНОВЫЕ числа\n", existing)
    assert "НОВЫЕ числа" in merged
    assert "старые числа" not in merged
    assert merged.endswith(existing[existing.index(report_merge.MARKER):])


def test_merge_without_existing_file_appends_marker():
    merged = report_merge.merge("# Отчёт\n\nчисла\n", "")
    assert merged.count(report_merge.MARKER) == 1
    assert merged.rstrip().endswith(report_merge.MARKER)


def test_merge_is_idempotent_and_never_duplicates_marker():
    first = report_merge.merge("# Отчёт\n\nA\n", "")
    second = report_merge.merge("# Отчёт\n\nB\n", first)
    third = report_merge.merge("# Отчёт\n\nC\n", second)
    assert third.count(report_merge.MARKER) == 1


def test_ensure_mergeable_accepts_missing_file(tmp_path: pathlib.Path):
    report_merge.ensure_mergeable(tmp_path / "нет-такого.md")


def test_ensure_mergeable_accepts_file_with_marker(tmp_path: pathlib.Path):
    path = tmp_path / "r.md"
    path.write_text(_with_marker("# Отчёт\n", MANUAL), encoding="utf-8")
    report_merge.ensure_mergeable(path)


def test_ensure_mergeable_rejects_file_without_marker(tmp_path: pathlib.Path):
    """Догадка о границе запрещена: файл без маркера отвергается, а не режется."""
    path = tmp_path / "r.md"
    path.write_text("# Отчёт\n\n## Приёмка PRI-262\n\nручное\n", encoding="utf-8")
    with pytest.raises(report_merge.MarkerMissing) as error:
        report_merge.ensure_mergeable(path)
    assert str(path) in str(error.value)
    assert report_merge.MARKER in str(error.value)


def test_merge_file_reads_existing_and_returns_without_writing(tmp_path: pathlib.Path):
    path = tmp_path / "r.md"
    original = _with_marker("# Отчёт\n\nстарое\n", MANUAL)
    path.write_text(original, encoding="utf-8")
    merged = report_merge.merge_file(path, "# Отчёт\n\nновое\n")
    assert "новое" in merged and MANUAL in merged
    assert path.read_text(encoding="utf-8") == original  # запись — дело вызывающего
