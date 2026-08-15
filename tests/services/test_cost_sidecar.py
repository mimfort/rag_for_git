import json
import tempfile
from pathlib import Path

import pytest

from reviewer.services import cost_sidecar


@pytest.fixture
def tmp_tempdir(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def _write(repo, pr, data):
    path = Path(cost_sidecar.sidecar_path(repo, pr))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _valid(**over):
    data = {"version": 1, "repo": "owner/name", "pr": 7, "model": "opus",
            "usage": {"by_model": {"opus": {"output": 3}}}, "total_cost": 12.5,
            "written_at": "2999-01-01T00:00:00+00:00"}
    data.update(over)
    return data


def test_reads_valid_sidecar_and_deletes_it(tmp_tempdir):
    path = _write("owner/name", 7, _valid())
    got = cost_sidecar.read_cost_sidecar("owner/name", 7)
    assert got["model"] == "opus"
    assert got["total_cost"] == 12.5
    assert got["usage"]["by_model"]["opus"]["output"] == 3
    assert not path.exists(), "sidecar обязан удаляться, иначе следующее ревью переиспользует замер"


def test_missing_sidecar_returns_none(tmp_tempdir):
    assert cost_sidecar.read_cost_sidecar("owner/name", 7) is None


def test_broken_json_is_ignored_and_removed(tmp_tempdir):
    path = Path(cost_sidecar.sidecar_path("owner/name", 7))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert cost_sidecar.read_cost_sidecar("owner/name", 7) is None
    assert not path.exists()


def test_foreign_version_is_ignored(tmp_tempdir):
    _write("owner/name", 7, _valid(version=99))
    assert cost_sidecar.read_cost_sidecar("owner/name", 7) is None


def test_stale_sidecar_is_ignored(tmp_tempdir):
    _write("owner/name", 7, _valid(written_at="2000-01-01T00:00:00+00:00"))
    assert cost_sidecar.read_cost_sidecar("owner/name", 7) is None


def test_sidecar_of_another_pr_is_not_read(tmp_tempdir):
    _write("owner/name", 8, _valid(pr=8))
    assert cost_sidecar.read_cost_sidecar("owner/name", 7) is None


def test_merge_prefers_explicit_per_field():
    sidecar = {"model": "opus", "usage": {"a": 1}, "total_cost": 10.0}
    merged = cost_sidecar.merge_metadata(
        {"model": "gpt", "usage": None, "total_cost": None}, sidecar)
    assert merged == {"model": "gpt", "usage": {"a": 1}, "total_cost": 10.0}


def test_merge_without_sidecar_returns_explicit():
    explicit = {"model": None, "usage": None, "total_cost": None}
    assert cost_sidecar.merge_metadata(explicit, None) == explicit
