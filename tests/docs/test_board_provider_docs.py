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


def _skill_section(text: str, skill: str) -> str:
    marker = f"### `{skill}`"
    section = text.split(marker, maxsplit=1)[1]
    return section.split("\n### ", maxsplit=1)[0]


MATRIX_CAPABILITIES = (
    "Sync/pagination",
    "Markdown normalization",
    "Links/subtasks",
    "Attachments",
    "Single read",
    "Discovery",
    "Create/target",
    "Finish/PR link",
    "Write-through",
)


def _matrix_rows(text: str) -> dict[str, list[str]]:
    """Строки таблицы «Capability matrix»: имя провайдера → значения девяти колонок."""
    section = text.split("## Capability matrix", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    rows: dict[str, list[str]] = {}
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or set(stripped) <= set("|-: "):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        rows[cells[0]] = cells[1:]
    return rows


def test_capability_matrix_has_one_complete_row_per_registered_provider():
    """Матрица — строка на провайдера; каждая из девяти колонок заполнена."""
    rows = _matrix_rows(_read("docs/board-providers.md"))

    assert rows.pop("Provider", None) == list(MATRIX_CAPABILITIES)
    registry = default_board_registry()
    documented = {
        registry.get(board_type).setup.label for board_type in registry.registered_types()
    }
    assert set(rows) == documented

    normalization = MATRIX_CAPABILITIES.index("Markdown normalization")
    for label, cells in rows.items():
        assert len(cells) == len(MATRIX_CAPABILITIES), label
        assert all(cells), label
        # Нормализация описания обязана быть названа явно, а не отмечена галочкой.
        assert cells[normalization] != "✓", label


def test_provider_reference_documents_every_registered_credential_env():
    text = _read("docs/board-providers.md")
    registry = default_board_registry()

    for board_type in registry.registered_types():
        spec = registry.get(board_type)
        assert f"## {spec.setup.label}" in text, board_type
        for field in spec.credential_fields:
            assert field.env in text, (board_type, field.env)


def test_provider_reference_records_shared_transport_and_its_known_debt():
    text = _read("docs/board-providers.md")

    section = text.split("## Shared transport", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    for module in ("restbase.py", "pagination.py", "graphql.py", "yfm.py"):
        assert module in section
    assert "Known debt" in section
    assert "out of scope" in section


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


def test_readme_sync_error_hints_use_registry_setup_and_current_config_fallback():
    for rel in ("README.md", "README.ru.md"):
        text = _read(rel)
        sync_section = _skill_section(text, "reviewer_sync-tasks")
        assert "TASK_BOARD_*" not in sync_section
        assert "reviewer init" in sync_section
        assert "reviewer check" in sync_section
        assert "docs/board-providers.md" in sync_section
        assert "non-secret deploy-wide fallback" in text
        assert "configured registry credentials" in text
        assert "Credentials are not returned" in text


def test_readme_solve_workflow_does_not_list_index_task():
    for rel in ("README.md", "README.ru.md"):
        text = _read(rel)
        solve_section = _skill_section(text, "reviewer_solve-task")
        assert "index_task" not in solve_section
