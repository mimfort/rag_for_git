"""Contract suite существующих провайдеров досок."""

import pytest

from tests.tasks.boards.contract import ADAPTERS, ProviderContract


@pytest.mark.parametrize("adapter", ADAPTERS, indirect=True, ids=lambda item: item.board_type)
class TestExistingProviderContract(ProviderContract):
    """Применяет общий provider contract ко всем встроенным адаптерам."""
