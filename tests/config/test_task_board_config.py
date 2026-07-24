import pytest

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
