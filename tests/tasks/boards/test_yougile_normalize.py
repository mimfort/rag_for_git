from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.boards.yougile import normalize_yougile

KP = r"[A-Z]+-\d+"
URL = "https://ru.yougile.com/team/T/#{code}"


def _raw(**kw):
    base = dict(key="ID-10", project_code="PRI-10", title="T", description="",
                status="Backlog", subtask_ids=[], timestamp=1)
    base.update(kw)
    return RawTask(**base)


def test_key_aliases_status_url():
    b = normalize_yougile(_raw(), KP, URL)
    assert b["key"] == "ID-10"
    assert b["aliases"] == ["PRI-10"]
    assert b["status"] == "Backlog"
    assert b["criteria"] == []
    assert b["url"] == "https://ru.yougile.com/team/T/#PRI-10"


def test_url_none_without_template():
    b = normalize_yougile(_raw(), KP, "")
    assert b["url"] is None


def test_related_links_from_description_excluding_self():
    raw = _raw(description="связано с PRI-96 и ID-10 и PRI-10 и ABC-7")
    b = normalize_yougile(raw, KP, URL)
    rels = {lk["key"] for lk in b["links"] if lk["type"] == "related"}
    assert rels == {"PRI-96", "ABC-7"}        # self key ID-10 и alias PRI-10 исключены


def test_subtask_links_with_titles_and_related_dedup():
    raw = _raw(description="см. ID-55", subtask_ids=["u1"])
    b = normalize_yougile(raw, KP, URL, subtask_titles={"u1": "ID-55:Подзадача"})
    sub = [lk for lk in b["links"] if lk["type"] == "subtask"]
    assert sub == [{"type": "subtask", "key": "u1", "title": "ID-55:Подзадача"}]
    rels = {lk["key"] for lk in b["links"] if lk["type"] == "related"}
    assert "ID-55" in rels                     # код ID-55 — отдельный от UUID u1


def test_alias_omitted_when_equals_key():
    b = normalize_yougile(_raw(project_code="ID-10"), KP, URL)
    assert b["aliases"] == []


def test_subtask_without_title_keeps_edge():
    raw = _raw(subtask_ids=["u9"])
    b = normalize_yougile(raw, KP, URL)
    assert b["links"] == [{"type": "subtask", "key": "u9"}]


def test_normalize_sets_project_prefix():
    b = normalize_yougile(_raw(project_code="PRI-10"), KP, URL)
    assert b["project"] == "PRI"
