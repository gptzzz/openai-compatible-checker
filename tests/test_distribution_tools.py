"""Pure regression tests for offline distribution verification; no network or builds."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location(
    "smoke_distribution", Path(__file__).resolve().parents[1] / "tools" / "smoke_distribution.py"
)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class DistributionToolsTests(unittest.TestCase):
    def test_child_environment_drops_secrets_proxies_and_python_path(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "private", "HTTP_PROXY": "http://proxy.invalid",
                                    "PYTHONPATH": "/tmp/source", "GITHUB_STEP_SUMMARY": "/tmp/summary",
                                    "PATH": "/bin"}, clear=True):
            env = smoke.isolated_env()
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env[smoke.KEY_ENV], smoke.MOCK_KEY)
        for key in ("OPENAI_API_KEY", "HTTP_PROXY", "PYTHONPATH", "GITHUB_STEP_SUMMARY"):
            self.assertNotIn(key, env)

    def test_success_and_failure_exit_codes_are_enforced(self):
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            with self.assertRaisesRegex(ValueError, "expected 0"):
                smoke.run([], Path.cwd(), {})
            self.assertEqual(smoke.run([], Path.cwd(), {}, expected=1).returncode, 1)

    def test_mock_secret_in_stderr_or_stdout_is_rejected_without_printing_it(self):
        for stdout, stderr in ((smoke.MOCK_KEY, ""), ("", smoke.MOCK_KEY)):
            with self.subTest(stdout=bool(stdout)), patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout, stderr)
            ):
                with self.assertRaisesRegex(ValueError, "credential leaked") as caught:
                    smoke.run([], Path.cwd(), {})
                self.assertNotIn(smoke.MOCK_KEY, str(caught.exception))

    def test_metadata_checks_distribution_and_dependency_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.whl"
            for name, deps, valid in (("openai-compatible-checker", "", True),
                                      ("another-project", "", False),
                                      ("openai-compatible-checker", "Requires-Dist: requests\n", False)):
                with self.subTest(name=name, deps=deps):
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr("test.dist-info/METADATA", f"Name: {name}\nVersion: 2.0.0\n{deps}")
                    if valid:
                        self.assertEqual(smoke.wheel_metadata(path)["version"], "2.0.0")
                    else:
                        with self.assertRaises(ValueError):
                            smoke.wheel_metadata(path)

    def test_profile_validation_rejects_false_passes(self):
        checks = smoke.PROFILE_CHECKS["codex"]
        report = {
            "tool": {"name": "openai-compatible-checker", "version": "2.0.0"},
            "configuration": {"profile": "codex", "checks": checks,
                              "options": {"responses_stream": True, "tools_roundtrip": False}},
            "checks": [{"name": name, "ok": True} for name in checks],
            "summary": {"checks_total": 3, "passed": 3, "failed": 0, "status": "ok"},
        }
        smoke.validate_profile_report(report, "codex", "2.0.0")
        def wrong_profile(value):
            value["configuration"]["profile"] = "basic"
        def wrong_checks(value):
            value["checks"] = [{"name": name, "ok": True} for name in smoke.PROFILE_CHECKS["basic"]]
        def hidden_failure(value):
            value["checks"][0]["ok"] = False
        def wrong_option(value):
            value["configuration"]["options"]["responses_stream"] = False
        def wrong_version(value):
            value["tool"]["version"] = "20.0.0"
        for mutate in (wrong_profile, wrong_checks, hidden_failure, wrong_option, wrong_version):
            with self.subTest(case=mutate.__name__):
                changed = copy.deepcopy(report)
                mutate(changed)
                with self.assertRaises(ValueError):
                    smoke.validate_profile_report(changed, "codex", "2.0.0")

    def test_sample_is_explicitly_offline_and_preserves_measurements(self):
        report = {"summary": {"status": "ok"}, "checks": [{"latency_ms": 1.234}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.json"
            smoke.write_sample(path, report)
            sample = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(sample["sample_provenance"]["kind"], "offline_mock_only")
        self.assertFalse(sample["sample_provenance"]["live_service_tested"])
        self.assertEqual(sample["checks"], report["checks"])
        self.assertNotIn("sample_provenance", report)


if __name__ == "__main__":
    unittest.main()
