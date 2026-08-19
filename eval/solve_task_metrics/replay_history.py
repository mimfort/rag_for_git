"""Хранилище снимков replay (PRI-254).

Своя схема и свой файл: формат snapshot агрегатный, а отчёт A/B обязан
показывать дельту ПО ЗАДАЧАМ, поэтому смешивать истории нельзя.
"""
from __future__ import annotations

import json
import pathlib

HISTORY_PATH_NAME = "replay_history.jsonl"


class SnapshotNotFound(ValueError):
    """В истории нет снимка, подходящего под ссылку."""


class PartialSnapshotRejected(ValueError):
    """Частичный снимок (--limit) нельзя использовать как сторону сравнения."""


def append(path: pathlib.Path, snapshot: dict) -> None:
    """Дописать снимок в jsonl-историю."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False) + "\n")


def load(path: pathlib.Path) -> list:
    """Прочитать историю; отсутствующий файл — пустая история."""
    if not path.exists():
        return []
    out: list = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def select(snapshots: list, ref: str) -> dict:
    """Выбрать снимок: 'last', отступ '-N' или имя варианта (последний такой).

    Частичный снимок отвергается явно: сравнение усечённого корпуса с полным
    даёт разницу корпуса, а не эффект варианта. Тот же fail-closed, что у
    sync_board, где --limit отключает продвижение курсора.
    """
    if not snapshots:
        raise SnapshotNotFound("история replay пуста; сначала прогоните replay")
    if ref == "last":
        chosen = snapshots[-1]
    elif ref.startswith("-") and ref[1:].isdigit():
        offset = int(ref[1:])
        if offset >= len(snapshots):
            raise SnapshotNotFound(
                f"в истории {len(snapshots)} снимк(ов) — отступ {ref} недоступен"
            )
        chosen = snapshots[-1 - offset]
    else:
        matching = [s for s in snapshots if s.get("variant") == ref]
        if not matching:
            raise SnapshotNotFound(f"снимков варианта {ref!r} в истории нет")
        chosen = matching[-1]
    if chosen.get("partial"):
        raise PartialSnapshotRejected(
            "снимок помечен partial (снят с --limit) и не годится как сторона "
            "сравнения; прогоните полный replay"
        )
    return chosen


def comparability_warnings(old: dict, new: dict) -> list:
    """Чем стороны сравнения различаются помимо варианта.

    Молча склеивать снимки с разных состояний индекса нельзя: дрейф индекса
    выглядел бы как эффект варианта.
    """
    warnings: list = []
    if old.get("indexed_sha") != new.get("indexed_sha"):
        warnings.append(
            f"indexed_sha сторон различается ({old.get('indexed_sha')} против "
            f"{new.get('indexed_sha')}): дельта включает дрейф base-индекса"
        )
    if old.get("commit") != new.get("commit"):
        warnings.append(
            f"коммит сторон различается ({old.get('commit')} против "
            f"{new.get('commit')}): ground truth мог измениться"
        )
    if old.get("corpus") != new.get("corpus"):
        warnings.append(
            f"размер корпуса различается ({old.get('corpus')} против "
            f"{new.get('corpus')})"
        )
    return warnings
