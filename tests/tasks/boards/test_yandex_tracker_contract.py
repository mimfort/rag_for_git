"""Общий contract-набор на фейке yandex_tracker (постоянная параметризация — в фазе C)."""
from __future__ import annotations

import pytest

from tests.tasks.boards.contract import ProviderContract
from tests.tasks.boards.fakes import yandex_tracker as fake


@pytest.mark.parametrize("adapter", [fake.ADAPTER], indirect=True)
class TestYandexTrackerContract(ProviderContract):
    pass
