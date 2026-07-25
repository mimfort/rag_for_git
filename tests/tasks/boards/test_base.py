from reviewer.config.settings import Settings
from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.tasks.boards import (
    RawTask, make_board_provider, make_board_providers,
)
from reviewer.tasks.boards.base import project_prefix
from reviewer.tasks.boards.registry import BoardProviderRegistry
from tests.tasks.boards.provider_fakes import FakeBoard, fake_provider_spec


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


def test_factory_uses_injected_registry_for_new_provider():
    registry = BoardProviderRegistry([fake_provider_spec()])
    settings = Settings(_env_file=None)
    provider = make_board_provider(
        settings,
        "fake",
        registry=registry,
        credential_source=ProviderCredentialSource(values={"FAKE_TOKEN": "x"}),
    )
    assert isinstance(provider, FakeBoard)
    assert provider.board_type == "fake"


def test_make_providers_reads_env_credentials_absent_from_settings(monkeypatch):
    monkeypatch.setenv("FAKE_TOKEN", "env-only-token")
    registry = BoardProviderRegistry([fake_provider_spec()])

    providers = make_board_providers(
        Settings(_env_file=None),
        registry=registry,
    )

    assert len(providers) == 1
    assert isinstance(providers[0], FakeBoard)
    assert providers[0].context.credentials["FAKE_TOKEN"] == "env-only-token"
    providers[0].close()


def test_make_board_provider_threads_immutable_options():
    settings = Settings(
        _env_file=None,
        youtrack_token="perm:x",
        youtrack_base_url="https://yt.example/api",
    )
    provider = make_board_provider(
        settings,
        "youtrack",
        provider_options={"status_field": "Stage"},
    )
    assert provider is not None
    assert provider._status_field == "Stage"
    assert not hasattr(provider, "set_status_field")
    provider.close()


def test_default_registry_contains_exactly_current_complete_providers():
    from reviewer.tasks.boards import registry as registry_module
    from tests.tasks.boards.test_registry import EXPECTED_BOARD_TYPES

    registry = registry_module.default_board_registry()
    assert registry.registered_types() == EXPECTED_BOARD_TYPES
    assert registry_module.default_board_registry() is registry


def test_make_providers_closes_created_providers_when_later_factory_fails():
    made: list[FakeBoard] = []

    def first(context):
        provider = FakeBoard(context)
        provider.board_type = "first"
        made.append(provider)
        return provider

    def second(context):
        raise RuntimeError("boom")

    registry = BoardProviderRegistry([
        fake_provider_spec(factory=first, board_type="first"),
        fake_provider_spec(factory=second, board_type="second"),
    ])
    credentials = ProviderCredentialSource(values={"FAKE_TOKEN": "x"})
    settings = Settings(_env_file=None)
    import pytest

    with pytest.raises(RuntimeError, match="boom"):
        make_board_providers(
            settings,
            registry=registry,
            credential_source=credentials,
        )
    assert made[0].closed is True


def test_generic_modules_do_not_branch_on_concrete_board_types():
    from pathlib import Path

    root = Path(__file__).parents[3]
    for relative in (
        "reviewer/tasks/boards/__init__.py",
        "reviewer/config/settings.py",
        "reviewer/app.py",
    ):
        text = (root / relative).read_text()
        assert 'type_ == "yougile"' not in text
        assert 'type_ == "youtrack"' not in text
        assert 'board_type == "yougile"' not in text
        assert 'board_type == "youtrack"' not in text


def test_both_providers_implement_create():
    # контракт Protocol: обе доски умеют создавать задачу с одной сигнатурой
    import inspect

    from reviewer.tasks.boards.yougile import YougileBoard
    from reviewer.tasks.boards.youtrack import YouTrackBoard

    for cls in (YougileBoard, YouTrackBoard):
        sig = inspect.signature(cls.create)
        assert list(sig.parameters) == ["self", "doc_md", "title", "target", "project"]
