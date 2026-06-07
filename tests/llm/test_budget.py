import pytest
from reviewer.llm.budget import BudgetTracker, BudgetExceeded

def test_budget_raises_after_max_iterations():
    b = BudgetTracker(max_iterations=2)
    b.tick(); b.tick()
    with pytest.raises(BudgetExceeded):
        b.tick()
