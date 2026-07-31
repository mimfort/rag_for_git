"""Чистая классификация file-level фрагментов сводок без I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from reviewer.tasks.boards.base import JsonValue


@dataclass(frozen=True)
class FragmentFile:
    """Файл и его сводка в результате расчёта дельты."""

    path: str
    fingerprint: str
    summary: str = ""
    provenance: Mapping[str, JsonValue] = field(default_factory=dict)
    from_cluster_key: str | None = None


@dataclass(frozen=True)
class StoredSummaryFragment:
    """Сохранённый file-level фрагмент до расчёта дельты."""

    cluster_key: str
    path: str
    fingerprint: str
    summary: str
    provenance: Mapping[str, JsonValue]


@dataclass(frozen=True)
class FragmentDelta:
    """Детерминированное распределение текущих и сохранённых фрагментов."""

    added: tuple[FragmentFile, ...]
    changed: tuple[FragmentFile, ...]
    removed: tuple[FragmentFile, ...]
    moved: tuple[FragmentFile, ...]
    reused: tuple[FragmentFile, ...]

    @property
    def pending_paths(self) -> tuple[str, ...]:
        """Пути, для которых нужно сгенерировать новую сводку."""
        return tuple(item.path for item in self.added + self.changed)


def _from_stored(fragment: StoredSummaryFragment, *, from_cluster_key: str | None = None) -> FragmentFile:
    return FragmentFile(
        path=fragment.path,
        fingerprint=fragment.fingerprint,
        summary=fragment.summary,
        provenance=fragment.provenance,
        from_cluster_key=from_cluster_key,
    )


def build_fragment_delta(
    cluster_key: str,
    current: Mapping[str, str],
    stored: list[StoredSummaryFragment],
    *,
    bootstrap: bool,
    full_rebuild: bool,
) -> FragmentDelta:
    """Классифицировать file-level фрагменты без обращения к хранилищу.

    Фрагмент текущего кластера имеет приоритет над исторической копией другого
    кластера. Межкластерное переиспользование разрешено только в обычном
    инкрементальном режиме и лишь при точном совпадении пути и fingerprint.
    """
    current_cluster = [item for item in stored if item.cluster_key == cluster_key]
    by_current_path: dict[str, list[StoredSummaryFragment]] = {}
    for item in current_cluster:
        by_current_path.setdefault(item.path, []).append(item)
    by_exact_cross_cluster: dict[tuple[str, str], StoredSummaryFragment] = {}
    if not bootstrap and not full_rebuild:
        for item in stored:
            if item.cluster_key != cluster_key:
                by_exact_cross_cluster.setdefault((item.path, item.fingerprint), item)

    added: list[FragmentFile] = []
    changed: list[FragmentFile] = []
    moved: list[FragmentFile] = []
    reused: list[FragmentFile] = []
    for path, fingerprint in sorted(current.items()):
        fresh = FragmentFile(path=path, fingerprint=fingerprint)
        if bootstrap or full_rebuild:
            added.append(fresh)
            continue

        current_fragments = by_current_path.get(path, [])
        matching_current = next(
            (item for item in current_fragments if item.fingerprint == fingerprint), None
        )
        if matching_current is not None:
            reused.append(_from_stored(matching_current))
            continue
        if current_fragments:
            changed.append(fresh)
            continue

        previous = by_exact_cross_cluster.get((path, fingerprint))
        if previous is not None:
            moved.append(_from_stored(previous, from_cluster_key=previous.cluster_key))
        else:
            added.append(fresh)

    removed_by_path = {
        item.path: item for item in current_cluster if item.path not in current
    }
    removed = [_from_stored(item) for _, item in sorted(removed_by_path.items())]
    return FragmentDelta(
        added=tuple(added),
        changed=tuple(changed),
        removed=tuple(removed),
        moved=tuple(moved),
        reused=tuple(reused),
    )
