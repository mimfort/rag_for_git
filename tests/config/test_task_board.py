from reviewer.config.task_board import migrate_legacy_board_args


def test_new_board_arguments_win_with_deterministic_legacy_warnings():
    target, options, warnings = migrate_legacy_board_args(
        target="new-target",
        provider_options={"status_field": "Stage"},
        done_state="Fixed",
        status_field="State",
        done_column="Done",
    )

    assert target == "new-target"
    assert options == {"status_field": "Stage"}
    assert warnings == [
        "legacy status_field ignored because provider_options.status_field is set",
        "legacy done_column ignored because target is set",
        "legacy done_state ignored because target is set",
    ]


def test_legacy_board_arguments_migrate_to_generic_shape():
    target, options, warnings = migrate_legacy_board_args(
        target=None,
        provider_options=None,
        done_state="Fixed",
        status_field="State",
        done_column=None,
    )

    assert target == "Fixed"
    assert options == {"status_field": "State"}
    assert warnings == [
        "legacy status_field migrated to provider_options.status_field",
        "legacy done_state migrated to target",
    ]
