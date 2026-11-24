import unittest
try:
    from test_quota_support import run_cli
except ImportError:  # running from the repository root
    from tests.test_quota_support import run_cli  # type: ignore



class CliSurfaceTest(unittest.TestCase):
    def test_help_exposes_the_three_public_commands(self):
        process = run_cli("--help")
        self.assertEqual(process.returncode, 0, process.stderr)
        for command in ("sample", "report", "daily"):
            self.assertIn(command, process.stdout)


if __name__ == "__main__":
    unittest.main()
