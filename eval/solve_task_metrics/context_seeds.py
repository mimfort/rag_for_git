"""Сид-символы контекстного ядра: что дифф задачи реально трогал (PRI-261).

Сиды — НЕ все символы изменённых файлов. Замер: сидирование целыми файлами даёт
медиану 57 новых core-файлов (37 % репозитория, «мусорное ядро»), сидирование
затронутыми символами — 14.5 на том же графе и той же глубине. Разница вчетверо
решается здесь, а не выбором числа хопов.

Символы берутся на КОММИТЕ МЕРЖА, а не из сегодняшнего индекса: номера строк
диффа относятся к своему коммиту, и наложение их на сегодняшние диапазоны чанков
попадает не в те символы у любого файла, который с тех пор менялся. С сегодняшним
графом результат сшивается по имени символа, а не по строке.
"""
from __future__ import annotations

import re

from reviewer.index.chunker import chunk_python
from reviewer.metrics.brief_quality.classify import is_core_production_path

from . import ground_truth

# '@@ -a,b +c,d @@ [контекст]'. Интересует только правая сторона: сид — символ
# в состоянии ПОСЛЕ мержа, который сегодняшний граф и знает по имени.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_hunk_ranges(diff_text: str) -> list:
    """Диапазоны строк правой стороны диффа: [(start, end), ...].

    Чистое удаление (длина 0) даёт диапазон в одну строку на месте стыка:
    у удалённого кода иначе не было бы сида вовсе, и задача теряла бы часть
    знаменателя молча.
    """
    ranges: list = []
    for line in (diff_text or "").splitlines():
        match = _HUNK_RE.match(line)
        if not match:
            continue
        start = int(match.group(1))
        length = int(match.group(2)) if match.group(2) is not None else 1
        if length == 0:
            ranges.append((start, start))
        else:
            ranges.append((start, start + length - 1))
    return ranges


def _symbols_at(path: str, source: str, ranges: list) -> set:
    """Символы файла, чьи диапазоны пересекаются с изменёнными строками."""
    try:
        chunks = chunk_python(path, source.encode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — не-Python или битый файл не роняет прогон
        return set()
    hits: set = set()
    for chunk in chunks:
        for start, end in ranges:
            if chunk.start_line <= end and chunk.end_line >= start:
                hits.add(f"{path}#{chunk.symbol_fqn}")
                break
    return hits


def seeds_for_merge(sha: str, core_paths: set, run_git) -> set:
    """Сид-символы одного PR-мержа по его core-путям."""
    seeds: set = set()
    for path in sorted(p for p in core_paths if is_core_production_path(p)):
        try:
            diff = run_git(["diff", "--unified=0", f"{sha}^1", sha, "--", path])
            source = run_git(["show", f"{sha}:{path}"])
        except ground_truth.GitError:
            # Путь удалён или недостижим на этом коммите: сидов меньше, прогон жив.
            continue
        ranges = parse_hunk_ranges(diff)
        if ranges:
            seeds |= _symbols_at(path, source, ranges)
    return seeds


def collect_seeds(truth, run_git) -> set:
    """Сид-символы задачи: объединение по всем её настоящим PR-мержам."""
    core_paths = {p for p in truth.changed if is_core_production_path(p)}
    seeds: set = set()
    for sha in truth.merge_shas:
        seeds |= seeds_for_merge(sha, core_paths, run_git)
    return seeds
