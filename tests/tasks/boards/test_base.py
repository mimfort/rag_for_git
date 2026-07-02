from reviewer.config.settings import Settings
from reviewer.tasks.boards import (
    RawTask, make_board_provider, make_board_providers,
)
from reviewer.tasks.boards.base import project_prefix


def test_project_prefix_extracts_alpha_prefix():
    assert project_prefix("PRI-5") == "PRI"
    assert project_prefix("TES-1") == "TES"
    assert project_prefix("0DEV-7") == ""        # код должен начинаться с буквы
    assert project_prefix("") == ""
    assert project_prefix("UUID-NO-NUM") == ""   # хвост не число → не код задачи


def test_rawtask_fields_and_links_default():
    rt = RawTask(key="ID-1", project_code="PRI-1", title="t", description="d",
                 status="Backlog", subtask_ids=["u1"], timestamp=123)
    assert rt.key == "ID-1" and rt.timestamp == 123
    assert rt.links == []                       # links — необязательное, дефолт пуст


def test_rawtask_links_explicit():
    rt = RawTask(key="A-1", project_code="A-1", title="t", description="",
                 status=None, subtask_ids=[], timestamp=1,
                 links=[{"type": "related", "key": "A-2"}])
    assert rt.links == [{"type": "related", "key": "A-2"}]


def test_make_provider_none_when_no_api_key():
    s = Settings(_env_file=None, yougile_api_key="")
    assert make_board_provider(s, "yougile") is None


def test_make_provider_unknown_type_none():
    s = Settings(_env_file=None)
    assert make_board_provider(s, "jira") is None


def test_make_provider_yougile():
    s = Settings(_env_file=None, yougile_api_key="k",
                 task_board_key_pattern=r"[A-Z]+-\d+")
    prov = make_board_provider(s, "yougile")
    assert prov is not None and prov.__class__.__name__ == "YougileBoard"
    assert prov.board_type == "yougile"
    prov.close()


def test_make_provider_youtrack():
    s = Settings(_env_file=None, youtrack_token="perm:x",
                 youtrack_base_url="https://c.youtrack.cloud/api",
                 task_board_key_pattern=r"[A-Z]+-\d+")
    prov = make_board_provider(s, "youtrack")
    assert prov is not None and prov.__class__.__name__ == "YouTrackBoard"
    assert prov.board_type == "youtrack"
    prov.close()


def test_make_providers_collects_all_configured():
    s = Settings(_env_file=None, yougile_api_key="yk", youtrack_token="perm:x",
                 youtrack_base_url="https://c.youtrack.cloud/api")
    provs = make_board_providers(s)
    assert {p.board_type for p in provs} == {"yougile", "youtrack"}
    for p in provs:
        p.close()


def test_make_providers_empty_when_nothing_configured():
    s = Settings(_env_file=None, yougile_api_key="", youtrack_token="",
                 task_board_api_key="")
    assert make_board_providers(s) == []


def test_make_board_provider_threads_status_field(monkeypatch):
    import reviewer.tasks.boards as boards

    captured = {}

    class _FakeYT:
        board_type = "youtrack"

        def __init__(self, *, token, base_url, key_pattern, status_field="State", **kw):
            captured["status_field"] = status_field

    monkeypatch.setattr("reviewer.tasks.boards.youtrack.YouTrackBoard", _FakeYT)

    settings = type("S", (), {
        "board_creds": staticmethod(lambda t: ("tok", "https://yt/api")),
        "task_board_key_pattern": r"TES-\d+",
        "task_board_url_template": "",
        "task_attachment_max_bytes": 1,
        "task_attachment_timeout": 1.0,
        "task_attachment_store_chars": 1,
    })()
    boards.make_board_provider(settings, "youtrack", status_field="Stage")
    assert captured["status_field"] == "Stage"
