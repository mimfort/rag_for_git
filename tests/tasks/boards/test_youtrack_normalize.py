from reviewer.tasks.boards.youtrack import _issue_to_raw, normalize_youtrack

KP = r"[A-Z]+-\d+"
BASE = "https://c.youtrack.cloud/api"


def _issue(**kw):
    base = {
        "idReadable": "PRJ-7",
        "summary": "Заголовок",
        "description": "",
        "updated": 1700000000000,
        "customFields": [{"name": "State", "value": {"name": "In Progress"}}],
        "links": [],
    }
    base.update(kw)
    return base


def test_issue_to_raw_basic():
    raw = _issue_to_raw(_issue())
    assert raw.key == "PRJ-7"
    assert raw.project_code == "PRJ-7"
    assert raw.title == "Заголовок"
    assert raw.status == "In Progress"
    assert raw.timestamp == 1700000000000
    assert raw.subtask_ids == []


def test_issue_to_raw_state_missing():
    raw = _issue_to_raw(_issue(customFields=[{"name": "Priority",
                                              "value": {"name": "High"}}]))
    assert raw.status is None


def test_issue_to_raw_links_subtask_vs_related():
    issue = _issue(links=[
        {"direction": "OUTWARD", "linkType": {"name": "Subtask"},
         "issues": [{"idReadable": "PRJ-8"}]},
        {"direction": "OUTWARD", "linkType": {"name": "Relates"},
         "issues": [{"idReadable": "PRJ-9"}]},
    ])
    raw = _issue_to_raw(issue)
    assert {"type": "subtask", "key": "PRJ-8"} in raw.links
    assert {"type": "related", "key": "PRJ-9"} in raw.links


def test_normalize_url_and_aliases():
    raw = _issue_to_raw(_issue())
    b = normalize_youtrack(raw, KP, BASE)
    assert b["key"] == "PRJ-7"
    assert b["aliases"] == []
    assert b["criteria"] == []
    assert b["status"] == "In Progress"
    assert b["url"] == "https://c.youtrack.cloud/issue/PRJ-7"   # /api отброшен


def test_normalize_url_none_without_base():
    raw = _issue_to_raw(_issue())
    assert normalize_youtrack(raw, KP, "")["url"] is None


def test_normalize_related_from_description_excludes_self_and_links():
    issue = _issue(description="зависит от ABC-1 и PRJ-7 и PRJ-8",
                   links=[{"direction": "OUTWARD", "linkType": {"name": "Subtask"},
                           "issues": [{"idReadable": "PRJ-8"}]}])
    raw = _issue_to_raw(issue)
    b = normalize_youtrack(raw, KP, BASE)
    rels = {lk["key"] for lk in b["links"] if lk["type"] == "related"}
    assert rels == {"ABC-1"}            # self PRJ-7 и уже-связанный PRJ-8 исключены


def test_normalize_null_description():
    raw = _issue_to_raw(_issue(description=None))
    b = normalize_youtrack(raw, KP, BASE)
    assert b["description"] == ""
    assert b["links"] == []


def test_issue_to_raw_state_non_dict_value():
    raw = _issue_to_raw(_issue(customFields=[{"name": "State", "value": None}]))
    assert raw.status is None
    raw2 = _issue_to_raw(_issue(customFields=[{"name": "State", "value": "Done"}]))
    assert raw2.status is None


def test_normalize_sets_project_prefix():
    raw = _issue_to_raw(_issue(idReadable="PRJ-7"))
    b = normalize_youtrack(raw, KP, BASE)
    assert b["project"] == "PRJ"


def test_normalize_includes_injected_attachments():
    raw = _issue_to_raw(_issue())
    atts = [{"name": "spec.md", "mime_type": "text/markdown", "size": 4, "content_text": "spec"}]
    b = normalize_youtrack(raw, KP, BASE, attachments=atts)
    assert b["attachments"] == atts


def test_normalize_attachments_default_empty():
    raw = _issue_to_raw(_issue())
    assert normalize_youtrack(raw, KP, BASE)["attachments"] == []
