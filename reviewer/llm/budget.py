class BudgetExceeded(RuntimeError):
    pass

class BudgetTracker:
    def __init__(self, max_iterations: int):
        self.max_iterations = max_iterations
        self.iterations = 0

    def tick(self) -> None:
        if self.iterations >= self.max_iterations:
            raise BudgetExceeded(f"Превышен бюджет итераций ({self.max_iterations})")
        self.iterations += 1
