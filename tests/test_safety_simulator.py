import unittest

from app.safety_simulator import run_default_suite


class SafetySimulatorTests(unittest.TestCase):
    def test_default_suite_passes(self):
        results = run_default_suite()
        failures = [result for result in results if not result.passed]
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
