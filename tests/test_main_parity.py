import subprocess
import unittest
from pathlib import Path


class MainParityTests(unittest.TestCase):
    def test_receipts_branch_uses_main_event_loop(self):
        repo = Path(__file__).resolve().parents[1]
        try:
            main_version = subprocess.check_output(
                ["git", "show", "main:app/main.py"],
                cwd=repo,
                text=True,
            )
        except Exception as exc:
            self.skipTest(f"git main reference unavailable: {exc}")

        current_version = (repo / "app" / "main.py").read_text()
        self.assertEqual(current_version, main_version)


if __name__ == "__main__":
    unittest.main()
