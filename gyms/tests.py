"""Run integration checks in an isolated process with real per-gym databases."""
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import secrets
import unittest


class DatabaseIsolationTests(unittest.TestCase):
    def test_database_per_gym_workflow(self):
        root = Path(__file__).resolve().parent.parent
        token = "test_gym_platform_" + uuid4().hex
        result = subprocess.run(
            [sys.executable, str(root / "tests" / "database_workflow.py")],
            cwd=root,
            env={**os.environ, "PGDATABASE": token, "PGUSER": token,
                 "PGPASSWORD": secrets.token_urlsafe(32)},
            text=True, capture_output=True, timeout=540,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 204 tests", result.stderr)
