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
    assert "TASK_BOARD_TYPE" in keys
    assert "TASK_BOARD_KEY_PATTERN" in keys
