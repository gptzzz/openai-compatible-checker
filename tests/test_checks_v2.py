"""Tests for the checks added in 2.0. Each check has a passing case and at least three failures."""

import json
import tempfile
import unittest
from pathlib import Path

from cli_support import TEST_KEY, CLITestCase
from mock_server import RelayMockHandler

import openai_compatible_checker as checker


class StreamUsageTests(CLITestCase):
    def test_usage_chunk_before_done_passes(self):
        result = self.assert_passes("stream_usage")
        self.assertEqual(result["usage"]["total_tokens"], 10)
        self.assertTrue(result["usage_chunk_choices_empty"])
        self.assertNotIn("notes", result)
        sent = RelayMockHandler.requests_seen[-1]["payload"]
        self.assertEqual(sent["stream_options"], {"include_usage": True})

    def test_usage_chunk_variants_pass_with_notes(self):
        without_choices = self.assert_passes("stream_usage", "usage-no-choices")
        self.assertIn(checker.NOTE_USAGE_WITHOUT_CHOICES, without_choices["notes"])
        with_choices = self.assert_passes("stream_usage", "usage-with-choices")
        self.assertFalse(with_choices["usage_chunk_choices_empty"])
        self.assertTrue(any("non-empty choices" in note for note in with_choices["notes"]))

    def test_usage_failures(self):
        self.assert_fails("stream_usage", "no-usage", "usage_missing")
        self.assert_fails("stream_usage", "usage-after-done", "usage_missing")
        self.assert_fails("stream_usage", "bad-usage", "usage_invalid")
        self.assert_fails("stream_usage", "truncated-sse", "sse_incomplete")

    def test_plain_stream_does_not_request_usage_and_reports_ttft(self):
        result = self.assert_passes("stream")
        self.assertNotIn("stream_options", RelayMockHandler.requests_seen[-1]["payload"])
        self.assertIsNotNone(result["ttft_ms"])
        self.assertGreaterEqual(result["ttft_ms"], result["first_event_ms"])


class ToolsTests(CLITestCase):
    def test_tool_call_passes_and_request_shape_is_standard(self):
        result = self.assert_passes("tools")
        self.assertEqual(result["tool_call_count"], 1)
        self.assertEqual(result["finish_reason"], "tool_calls")
        self.assertEqual(result["argument_keys"], ["city"])
        self.assertTrue(result["city_argument_present"])
        self.assertTrue(result["tool_call_ids_have_call_prefix"])
        self.assertNotIn("roundtrip", result)
        sent = RelayMockHandler.requests_seen[-1]["payload"]
        self.assertEqual(sent["tools"][0]["function"]["name"], "get_weather")
        self.assertEqual(sent["tool_choice"], "auto")

    def test_tool_call_failures(self):
        cases = [
            ("tool-args-not-json", "tool_arguments_not_json"),
            ("tool-args-object", "tool_arguments_not_string"),
            ("tool-no-id", "tool_call_missing_id"),
            ("tool-no-call", "no_tool_call"),
            ("tool-finish-stop", "unexpected_finish_reason"),
            ("tool-wrong-name", "tool_call_wrong_name"),
            ("server-error", "server_error"),
        ]
        for mode, kind in cases:
            with self.subTest(mode=mode):
                self.assert_fails("tools", mode, kind)

    def test_legacy_function_call_is_named_in_the_error(self):
        result = self.assert_fails("tools", "tool-legacy-function-call", "no_tool_call")
        self.assertIn("function_call", result["error"]["message"])

    def test_roundtrip_sends_tool_result_and_checks_final_answer(self):
        result = self.assert_passes("tools", "ok", "--tools-roundtrip")
        self.assertTrue(result["roundtrip"]["ok"])
        self.assertTrue(result["roundtrip"]["mentions_tool_result"])
        follow_up = RelayMockHandler.requests_seen[-1]["payload"]
        roles = [message["role"] for message in follow_up["messages"]]
        self.assertEqual(roles, ["user", "assistant", "tool"])
        self.assertEqual(follow_up["messages"][2]["tool_call_id"], "call_mock_1")
        self.assertEqual(follow_up["messages"][1]["tool_calls"][0]["id"], "call_mock_1")

    def test_roundtrip_failures(self):
        cases = [
            ("roundtrip-400", "bad_request"),
            ("roundtrip-loop", "roundtrip_no_final_answer"),
            ("roundtrip-empty", "roundtrip_no_final_answer"),
        ]
        for mode, kind in cases:
            with self.subTest(mode=mode):
                result = self.assert_fails("tools", mode, kind, "--tools-roundtrip")
                self.assertEqual(result["stage"], "roundtrip")
                self.assertFalse(result["roundtrip"]["ok"])
                self.assertEqual(result["tool_call_count"], 1)


class JsonModeTests(CLITestCase):
    def test_json_object_passes(self):
        result = self.assert_passes("json_mode")
        self.assertEqual(result["json_keys"], ["n", "status"])
        sent = RelayMockHandler.requests_seen[-1]["payload"]
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        # OpenAI requires the word JSON somewhere in the messages when JSON mode is on.
        self.assertIn("JSON", json.dumps(sent["messages"]))

    def test_json_mode_failures(self):
        self.assert_fails("json_mode", "json-not-json", "invalid_json_output")
        fenced = self.assert_fails("json_mode", "json-fenced", "invalid_json_output")
        self.assertIn("code fence", fenced["error"]["message"])
        self.assert_fails("json_mode", "json-array", "json_output_not_object")
        self.assert_fails("json_mode", "json-400", "bad_request")


class ResponsesTests(CLITestCase):
    def test_non_streaming_responses_passes(self):
        result = self.assert_passes("responses")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"]["input_tokens"], 9)
        self.assertEqual(result["output_item_types"], ["message"])
        self.assertNotIn("stream", result)
        sent = RelayMockHandler.requests_seen[-1]
        self.assertTrue(sent["path"].endswith("/v1/responses"))
        self.assertFalse(sent["payload"]["store"])

    def test_non_streaming_failures(self):
        cases = [
            ("responses-404", "not_found"),
            ("responses-incomplete", "response_not_completed"),
            ("responses-no-text", "invalid_schema"),
            ("responses-no-usage", "usage_missing"),
            ("responses-float-usage", "usage_invalid"),
        ]
        for mode, kind in cases:
            with self.subTest(mode=mode):
                self.assert_fails("responses", mode, kind)

    def test_output_text_only_gets_a_hint(self):
        result = self.assert_fails("responses", "responses-output-text-only", "invalid_schema")
        self.assertIn("SDK convenience", result["error"]["message"])

    def test_streaming_responses_passes_like_codex_expects(self):
        result = self.assert_passes("responses", "ok", "--responses-stream")
        stream = result["stream"]
        self.assertTrue(stream["ok"])
        self.assertTrue(stream["response_completed"])
        self.assertEqual(stream["output_text_delta_count"], 2)
        self.assertEqual(stream["output_item_done_types"], ["message"])
        self.assertEqual(stream["usage"]["total_tokens"], 11)
        self.assertIsNotNone(stream["ttft_ms"])
        self.assertTrue(RelayMockHandler.requests_seen[-1]["payload"]["stream"])

    def test_streaming_failures(self):
        cases = [
            ("rs-no-completed", "sse_incomplete"),
            ("rs-failed", "response_failed"),
            ("rs-incomplete", "response_not_completed"),
            ("rs-json", "unexpected_content_type"),
            ("rs-bad-usage", "usage_invalid"),
            ("rs-no-item", "invalid_sse_schema"),
            ("rs-no-id", "invalid_sse_schema"),
            ("rs-error-event", "api_error"),
        ]
        for mode, kind in cases:
            with self.subTest(mode=mode):
                result = self.assert_fails("responses", mode, kind, "--responses-stream")
                self.assertEqual(result["stage"], "stream")
                self.assertFalse(result["stream"]["ok"])
                self.assertEqual(result["status"], "completed")


class ImageTests(CLITestCase):
    def test_data_url_png_passes(self):
        result = self.assert_passes("image")
        self.assertTrue(result["color_named"])
        self.assertEqual(result["image_bytes"], len(checker.PROBE_PNG))
        part = RelayMockHandler.requests_seen[-1]["payload"]["messages"][0]["content"][1]
        self.assertEqual(part["type"], "image_url")
        self.assertTrue(part["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_data_url_is_not_copied_into_the_report(self):
        result = self.run_cli("--base-url", self.base_url("image-400"), "--model", "gpt-test", "--check", "image")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(checker.PROBE_PNG_DATA_URL[:60], result.stdout)

    def test_image_failures(self):
        self.assert_fails("image", "image-400", "bad_request")
        self.assert_fails("image", "image-empty", "empty_content")
        self.assert_fails("image", "image-html", "unexpected_content_type")

    def test_unnamed_color_is_a_note_not_a_failure(self):
        result = self.assert_passes("image", "image-no-color")
        self.assertFalse(result["color_named"])
        self.assertTrue(result["notes"])

    def test_remote_url_is_added_after_image_and_tested(self):
        remote = "https://images.example.com/red.png?sig=abc123secret"
        result = self.run_cli(
            "--base-url",
            self.base_url("image-url-400"),
            "--model",
            "gpt-test",
            "--check",
            "image",
            "--image-url",
            remote,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        report = self.load_stdout_report(result)
        self.assertEqual([item["name"] for item in report["checks"]], ["image", "image_url"])
        self.assertTrue(report["checks"][0]["ok"])
        self.assertEqual(report["checks"][1]["error"]["kind"], "bad_request")
        self.assertEqual(report["configuration"]["options"]["image_url_host"], "images.example.com")
        self.assertNotIn("abc123secret", result.stdout)
        self.assertEqual(
            RelayMockHandler.requests_seen[-1]["payload"]["messages"][0]["content"][1]["image_url"]["url"], remote
        )

    def test_remote_url_failures(self):
        remote = ("--image-url", "https://images.example.com/red.png")
        self.assert_fails("image_url", "image-url-400", "bad_request", *remote)
        self.assert_fails("image_url", "image-empty", "empty_content", *remote)
        self.assert_fails("image_url", "image-html", "unexpected_content_type", *remote)

    def test_remote_url_passes_and_requires_the_flag(self):
        passed = self.assert_passes("image_url", "ok", "--image-url", "https://images.example.com/red.png")
        self.assertEqual(passed["image_url_host"], "images.example.com")
        missing = self.run_cli("--base-url", self.base_url(), "--model", "gpt-test", "--check", "image_url")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--image-url is required", missing.stderr)
        bad = self.run_cli(
            "--base-url",
            self.base_url(),
            "--model",
            "gpt-test",
            "--check",
            "image",
            "--image-url",
            "file:///etc/passwd",
        )
        self.assertEqual(bad.returncode, 2)
        self.assertIn("http:// or https://", bad.stderr)


class ErrorShapeTests(CLITestCase):
    def assert_shape(self, mode, shape):
        result = self.run_cli("--base-url", self.base_url(mode), "--check", "error_shape")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        check = self.load_stdout_report(result)["checks"][0]
        self.assertTrue(check["ok"])
        self.assertEqual(check["status_code"], 401)
        self.assertEqual(check["error_shape"], shape)
        # The probe must use the fixed fake key, never the user's key.
        self.assertEqual(RelayMockHandler.requests_seen[-1]["authorization"], f"Bearer {checker.INVALID_PROBE_KEY}")
        self.assertNotIn(TEST_KEY, json.dumps(RelayMockHandler.requests_seen))
        return check

    def test_shapes_are_reported_not_failed(self):
        nested = self.assert_shape("ok", "openai_nested")
        self.assertEqual(nested["error_code"], "invalid_api_key")
        flat = self.assert_shape("flat-error", "flat")
        self.assertEqual(flat["error_code"], "INVALID_API_KEY")
        self.assertTrue(any("flat" in note for note in flat["notes"]))
        self.assert_shape("string-error", "other")

    def test_error_shape_failures(self):
        cases = [
            ("html-error", "error_body_not_json"),
            ("no-auth", "auth_not_enforced"),
            ("server-error", "server_error"),
            ("redirect", "redirect_rejected"),
            # A CDN block page is JSON (or plain text) with 403, but it never reached the API.
            ("cdn-block", "blocked_by_cdn"),
            ("cdn-block-json", "blocked_by_cdn"),
        ]
        for mode, kind in cases:
            with self.subTest(mode=mode):
                result = self.run_cli("--base-url", self.base_url(mode), "--check", "error_shape")
                self.assertEqual(result.returncode, 1)
                check = self.load_stdout_report(result)["checks"][0]
                self.assertEqual(check["error"]["kind"], kind)


class LatencyTests(CLITestCase):
    def test_sequential_runs_report_percentiles(self):
        result = self.assert_passes("latency", "ok", "--runs", "4")
        self.assertEqual(result["runs"], 4)
        self.assertEqual(result["succeeded"], 4)
        self.assertEqual(result["error_rate"], 0)
        self.assertEqual(result["ttft_ms"]["count"], 4)
        self.assertLessEqual(result["ttft_ms"]["p50"], result["ttft_ms"]["p95"])
        self.assertLessEqual(result["total_ms"]["p95"], result["total_ms"]["max"])
        self.assertEqual(len(result["samples"]), 4)
        self.assertEqual(len(RelayMockHandler.requests_seen), 4)

    def test_failed_runs_fail_the_check_but_keep_statistics(self):
        result = self.assert_fails("latency", "flaky", "latency_errors", "--runs", "4")
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["error_rate"], 0.5)
        self.assertEqual(result["errors"], {"server_error": 2})
        self.assertEqual(result["total_ms"]["count"], 2)
        all_failed = self.assert_fails("latency", "server-error", "latency_errors", "--runs", "2")
        self.assertIsNone(all_failed["ttft_ms"]["p50"])
        self.assert_fails("latency", "truncated-sse", "latency_errors", "--runs", "2")

    def test_runs_are_bounded(self):
        for value in ("0", "51"):
            with self.subTest(runs=value):
                result = self.run_cli(
                    "--base-url", self.base_url(), "--model", "gpt-test", "--check", "latency", "--runs", value
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("runs must be between 1 and 50", result.stderr)


class ProfileAndOutputTests(CLITestCase):
    def test_profiles_select_the_documented_checks(self):
        expected = {
            "codex": ["models", "responses", "stream"],
            "agent": ["tools", "json_mode", "stream_usage"],
        }
        for profile, names in expected.items():
            with self.subTest(profile=profile):
                result = self.run_cli("--base-url", self.base_url(), "--model", "gpt-test", "--profile", profile)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = self.load_stdout_report(result)
                self.assertEqual([item["name"] for item in report["checks"]], names)
                self.assertEqual(report["configuration"]["profile"], profile)
        codex = self.load_stdout_report(
            self.run_cli("--base-url", self.base_url(), "--model", "gpt-test", "--profile", "codex")
        )
        self.assertTrue(codex["checks"][1]["stream"]["ok"])
        agent = self.load_stdout_report(
            self.run_cli(
                "--base-url", self.base_url(), "--model", "gpt-test", "--profile", "agent", "--no-tools-roundtrip"
            )
        )
        self.assertNotIn("roundtrip", agent["checks"][0])

    def test_full_profile_passes_and_never_leaks_the_key(self):
        result = self.run_cli("--base-url", self.base_url(), "--model", "gpt-test", "--profile", "full", "--runs", "2")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        report = self.load_stdout_report(result)
        self.assertEqual(report["summary"]["status"], "ok")
        self.assertEqual(len(report["checks"]), len(checker.PROFILES["full"]))
        self.assertTrue(report["checks"][4]["roundtrip"]["ok"])
        self.assertIn("does not prove", report["scope"])
        self.assertNotIn(TEST_KEY, result.stdout + result.stderr)

    def test_profile_and_check_are_mutually_exclusive(self):
        result = self.run_cli(
            "--base-url", self.base_url(), "--model", "gpt-test", "--profile", "full", "--check", "models"
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("either --profile or --check", result.stderr)

    def test_model_is_required_only_for_model_checks(self):
        ok = self.run_cli("--base-url", self.base_url(), "--check", "models", "--check", "error_shape")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        missing = self.run_cli("--base-url", self.base_url(), "--profile", "agent")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--model is required for the selected checks: tools, json_mode, stream_usage", missing.stderr)

    def test_base_url_must_not_be_the_responses_endpoint(self):
        result = self.run_cli("--base-url", "https://api.example.com/v1/responses", "--check", "models")
        self.assertEqual(result.returncode, 2)
        self.assertIn("API prefix", result.stderr)

    def test_markdown_report(self):
        result = self.run_cli(
            "--base-url",
            self.base_url("json-array"),
            "--model",
            "gpt-test",
            "--check",
            "models",
            "--check",
            "json_mode",
            "--format",
            "markdown",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        text = result.stdout
        self.assertIn("## OpenAI-compatible API check: 1/2 passed (degraded)", text)
        self.assertIn("| models | PASS | 200 |", text)
        self.assertIn("| json\\_mode | FAIL | 200 |", text)
        self.assertIn("json\\_output\\_not\\_object", text)
        self.assertIn("does not prove", text)
        self.assertNotIn(TEST_KEY, text)

    def test_step_summary_is_appended_in_actions_and_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = Path(temp_dir) / "summary.md"
            summary.write_text("previous step\n", encoding="utf-8")
            result = self.run_cli(
                "--base-url",
                self.base_url(),
                "--check",
                "models",
                env_overrides={"GITHUB_STEP_SUMMARY": str(summary)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            content = summary.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("previous step\n"))
            self.assertIn("| models | PASS |", content)
            self.load_stdout_report(result)  # stdout still carries the JSON report

            before = summary.read_text(encoding="utf-8")
            self.run_cli(
                "--base-url",
                self.base_url(),
                "--check",
                "models",
                "--no-step-summary",
                env_overrides={"GITHUB_STEP_SUMMARY": str(summary)},
            )
            self.assertEqual(summary.read_text(encoding="utf-8"), before)

    def test_list_checks_needs_no_arguments(self):
        result = self.run_cli("--list-checks", key=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in checker.ALL_CHECKS:
            self.assertIn(name, result.stdout)
        self.assertIn("codex", result.stdout)

    def test_legacy_script_name_still_works(self):
        from cli_support import LEGACY_CHECKER

        result = self.run_cli("--base-url", self.base_url(), "--model", "gpt-test", script=LEGACY_CHECKER)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.load_stdout_report(result)
        self.assertEqual(report["summary"]["passed"], 3)
        self.assertIn("renamed", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
