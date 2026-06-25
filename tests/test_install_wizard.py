"""Тесты wizard-групп installer — проверяют набор ключей в WIZARD_GROUPS."""
from reviewer.install import WIZARD_GROUPS


def _keys():
    return {f.key for g in WIZARD_GROUPS for f in g.fields}


def test_wizard_has_per_type_board_creds():
    keys = _keys()
    assert "YOUGILE_API_KEY" in keys
    assert "YOUTRACK_TOKEN" in keys
    assert "YOUTRACK_BASE_URL" in keys


def test_wizard_keeps_board_selectors():
    keys = _keys()
    # TASK_BOARD_TYPE устарел и удалён из wizard; TYPE задаётся в .review.yml через task_board.type
    assert "TASK_BOARD_MCP" in keys
    assert "TASK_BOARD_KEY_PATTERN" in keys


def test_wizard_has_gitlab_vcs_fields():
    keys = _keys()
    assert "GITLAB_TOKEN" in keys
    assert "GITLAB_URL" in keys
    assert "VCS_PROVIDER" in keys


def test_wizard_has_web_admin_fields():
    keys = _keys()
    assert "WEB_ADMIN_USER" in keys
    assert "WEB_ADMIN_PASSWORD" in keys


def test_wizard_has_yougile_api_base():
    assert "YOUGILE_API_BASE" in _keys()


def test_wizard_total_field_count():
    total = sum(len(g.fields) for g in WIZARD_GROUPS)
    assert total == 20, f"Expected 20 fields, got {total}"
