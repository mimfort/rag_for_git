"""Тесты registry-driven wizard-групп installer."""
from reviewer.install import (
    VCS_SETUPS,
    WIZARD_GROUPS,
    board_env_group,
    common_board_env_fields,
    vcs_env_group,
)
from reviewer.tasks.boards.registry import (
    BoardProviderRegistry,
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderSetupSpec,
)
from tests.provider_access import FAKE_PROVIDER_ACCESS


REMOVED_STANDARD_KEYS = {
    "DEFAULT_REPO",
    "REVIEW_BRANCHES",
    "WEB_ADMIN_USER",
    "WEB_ADMIN_PASSWORD",
    "TASK_BOARD_MCP",
}


def _keys():
    return {f.key for g in WIZARD_GROUPS for f in g.fields}


def test_wizard_has_per_type_board_creds():
    keys = _keys()
    assert "YOUGILE_API_KEY" in keys
    assert "YOUTRACK_TOKEN" in keys
    assert "YOUTRACK_BASE_URL" in keys
    assert "JIRA_BASE_URL" in keys
    assert "JIRA_EMAIL" in keys
    assert "JIRA_API_TOKEN" in keys


def test_wizard_keeps_current_board_selectors_only():
    keys = _keys()
    # TASK_BOARD_TYPE устарел и удалён из wizard; TYPE задаётся в .review.yml через task_board.type
    assert {"TASK_BOARD_KEY_PATTERN", "TASK_BOARD_URL_TEMPLATE"} <= keys
    assert keys.isdisjoint(REMOVED_STANDARD_KEYS)


def test_wizard_has_gitlab_vcs_fields():
    keys = _keys()
    assert "GITLAB_TOKEN" in keys
    assert "GITLAB_URL" in keys
    assert "VCS_PROVIDER" in keys


def test_vcs_catalog_declares_github_and_gitlab_access():
    assert tuple(VCS_SETUPS) == ("github", "gitlab")
    assert [field.key for field in VCS_SETUPS["github"].credential_fields] == [
        "GITHUB_TOKEN"
    ]
    assert [field.key for field in VCS_SETUPS["gitlab"].credential_fields] == [
        "GITLAB_URL",
        "GITLAB_TOKEN",
    ]
    github_permissions = VCS_SETUPS["github"].access.minimum_permissions
    assert "Pull requests: Read and write" in github_permissions
    assert "Contents: Read" in github_permissions
    assert VCS_SETUPS["gitlab"].access.minimum_permissions == "PAT/project token with api scope"


def test_vcs_group_is_canonical_union_with_provider_fallback():
    keys = [field.key for field in vcs_env_group().fields]
    assert keys == ["VCS_PROVIDER", "GITHUB_TOKEN", "GITLAB_URL", "GITLAB_TOKEN"]
    assert len(keys) == len(set(keys))


def test_wizard_has_yougile_api_base():
    assert "YOUGILE_API_BASE" in _keys()


def test_board_group_uses_registry_metadata_without_legacy_aliases():
    registry = BoardProviderRegistry(
        [
            BoardProviderSpec(
                board_type="future",
                factory=lambda _context: object(),
                credential_fields=(
                    CredentialFieldSpec(
                        "FUTURE_TOKEN",
                        "Future token",
                        secret=True,
                        aliases=("OLD_FUTURE_TOKEN",),
                    ),
                    CredentialFieldSpec(
                        "FUTURE_URL",
                        "Future URL",
                        required=False,
                        default="https://future.example",
                    ),
                    CredentialFieldSpec(
                        "TASK_BOARD_SERVICE_TOKEN",
                        "Future service token",
                        secret=True,
                    ),
                ),
                setup=ProviderSetupSpec(
                    "Future",
                    "https://future.example/setup",
                    "Create a token.",
                    FAKE_PROVIDER_ACCESS,
                ),
            )
        ]
    )

    group = board_env_group(registry)
    fields = {field.key: field for field in group.fields}

    assert {"TASK_BOARD_KEY_PATTERN", "TASK_BOARD_URL_TEMPLATE"} <= fields.keys()
    assert "TASK_BOARD_MCP" not in fields
    assert fields["FUTURE_TOKEN"].secret is True
    assert fields["FUTURE_URL"].default == "https://future.example"
    assert fields["TASK_BOARD_SERVICE_TOKEN"].secret is True
    assert "OLD_FUTURE_TOKEN" not in fields
    assert {field.key for field in common_board_env_fields(group)} == {
        "TASK_BOARD_KEY_PATTERN",
        "TASK_BOARD_URL_TEMPLATE",
    }


def test_wizard_field_count_tracks_registry_credentials():
    from reviewer.tasks.boards.registry import default_board_registry

    registry_fields = sum(
        len(default_board_registry().get(board_type).credential_fields)
        for board_type in default_board_registry().registered_types()
    )
    non_board_fields = sum(
        len(group.fields) for group in WIZARD_GROUPS if group.title != "Доска задач"
    )

    assert sum(len(group.fields) for group in WIZARD_GROUPS) == (
        non_board_fields + 2 + registry_fields
    )
