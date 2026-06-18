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
    monkeypatch.setattr(inst, "fetch_skills_etag", lambda *a, **k: None)  # без сети
    lines = inst.staleness_warnings(system="Linux")
    assert any("kimi" in ln.lower() or "Kimi" in ln for ln in lines)


def test_staleness_warnings_failsoft(monkeypatch, tmp_path):
    _setup_kimi(monkeypatch, tmp_path)  # установленный клиент → дойдём до skills_staleness
    monkeypatch.setattr(inst, "fetch_skills_etag", lambda *a, **k: None)
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(inst, "skills_staleness", boom)
    assert inst.staleness_warnings(system="Linux") == []  # исключение проглочено


def test_staleness_warnings_fetches_etag_once(monkeypatch, tmp_path):
    # несколько установленных клиентов → upstream-ETag тянется ОДИН раз на весь обход
    monkeypatch.setattr(inst, "_home", lambda: tmp_path)
    for key in ("kimi", "gemini"):
        d = inst.CLIENTS[key].skills_fn("Linux")
        (d / "sync-tasks").mkdir(parents=True)
        (d / "sync-tasks" / "SKILL.md").write_bytes(b"# sync")
        inst.stamp_skills_dir(d, source_etag='"same"')   # стамп + чистые хэши
    calls = {"n": 0}
    def fake_etag(*a, **k):
        calls["n"] += 1
        return '"same"'                                  # совпадает со стампом → свежо
    monkeypatch.setattr(inst, "fetch_skills_etag", fake_etag)
    lines = inst.staleness_warnings(system="Linux")
    assert lines == []           # оба свежие
    assert calls["n"] == 1       # один HEAD, не по разу на клиента


def test_staleness_warnings_no_network_when_nothing_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(inst, "_home", lambda: tmp_path)  # ни у кого нет каталога скилов
    def boom(*a, **k):
        raise AssertionError("сеть не должна дёргаться без установленных скилов")
    monkeypatch.setattr(inst, "fetch_skills_etag", boom)
    assert inst.staleness_warnings(system="Linux") == []


def test_offline_fresh_when_stamp_version_unknown(monkeypatch, tmp_path):
    d = _setup_kimi(monkeypatch, tmp_path)
    monkeypatch.setattr(inst, "current_pkg_version", lambda: "unknown")
    inst.stamp_skills_dir(d, source_etag='"e1"')           # стамп пишет pkg_version="unknown"
    monkeypatch.setattr(inst, "fetch_skills_etag", lambda *a, **k: None)
    monkeypatch.setattr(inst, "current_pkg_version", lambda: "1.0.0")
    rep = inst.skills_staleness(inst.CLIENTS["kimi"], system="Linux")
    assert rep is not None and rep.stale is False          # стамп "unknown" → fresh


def test_offline_fresh_same_version(monkeypatch, tmp_path):
    d = _setup_kimi(monkeypatch, tmp_path)
    inst.stamp_skills_dir(d, source_etag='"e1"')
    monkeypatch.setattr(inst, "fetch_skills_etag", lambda *a, **k: None)
    rep = inst.skills_staleness(inst.CLIENTS["kimi"], system="Linux")
    assert rep is not None and rep.stale is False
