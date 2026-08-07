from dataclasses import FrozenInstanceError

import pytest

from reviewer.config import task_board
from reviewer.config.task_board import normalize_task_board_config


def test_normalizes_generic_task_board_form_without_mutating_input():
    raw = {
        "type": "youtrack",
        "mcp": "youtrack",
        "project": "PRI",
        "key_pattern": "PRI-\\d+",
        "url_template": "https://youtrack.example/issue/{code}",
        "create_target": "Open",
        "done_target": "Done",
        "options": {"status_field": "Stage"},
    }

    config = normalize_task_board_config(raw)

    assert config is not None
    assert config.board_type == "youtrack"
    assert config.mcp == "youtrack"
    assert config.project == "PRI"
    assert config.key_pattern == "PRI-\\d+"
    assert config.url_template == "https://youtrack.example/issue/{code}"
    assert config.create_target == "Open"
    assert config.done_target == "Done"
    assert config.options == {"status_field": "Stage"}
    assert config.warnings == ()
    assert raw == {
        "type": "youtrack",
        "mcp": "youtrack",
        "project": "PRI",
        "key_pattern": "PRI-\\d+",
        "url_template": "https://youtrack.example/issue/{code}",
        "create_target": "Open",
        "done_target": "Done",
        "options": {"status_field": "Stage"},
    }


def test_empty_task_board_is_disabled():
    assert normalize_task_board_config({}) is None
    assert normalize_task_board_config(None) is None


@pytest.mark.parametrize(
    ("raw_filter", "expected_filter", "expected_sparse"),
    [
        ({}, {"max_age_days": None, "include_archived": True}, {}),
        (
            {"max_age_days": 30},
            {"max_age_days": 30, "include_archived": True},
            {"max_age_days": 30},
        ),
        (
            {"include_archived": False},
            {"max_age_days": None, "include_archived": False},
            {"include_archived": False},
        ),
        (
            {"max_age_days": 90, "include_archived": True},
            {"max_age_days": 90, "include_archived": True},
            {"max_age_days": 90},
        ),
    ],
    ids=("empty", "age-only", "archive-only", "full"),
)
def test_normalizes_and_round_trips_task_sync_filter(
    raw_filter: dict[str, object],
    expected_filter: dict[str, object],
    expected_sparse: dict[str, object],
) -> None:
    config = normalize_task_board_config({
        "type": "yougile",
        "options": {"key_prefix": "PRI"},
        "sync_filter": raw_filter,
    })

    assert config is not None
    assert isinstance(config.sync_filter, task_board.TaskSyncFilter)
    assert config.sync_filter.canonical_dict() == expected_filter
    assert config.sync_filter.as_dict() == expected_sparse
    assert config.options == {"key_prefix": "PRI"}
    assert config.as_dict() == {
        "type": "yougile",
        "options": {"key_prefix": "PRI"},
        "sync_filter": expected_sparse,
    }
    assert normalize_task_board_config(config.as_dict()) == config


def test_absent_task_sync_filter_stays_absent() -> None:
    config = normalize_task_board_config({"type": "yougile"})

    assert config is not None
    assert config.sync_filter is None
    assert "sync_filter" not in config.as_dict()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "30", []])
def test_rejects_invalid_task_sync_filter_max_age_days(value: object) -> None:
    with pytest.raises(ValueError, match="max_age_days"):
        normalize_task_board_config({
            "type": "yougile",
            "sync_filter": {"max_age_days": value},
        })


@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
def test_rejects_invalid_task_sync_filter_include_archived(value: object) -> None:
    with pytest.raises(ValueError, match="include_archived"):
        normalize_task_board_config({
            "type": "yougile",
            "sync_filter": {"include_archived": value},
        })


@pytest.mark.parametrize("value", [None, 1, "recent", []])
def test_rejects_non_mapping_task_sync_filter(value: object) -> None:
    with pytest.raises(ValueError, match="task_board.sync_filter must be a mapping"):
        normalize_task_board_config({"type": "yougile", "sync_filter": value})


def test_rejects_unknown_task_sync_filter_fields() -> None:
    with pytest.raises(ValueError, match="task_board.sync_filter"):
        task_board.normalize_task_sync_filter({"unexpected": True})


def test_task_sync_filter_is_frozen() -> None:
    sync_filter = task_board.TaskSyncFilter()

    with pytest.raises(FrozenInstanceError):
        sync_filter.include_archived = False


def test_rejects_malformed_options():
    with pytest.raises(ValueError, match="task_board.options must be a mapping"):
        normalize_task_board_config({"type": "youtrack", "options": ["Stage"]})


def test_migrates_legacy_task_board_keys():
    config = normalize_task_board_config({
        "type": "youtrack",
        "done_state": "Fixed",
        "status_field": "Stage",
    })

    assert config is not None
    assert config.done_target == "Fixed"
    assert config.options == {"status_field": "Stage"}
    assert config.warnings == (
        "task_board.done_state migrated to done_target",
        "task_board.status_field migrated to options.status_field",
    )


def test_new_values_win_over_legacy_with_warning():
    config = normalize_task_board_config({
        "type": "youtrack",
        "done_target": "Done",
        "done_state": "Fixed",
        "options": {"status_field": "Stage"},
        "status_field": "State",
    })

    assert config is not None
    assert config.done_target == "Done"
    assert config.options == {"status_field": "Stage"}
    assert config.warnings == (
        "task_board.done_state ignored because done_target is set",
        "task_board.status_field ignored because options.status_field is set",
    )


def test_rejects_registry_declared_secret_names_without_echoing_values():
    secret = "do-not-echo-this-value"

    with pytest.raises(ValueError) as error:
        normalize_task_board_config({
            "type": "yougile",
            "options": {"YOUGILE_API_KEY": secret},
        })

    assert secret not in str(error.value)


def test_rejects_secret_key_declared_by_another_registered_provider():
    secret = "do-not-echo-cross-provider-secret"

    with pytest.raises(ValueError) as error:
        normalize_task_board_config({
            "type": "yougile",
            "options": {"YOUTRACK_TOKEN": secret},
        })

    assert secret not in str(error.value)


def test_rejects_secret_key_nested_in_arbitrary_json_lists():
    secret = "do-not-echo-nested-secret"

    with pytest.raises(ValueError) as error:
        normalize_task_board_config({
            "type": "yougile",
            "options": {"nested": [[{"YOUTRACK_TOKEN": secret}]]},
        })

    assert secret not in str(error.value)


def test_rejects_registered_secret_nested_in_task_sync_filter_without_echoing_value():
    secret = "do-not-echo-sync-filter-secret"

    with pytest.raises(ValueError, match="must not contain credentials") as error:
        normalize_task_board_config({
            "type": "yougile",
            "sync_filter": {"nested": [{"YOUTRACK_TOKEN": secret}]},
        })

    assert secret not in str(error.value)
