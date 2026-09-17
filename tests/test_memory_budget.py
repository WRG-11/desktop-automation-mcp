import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from desktop_automation_mcp import errors
from desktop_automation_mcp.memory_budget import InFlightMemoryBudget


class InFlightMemoryBudgetTests(unittest.TestCase):
    def test_reservation_is_released_after_success_and_failure(self):
        budget = InFlightMemoryBudget(100)
        with budget.reserve(60):
            self.assertEqual(budget.in_use, 60)
        self.assertEqual(budget.in_use, 0)

        with self.assertRaisesRegex(RuntimeError, "boom"):
            with budget.reserve(60):
                raise RuntimeError("boom")
        self.assertEqual(budget.in_use, 0)

    def test_concurrent_overcommit_is_rejected_with_stable_code(self):
        budget = InFlightMemoryBudget(100)
        with budget.reserve(60):
            with self.assertRaises(errors.MemoryBudgetExceededError) as ctx:
                with budget.reserve(50):
                    self.fail("overcommitted reservation must not run")
        self.assertIsInstance(ctx.exception, errors.PolicyDeniedError)
        self.assertEqual(ctx.exception.code, errors.MEMORY_BUDGET_EXCEEDED)
        self.assertEqual(budget.in_use, 0)


if __name__ == "__main__":
    unittest.main()
