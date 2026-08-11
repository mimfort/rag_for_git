"""Блок «Окружение»: состав безопасен, урезание работает и не блокирует (PRI-239)."""
from reviewer.bugreport.environment import (
    CLIENT_FIELDS,
    FIELD_ORDER,
    collect_environment,
)
from reviewer.bugreport.sanitize import Sanitizer


def _block(**kwargs):
    return collect_environment(**kwargs)


def test_server_side_fields_are_present_by_default():
    values = _block().values
    for key in ("os", "python", "reviewer_version", "plugin_version", "install_mode"):
        assert key in values


def test_client_fields_are_accepted_and_unknown_keys_are_dropped():
    block = _block(client_fields={
        "orchestrator_model": "opus-5",
        "mode": "subagent",
        "repo_path": "/srv/acme/billing",     # не в CLIENT_FIELDS
    })
    assert block.values["orchestrator_model"] == "opus-5"
    assert "repo_path" not in block.values
    assert "acme" not in block.as_markdown()


def test_client_fields_pass_through_the_sanitizer():
    block = _block(
        client_fields={"cli": "Claude Code в /Users/kate/work/acme"},
        sanitizer=Sanitizer.build(repos=["acme/billing"]),
    )
    assert "kate" not in block.values["cli"]
    assert "acme" not in block.values["cli"]


def test_self_hosted_is_a_fact_never_a_host():
    markdown = _block(vcs_type="gitlab", vcs_self_hosted=True).as_markdown()
    assert "да" in markdown
    assert "http" not in markdown


def test_scale_accepts_only_known_integer_counters():
    block = _block(scale={"clusters": 9, "files": "82", "secret": "acme-plan", "tasks": None})
    rendered = block.values["scale"]
    assert "clusters=9" in rendered and "files=82" in rendered
    assert "acme-plan" not in rendered
    assert "tasks" not in rendered


def test_scale_rejects_non_numeric_values():
    assert "files" not in _block(scale={"files": "много"}).values.get("scale", "")


def test_trimming_excludes_selected_lines():
    block = _block(client_fields={"orchestrator_model": "opus-5", "cli": "Claude Code"})
    trimmed = block.trimmed(exclude=["orchestrator_model"])
    assert "orchestrator_model" not in trimmed.values
    assert "cli" in trimmed.values


def test_trimming_to_nothing_is_allowed_and_renders_explicitly():
    trimmed = _block().trimmed(include=[])
    assert trimmed.values == {}
    assert "исключён пользователем" in trimmed.as_markdown()


def test_field_order_is_canonical():
    block = _block(client_fields={"cli": "Codex", "orchestrator_model": "gpt"},
                   board_type="yougile")
    assert block.keys() == [key for key in FIELD_ORDER if key in block.values]


def test_client_fields_cover_only_what_the_model_alone_knows():
    assert CLIENT_FIELDS <= set(FIELD_ORDER)
    for key in ("reviewer_version", "graph_backend", "board_type", "index_drift"):
        assert key not in CLIENT_FIELDS
