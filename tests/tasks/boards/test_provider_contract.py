"""Contract suite существующих провайдеров досок."""

import pytest

from tests.tasks.boards.contract import (
    ADAPTERS,
    JIRA_READ_ADAPTERS,
    ProviderContract,
    ProviderReadContract,
)


@pytest.mark.parametrize("adapter", ADAPTERS, indirect=True, ids=lambda item: item.board_type)
class TestExistingProviderContract(ProviderContract):
    pass


@pytest.mark.parametrize("adapter", JIRA_READ_ADAPTERS, indirect=True, ids=lambda item: item.board_type)
class TestJiraReadProviderContract(ProviderReadContract):
    pass
