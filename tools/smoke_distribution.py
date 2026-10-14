#!/usr/bin/env python3
"""Exercise a built wheel in a fresh venv using ONLY the local mock gateway.

No package index, real API key, remote API or publishing operation is used.
Run from a source checkout or unpacked sdist; Python 3.10+ and venv are required.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv
import zipfile
from email.parser import Parser

ROOT = Path(__file__).resolve().parents[1]
MOCK_KEY = "distribution-test-key-not-a-secret"
KEY_ENV = "OACHECK_DISTRIBUTION_MOCK_KEY"
PROFILE_CHECKS = {
    "basic": ["models", "chat", "stream"],
    "codex": ["models", "responses", "stream"],
    "agent": ["tools", "json_mode", "stream_usage"],
    "full": ["models", "chat", "stream", "stream_usage", "tools", "json_mode", "responses",
             "image", "error_shape", "latency"],
}


def isolated_env() -> dict[str, str]:
    """Keep OS essentials, but never inherit credentials, proxy or Python settings."""
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update({"PYTHONIOENCODING": "utf-8", "NO_PROXY": "127.0.0.1,localhost", KEY_ENV: MOCK_KEY})
    return env


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def run(command: list[str], cwd: Path, env: dict[str, str], expected: int = 0) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120, check=False)
    require(MOCK_KEY not in result.stdout + result.stderr, "The mock credential leaked in command output")
    require(result.returncode == expected,
            f"Command failed (exit {result.returncode}, expected {expected}): "
            + (result.stdout + result.stderr)[:1500])
    return result


def wheel_metadata(path: Path) -> dict[str, str]:
    require(path.is_file() and path.suffix == ".whl", "--wheel must be a local .whl file")
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        require(len(names) == 1, "Expected one METADATA file")
        metadata = Parser().parsestr(archive.read(names[0]).decode("utf-8"))
    require(metadata["Name"] == "openai-compatible-checker", "Unexpected distribution name")
    require(not metadata.get_all("Requires-Dist"), "The checker must remain dependency-free")
    require(bool(metadata["Version"]), "Distribution version is missing")
    return {"name": metadata["Name"], "version": metadata["Version"],
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def load_mock():
    spec = importlib.util.spec_from_file_location("distribution_mock", ROOT / "tests" / "mock_server.py")
    require(spec is not None and spec.loader is not None, "Local mock fixture is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Override any test configuration from the caller with a fixed, harmless value.
    module.RelayMockHandler.api_key = MOCK_KEY
    return module


def write_sample(path: Path, report: dict) -> None:
    """Add conspicuous provenance; leave observed results and timings untouched."""
    sample = {"sample_provenance": {
        "kind": "offline_mock_only",
        "live_service_tested": False,
        "description": "Generated from the repository's loopback mock. Not a live service compatibility result.",
        "reproduce": "python tools/smoke_distribution.py --wheel dist/<wheel>.whl --sample-json <path>",
    }, **report}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sample, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_profile_report(report: dict, profile: str, version: str) -> None:
    expected = PROFILE_CHECKS[profile]
    total = len(expected)
    require(report["tool"] == {"name": "openai-compatible-checker", "version": version},
            "Installed report tool identity/version mismatch")
    require(report["configuration"]["profile"] == profile, "Wrong profile in installed report")
    require(report["configuration"]["checks"] == expected, "Wrong configured checks")
    require([item["name"] for item in report["checks"]] == expected, "Wrong executed checks")
    require(all(item["ok"] is True for item in report["checks"]), "A profile check failed")
    require(report["summary"] == {"checks_total": total, "passed": total, "failed": 0, "status": "ok"},
            f"Unexpected {profile} summary")
    options = report["configuration"]["options"]
    require(options["responses_stream"] is (profile in ("codex", "full")), "Wrong Responses streaming option")
    require(options["tools_roundtrip"] is (profile in ("agent", "full")), "Wrong tools roundtrip option")


def smoke(wheel: Path) -> tuple[dict, dict]:
    metadata = wheel_metadata(wheel)
    mock = load_mock()
    results = []
    with tempfile.TemporaryDirectory(prefix="oacheck-wheel-") as directory:
        scratch = Path(directory)
        environment = scratch / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        env = isolated_env()
        run([str(python), "-I", "-m", "pip", "install", "--no-index", "--no-deps",
             "--no-cache-dir", "--disable-pip-version-check", str(wheel)], scratch, env)
        installed = run([str(python), "-I", "-c",
                         "import openai_compatible_checker as m; print(m.__file__)"], scratch, env)
        require(Path(installed.stdout.strip()).resolve().is_relative_to(environment.resolve()),
                "Import came from the source checkout instead of the isolated wheel installation")
        results.append("isolated_wheel_import")
        entries = [scripts / (name + (".exe" if os.name == "nt" else ""))
                   for name in ("oacheck", "openai-compatible-checker")]
        for entry in entries:
            result = run([str(entry), "--version"], scratch, env)
            require(result.stdout.strip() == f"oacheck {metadata['version']}", "Installed entry point version mismatch")
            listed = run([str(entry), "--list-checks"], scratch, env)
            listing_names = [line.split()[0] for line in listed.stdout.splitlines() if line.startswith("  ")]
            require(all(name in listing_names for name in PROFILE_CHECKS["full"] + ["image_url"]),
                    "Installed entry point returned an incomplete check listing")
            results.append(entry.stem + "_entry_point")

        server, thread = mock.start_server()
        try:
            def probe(mode, extra, expected=0, environment=env, entry=None):
                base = f"http://127.0.0.1:{server.server_port}/{mode}/v1"
                prefix = [str(entry)] if entry else [str(python), "-I", "-m", "openai_compatible_checker"]
                command = [*prefix, "--base-url", base, "--model", "gpt-test", "--api-key-env", KEY_ENV, *extra]
                return run(command, scratch, environment, expected)

            for entry in entries:
                result = probe("ok", ["--profile", "basic"], entry=entry)
                validate_profile_report(json.loads(result.stdout), "basic", metadata["version"])

            report = None
            for profile in PROFILE_CHECKS:
                result = probe("ok", ["--profile", profile, "--runs", "2"])
                current = json.loads(result.stdout)
                validate_profile_report(current, profile, metadata["version"])
                require("assistant reply marker" not in result.stdout and "stream reply marker" not in result.stdout,
                        "Default report included mock response content")
                results.append(profile + "_profile_offline")
                if profile == "full":
                    report = current
            for mode, check, error in (("responses-404", "responses", "not_found"),
                                       ("tool-args-not-json", "tools", "tool_arguments_not_json"),
                                       ("truncated-sse", "stream", "sse_incomplete")):
                result = probe(mode, ["--check", check], expected=1)
                item = json.loads(result.stdout)["checks"][0]
                require(item["ok"] is False and item["error"]["kind"] == error, "Negative fixture was misclassified")
                results.append(mode + "_rejected")
            echoed = probe("echo-key", ["--check", "chat", "--include-content"])
            require("[REDACTED]" in echoed.stdout, "Reflected mock credential was not redacted")
            results.append("reflected_mock_key_redacted")
            missing = env.copy()
            missing.pop(KEY_ENV)
            probe("ok", ["--check", "models"], expected=2, environment=missing)
            results.append("missing_key_usage_exit")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    return {"verification_scope": "offline_mock_only", "live_service_tested": False,
            "distribution": metadata, "python": sys.version.split()[0],
            "passed": len(results), "checks": results}, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True, help="Already-built local wheel (never downloaded)")
    parser.add_argument("--sample-json", type=Path, help="Write an explicitly labeled mock report to this path")
    args = parser.parse_args()
    try:
        summary, report = smoke(args.wheel.resolve())
        if args.sample_json:
            write_sample(args.sample_json, report)
        print(json.dumps(summary, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print(f"Offline distribution verification failed: {str(error).replace(MOCK_KEY, '[REDACTED]')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
