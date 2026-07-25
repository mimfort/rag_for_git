"""Общий contract-набор на фейке kaiten (постоянная параметризация — в фазе C)."""
from __future__ import annotations

import pytest

from tests.tasks.boards.contract import ProviderContract
from tests.tasks.boards.fakes import kaiten as fake


@pytest.mark.parametrize("adapter", [fake.ADAPTER], indirect=True)
class TestKaitenContract(ProviderContract):
    pass
