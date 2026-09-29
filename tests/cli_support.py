"""Shared helpers for CLI tests. Every test talks only to the local mock server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from mock_server import RelayMockHandler, start_server

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
CHECKER = REPO_ROOT / "openai_compatible_checker.py"
LEGACY_CHECKER = REPO_ROOT / "ai_api_relay_checker.py"
TEST_KEY = RelayMockHandler.api_key


class CLITestCase(unittest.TestCase):
    server = None

    @classmethod
    def setUpClass(cls):
        cls.server, cls.thread = start_server()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        RelayMockHandler.requests_seen = []
        RelayMockHandler.counters = {}

    def base_url(self, mode="ok"):
        return f"{self.origin}/{mode}/v1"

    def run_cli(
        self,
        *args,
        key=TEST_KEY,
        env_name="TEST_API_KEY",
        env_overrides=None,
        script=CHECKER,
        timeout=30,
    ):
        env = os.environ.copy()
        # Never let a developer's real key or a CI job summary leak into tests.
        for name in ("OPENAI_API_KEY", env_name, "GITHUB_STEP_SUMMARY"):
            env.pop(name, None)
        if key is not None:
            env[env_name] = key
        env.update(env_overrides or {})
        command = [sys.executable, str(script), "--api-key-env", env_name, *map(str, args)]
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )

    def load_stdout_report(self, result):
        self.assertTrue(result.stdout.strip(), result.stderr)
        return json.loads(result.stdout)

    def run_single(self, check, mode="ok", *extra, expect_code=None):
        """Run one check against a mock mode and return its result object."""
        result = self.run_cli("--base-url", self.base_url(mode), "--model", "gpt-test", "--check", check, *extra)
        report = self.load_stdout_report(result)
        self.assertNotIn(TEST_KEY, result.stdout)
        check_result = report["checks"][0]
        self.assertEqual(check_result["name"], check)
        if expect_code is None:
            expect_code = 0 if check_result["ok"] else 1
        self.assertEqual(result.returncode, expect_code, result.stderr + result.stdout[:2000])
        return check_result

    def assert_fails(self, check, mode, kind, *extra):
        check_result = self.run_single(check, mode, *extra, expect_code=1)
        self.assertFalse(check_result["ok"])
        self.assertEqual(check_result["error"]["kind"], kind, check_result)
        return check_result

    def assert_passes(self, check, mode="ok", *extra):
        check_result = self.run_single(check, mode, *extra, expect_code=0)
        self.assertTrue(check_result["ok"], check_result)
        return check_result
