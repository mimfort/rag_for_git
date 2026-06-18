from pathlib import Path

from reviewer import install as inst


def _setup_kimi(monkeypatch, tmp_path) -> Path:
    monkeypatch.setattr(inst, "_home", lambda: tmp_path)
    d = inst.CLIENTS["kimi"].skills_fn("Linux")
    (d / "sync-tasks").mkdir(parents=True)
    (d / "sync-tasks" / "SKILL.md").write_bytes(b"# sync")
    return d


def test_none_when_no_skills_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(inst, "_home", lambda: tmp_path)
    assert inst.skills_staleness(inst.CLIENTS["kimi"], system="Linux") is None


def test_stale_when_no_stamp(monkeypatch, tmp_path):
    _setup_kimi(monkeypatch, tmp_path)
    rep = inst.skills_staleness(inst.CLIENTS["kimi"], system="Linux")
    assert rep.stale and "стамп" in rep.reason
    assert rep.command == "reviewer install-skills kimi"


def test_stale_on_local_drift(monkeypatch, tmp_path):
    d = _setup_kimi(monkeypatch, tmp_path)
    inst.stamp_skills_dir(d, source_etag='"e1"')
    (d / "sync-tasks" / "SKILL.md").write_bytes(b"# CHANGED locally")  # дрейф
    rep = inst.skills_staleness(inst.CLIENTS["kimi"], system="Linux")
    assert rep.stale and "разош" in rep.reason


def test_stale_on_etag_mismatch(monkeypatch, tmp_path):
    d = _setup_kimi(monkeypatch, tmp_path)
    inst.stamp_skills_dir(d, source_etag='"old"')
    monkeypatch.setattr(inst, "fetch_skills_etag", lambda *a, **k: '"new"')
    rep = inst.skills_staleness(inst.CLIENTS["kimi"], system="Linux")
    assert rep.stale and "upstream" in rep.reason


def test_fresh_when_etag_matches(monkeypatch, tmp_path):
    d = _setup_kimi(monkeypatch, tmp_path)
    inst.stamp_skills_dir(d, source_etag='"same"')
    monkeypatch.setattr(inst, "fetch_skills_etag", lambda *a, **k: '"same"')
    rep = inst.skills_staleness(inst.CLIENTS["kimi"], system="Linux")
    assert rep.stale is False


def test_offline_fallback_pkg_version(monkeypatch, tmp_path):
    d = _setup_kimi(monkeypatch, tmp_path)
    inst.stamp_skills_dir(d, source_etag='"e1"')
    # стамп пишет current_pkg_version(); эмулируем «сервер уехал вперёд» + офлайн
    monkeypatch.setattr(inst, "fetch_skills_etag", lambda *a, **k: None)
    monkeypatch.setattr(inst, "current_pkg_version", lambda: "99.0.0")
    rep = inst.skills_staleness(inst.CLIENTS["kimi"], system="Linux")
    assert rep.stale and "сервер обновился" in rep.reason


def test_staleness_warnings_collects(monkeypatch, tmp_path):
    _setup_kimi(monkeypatch, tmp_path)  # kimi без стампа → stale
    lines = inst.staleness_warnings(system="Linux")
    assert any("kimi" in ln.lower() or "Kimi" in ln for ln in lines)


def test_staleness_warnings_failsoft(monkeypatch, tmp_path):
    monkeypatch.setattr(inst, "_home", lambda: tmp_path)
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(inst, "skills_staleness", boom)
    assert inst.staleness_warnings(system="Linux") == []  # не падает
