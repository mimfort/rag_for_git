"""Контракт пользовательской документации расширяемых провайдеров досок."""

import re
from pathlib import Path

from dotenv import dotenv_values

from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.config.settings import Settings
from reviewer.config.task_board import normalize_task_board_config
from reviewer.tasks.boards.registry import default_board_registry


ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_authoritative_provider_reference_has_full_capability_matrix():
    text = _read("docs/board-providers.md")

    assert "| Capability | YouGile | YouTrack | Jira Cloud |" in text
    expected_rows = {
        "Markdown normalization": "| Markdown normalization | HTML↔MD | Native MD | ADF↔MD |",
    }
    for capability in (
        "Sync/pagination",
        "Markdown normalization",
        "Links/subtasks",
        "Attachments",
        "Single read",
        "Discovery",
        "Create/target",
        "Finish/PR link",
        "Write-through",
    ):
        assert expected_rows.get(capability, f"| {capability} | ✓ | ✓ | ✓ |") in text


def test_provider_reference_documents_safe_credentials_and_jira_cloud_boundary():
    text = _read("docs/board-providers.md")

    for provider, fields in {
        "YouGile": ("YOUGILE_API_KEY", "YOUGILE_API_BASE"),
        "YouTrack": ("YOUTRACK_TOKEN", "YOUTRACK_BASE_URL"),
        "Jira Cloud": ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"),
    }.items():
        assert provider in text
        assert all(field in text for field in fields)
    assert "hidden input" in text
    assert "reviewer check" in text
    assert "rotation" in text.lower()
    assert "direct site URL" in text
    assert "without scopes" in text
    assert "api.atlassian.com/ex/jira" in text


def test_provider_reference_keeps_yougile_oidc_distinct_from_rest_auth():
    text = _read("docs/board-providers.md")

    oidc_section = text.split("## YouGile", maxsplit=1)[1].split("## ", maxsplit=1)[0]
    assert "OpenID Connect" in oidc_section
    assert "not an authorization flow for REST integrations" in oidc_section
    assert not re.search(r"REST\s+OAuth|OAuth\s+for\s+REST", oidc_section, re.IGNORECASE)


def test_generic_config_and_legacy_mapping_are_explicit_and_time_bounded():
    text = _read("docs/board-providers.md")

    assert "type: <registered-provider>" in text
    assert "create_target:" in text and "done_target:" in text and "options:" in text
    assert "done_column" in text and "done_state" in text and "status_field" in text
    assert "done_target" in text and "options.status_field" in text
    assert "one compatibility release" in text
    assert "no earlier than the next breaking release" in text
    assert "future breaking-cleanup task" in text


def test_extension_checklist_requires_complete_registration_and_contract_fixture():
    text = _read("docs/board-providers.md")

    checklist = text.split("## Adding a provider", maxsplit=1)[1]
    for required in (
        "adapter",
        "immutable spec",
        "explicit registry",
        "full contract fixture",
        "provider-specific tests",
        "documentation matrix row",
        "partial registration",
    ):
        assert required in checklist.lower()


def test_public_docs_use_registered_provider_terminology_not_a_closed_choice():
    closed_choice = re.compile(
        r"(?:type|board_type)\s*[:=].*(?:yougile\s*\|\s*youtrack|youtrack\s*\|\s*yougile)",
        re.IGNORECASE,
    )
    for rel in ("README.md", "README.ru.md", "CLAUDE.md", ".env.example", ".review.yml"):
        assert not closed_choice.search(_read(rel)), rel


def test_env_template_leaves_all_registry_credentials_unconfigured(monkeypatch):
    registry = default_board_registry()
    credential_envs = {
        env
        for board_type in registry.registered_types()
        for field in registry.get(board_type).credential_fields
        for env in (field.env, *field.aliases)
    }
    for env in credential_envs:
        monkeypatch.delenv(env, raising=False)

    template = ROOT / ".env.example"
    values = dotenv_values(template)
    assert all(values.get(env, "") == "" for env in credential_envs)
    assert not re.search(r"^[A-Z][A-Z0-9_]*=[ \t]+#", _read(".env.example"), re.MULTILINE)

    settings = Settings(_env_file=template)
    source = ProviderCredentialSource.from_settings(settings)
    assert registry.configured_types(source) == ()


def test_readmes_document_store_first_server_side_board_workflow_symmetrically():
    required = (
        "sync_board",
        "get_task(key, project=...)",
        "store-first",
        "tasks:<type>:<board>",
        "TASK_BOARD_API_KEY → YOUGILE_API_KEY",
        "TASK_BOARD_API_BASE → YOUGILE_API_BASE",
        "legacy metadata for older clients",
    )
    forbidden = ("board-mcp", "board mcp", "mcp__<board>", "tasks:<board>")

    for rel in ("README.md", "README.ru.md"):
        text = _read(rel)
        lowered = text.lower()
        for marker in required:
            assert marker.lower() in lowered, (rel, marker)
        for stale in forbidden:
            assert stale not in lowered, (rel, stale)


def test_provider_reference_scopes_legacy_aliases_to_yougile():
    text = _read("docs/board-providers.md")

    assert "TASK_BOARD_API_KEY → YOUGILE_API_KEY" in text
    assert "TASK_BOARD_API_BASE → YOUGILE_API_BASE" in text
    assert "only for YouGile" in text
    assert "new generic fields win" in text.lower()
    assert "warning" in text.lower()


def test_root_review_yml_is_parseable_generic_config_with_key_pattern():
    import yaml

    data = yaml.safe_load(_read(".review.yml"))
    config = normalize_task_board_config(data["task_board"])

    assert config is not None
    assert config.key_pattern == r"PRI-\d+"
    assert config.url_template is None
