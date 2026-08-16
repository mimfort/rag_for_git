"""Хранилище снимков replay и проверка сравнимости (PRI-254)."""
from __future__ import annotations

import pytest

from eval.solve_task_metrics import replay_history as rh


def _snap(**over):
    snap = {
        "schema": 1,
        "taken_at": "2026-08-17T00:00:00+00:00",
        "commit": "aaaaaaa",
        "variant": "baseline",
        "variant_params": None,
        "repo": "o/n",
        "branch": "dev",
        "indexed_sha": "sha1",
        "chunks": 10,
        "graph_nodes": 20,
        "partial": False,
        "corpus": 1,
        "statuses": {},
        "aggregate": {},
        "tasks": [],
    }
    snap.update(over)
    return snap


def test_append_and_load_roundtrip(tmp_path):
    path = tmp_path / rh.HISTORY_PATH_NAME
    rh.append(path, _snap(commit="one"))
    rh.append(path, _snap(commit="two"))
    assert [s["commit"] for s in rh.load(path)] == ["one", "two"]


def test_load_missing_file_is_empty(tmp_path):
    assert rh.load(tmp_path / "нет.jsonl") == []


def test_select_last_and_offset():
    snaps = [_snap(commit="one"), _snap(commit="two"), _snap(commit="three")]
    assert rh.select(snaps, "last")["commit"] == "three"
    assert rh.select(snaps, "-1")["commit"] == "two"


def test_select_by_variant_takes_most_recent():
    snaps = [
        _snap(variant="baseline", commit="one"),
        _snap(variant="limits", commit="two"),
        _snap(variant="baseline", commit="three"),
    ]
    assert rh.select(snaps, "baseline")["commit"] == "three"


def test_select_rejects_partial_snapshot():
    with pytest.raises(rh.PartialSnapshotRejected):
        rh.select([_snap(partial=True)], "last")


def test_select_on_empty_history_raises():
    with pytest.raises(rh.SnapshotNotFound):
        rh.select([], "last")


def test_select_unknown_variant_raises():
    with pytest.raises(rh.SnapshotNotFound):
        rh.select([_snap()], "нет-такого")


def test_comparability_warns_on_index_drift():
    warnings = rh.comparability_warnings(
        _snap(indexed_sha="sha1"), _snap(indexed_sha="sha2")
    )
    assert any("indexed_sha" in w for w in warnings)


def test_comparability_warns_on_commit_mismatch():
    warnings = rh.comparability_warnings(_snap(commit="a"), _snap(commit="b"))
    assert any("коммит" in w for w in warnings)


def test_comparability_silent_when_identical():
    assert rh.comparability_warnings(_snap(), _snap()) == []
