from reviewer.tasks.graph import PRRef, TaskGraph


class _FakeDriver:
    def __init__(self, records=None):
        self.calls = []
        self._records = records if records is not None else []

    def execute_query(self, query, **params):
        self.calls.append((query, params))
        return (self._records, None, None)


def test_upsert_task_codes_are_key_plus_aliases_deduped():
    d = _FakeDriver()
    TaskGraph(d).upsert_task("ID-1", ["PRI-2", "ID-1"], "T", "Open", "u")
    _query, params = d.calls[0]
    assert params["key"] == "ID-1"
    assert params["codes"] == ["ID-1", "PRI-2"]  # key first, self-alias dropped


def test_upsert_links_filters_keyless_and_counts():
    d = _FakeDriver()
    n = TaskGraph(d).upsert_links("ID-1", [
        {"key": "ID-2", "title": "child", "type": "subtask"},
        {"title": "no key"},  # dropped
    ])
    assert n == 1
    _query, params = d.calls[0]
    assert params["rows"] == [{"key": "ID-2", "title": "child", "type": "subtask"}]


def test_upsert_links_empty_does_not_query():
    d = _FakeDriver()
    assert TaskGraph(d).upsert_links("ID-1", []) == 0
    assert d.calls == []


def test_link_pr_params():
    d = _FakeDriver()
    pr = PRRef(repo="o/r", number=7, url="https://github.com/o/r/pull/7", sha="abc")
    TaskGraph(d).link_pr("ID-1", pr, ["a.py#foo", "b.py#bar"])
    _query, params = d.calls[0]
    assert params["key"] == "ID-1"
    assert params["pid"] == "o/r#7"
    assert params["repo"] == "o/r" and params["number"] == 7 and params["sha"] == "abc"
    assert params["touched"] == ["a.py#foo", "b.py#bar"]


def test_task_context_parses_record():
    rec = {
        "key": "ID-1", "title": "T", "status": "Open", "url": "u",
        "prs": [{"id": "o/r#7", "url": "pr", "sha": "abc", "touched": ["a.py#foo"]}],
        "linked": [{"key": "ID-2", "title": "child", "status": "Done",
                    "type": "subtask", "prs": [{"id": "o/r#8", "url": "pr8"}]}],
    }
    ctx = TaskGraph(_FakeDriver([rec])).task_context("ID-1")
    assert ctx["key"] == "ID-1"
    assert ctx["prs"][0]["touched"] == ["a.py#foo"]
    assert ctx["linked"][0]["type"] == "subtask"


def test_task_context_empty_when_no_record():
    assert TaskGraph(_FakeDriver([])).task_context("ZZ-9") == {}
