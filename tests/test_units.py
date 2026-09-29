"""Unit tests for pure helpers: percentiles, PNG builder, Markdown escaping, shapes, usage rules."""

import base64
import struct
import unittest
import zlib

import cli_support  # noqa: F401  (puts the repository root on sys.path)
import openai_compatible_checker as checker


class PercentileTests(unittest.TestCase):
    def test_nearest_rank(self):
        values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        self.assertEqual(checker.nearest_rank(values, 50), 50)
        self.assertEqual(checker.nearest_rank(values, 95), 100)
        self.assertEqual(checker.nearest_rank(values, 90), 90)
        self.assertEqual(checker.nearest_rank([7], 95), 7)
        self.assertIsNone(checker.nearest_rank([], 50))

    def test_small_samples_make_p95_the_maximum(self):
        # With 5 runs, ceil(0.95 * 5) = 5, so p95 is the slowest run. docs/latency.md says so.
        self.assertEqual(checker.nearest_rank([5, 1, 4, 2, 3], 95), 5)
        self.assertEqual(checker.nearest_rank([5, 1, 4, 2, 3], 50), 3)

    def test_invalid_percentile(self):
        with self.assertRaises(ValueError):
            checker.nearest_rank([1, 2], 0)

    def test_distribution(self):
        self.assertEqual(
            checker.distribution([3.0, 1.0, 2.0]),
            {"count": 3, "p50": 2.0, "p95": 3.0, "min": 1.0, "max": 3.0},
        )
        self.assertEqual(checker.distribution([])["p50"], None)


class PngTests(unittest.TestCase):
    def test_probe_png_is_a_valid_8x8_rgb_image(self):
        data = checker.PROBE_PNG
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        offset = 8
        chunks = {}
        while offset < len(data):
            (length,) = struct.unpack(">I", data[offset : offset + 4])
            tag = data[offset + 4 : offset + 8]
            body = data[offset + 8 : offset + 8 + length]
            (crc,) = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])
            self.assertEqual(crc, zlib.crc32(tag + body) & 0xFFFFFFFF, tag)
            chunks[tag] = body
            offset += 12 + length
        self.assertEqual(list(chunks), [b"IHDR", b"IDAT", b"IEND"])
        width, height, depth, color_type = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
        self.assertEqual((width, height, depth, color_type), (8, 8, 8, 2))
        pixels = zlib.decompress(chunks[b"IDAT"])
        self.assertEqual(len(pixels), 8 * (1 + 8 * 3))
        self.assertEqual(pixels[1:4], b"\xff\x00\x00")

    def test_data_url_round_trips(self):
        prefix = "data:image/png;base64,"
        self.assertTrue(checker.PROBE_PNG_DATA_URL.startswith(prefix))
        self.assertEqual(base64.b64decode(checker.PROBE_PNG_DATA_URL[len(prefix) :]), checker.PROBE_PNG)


class ErrorShapeTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(checker.classify_error_shape({"error": {"message": "bad key", "code": "x"}}), "openai_nested")
        self.assertEqual(checker.classify_error_shape({"code": "INVALID_API_KEY", "message": "bad key"}), "flat")
        self.assertEqual(checker.classify_error_shape({"type": "auth", "message": "bad key"}), "flat")
        self.assertEqual(checker.classify_error_shape({"error": "bad key"}), "other")
        self.assertEqual(checker.classify_error_shape({"message": "bad key"}), "other")
        self.assertEqual(checker.classify_error_shape(["bad key"]), "other")

    def test_http_status_classes(self):
        self.assertEqual(checker.classify_http_status(400), "bad_request")
        self.assertEqual(checker.classify_http_status(422), "bad_request")
        self.assertEqual(checker.classify_http_status(401), "authentication_error")
        self.assertEqual(checker.classify_http_status(404), "not_found")
        self.assertEqual(checker.classify_http_status(429), "rate_limited")
        self.assertEqual(checker.classify_http_status(502), "server_error")
        self.assertEqual(checker.classify_http_status(302), "redirect_rejected")
        self.assertEqual(checker.classify_http_status(418), "http_error")

    def test_cdn_block_pages_are_recognised_and_api_errors_are_not(self):
        label = checker.cdn_block_label
        self.assertEqual(label({}, b"error code: 1010"), "Cloudflare error 1010")
        self.assertEqual(label({}, b"  error code: 1020\n"), "Cloudflare error 1020")
        cf_json = b'{"title":"Error 1010: Access denied","error_code":1010,"cloudflare_error":true}'
        self.assertEqual(label({}, cf_json), "Cloudflare error 1010")
        self.assertTrue(label({"cf-mitigated": "challenge"}, b"<html></html>").startswith("Cloudflare cf-mitigated"))
        # Ordinary API errors, including the flat and nested auth shapes, are left alone.
        self.assertIsNone(label({}, b'{"code":"INVALID_API_KEY","message":"Invalid API key"}'))
        self.assertIsNone(label({}, b'{"error":{"message":"bad key","code":"invalid_api_key"}}'))
        self.assertIsNone(label({}, b'{"error_code":1010}'))
        self.assertIsNone(label({}, b"error code: 1010 and more text"))
        self.assertIsNone(label(None, b""))


class UsageRuleTests(unittest.TestCase):
    def test_responses_usage_matches_what_codex_parses(self):
        good = {
            "input_tokens": 9,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 2,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 11,
        }
        checker.validate_responses_usage(good, "response")
        checker.validate_responses_usage({"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}, "response")
        bad_cases = [
            {"input_tokens": 1, "output_tokens": 1},
            {"input_tokens": 1.0, "output_tokens": 1, "total_tokens": 2},
            {"input_tokens": True, "output_tokens": 1, "total_tokens": 2},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "input_tokens_details": {}},
            {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "output_tokens_details": {"reasoning_tokens": "0"},
            },
            "not an object",
        ]
        for usage in bad_cases:
            with self.subTest(usage=usage):
                with self.assertRaises(checker.ProbeFailure) as caught:
                    checker.validate_responses_usage(usage, "response")
                self.assertEqual(caught.exception.kind, "usage_invalid")

    def test_normalize_usage_keeps_numbers_and_details_only(self):
        usage = checker.normalize_usage(
            {
                "prompt_tokens": 3,
                "completion_tokens": "x",
                "total_tokens": True,
                "prompt_tokens_details": {"cached_tokens": 2},
            }
        )
        self.assertEqual(usage, {"prompt_tokens": 3, "prompt_tokens_details.cached_tokens": 2})


class SseParsingTests(unittest.TestCase):
    def test_event_names_and_multiline_data(self):
        self.assertEqual(checker.sse_fields(["event: response.created", "data: {}"]), ("response.created", "{}"))
        self.assertEqual(checker.sse_fields(["data: a", "data: b"]), (None, "a\nb"))
        self.assertIsNone(checker.sse_fields([": comment", "event: ping"]))
        self.assertEqual(checker.sse_fields(["data:[DONE]"]), (None, "[DONE]"))


class MarkdownTests(unittest.TestCase):
    def test_cells_cannot_break_the_table_or_inject_markup(self):
        cell = checker.md("a|b <script>x</script> [link](http://evil) `code`\nnext_line *bold*")
        self.assertNotIn("|b", cell.replace("\\|", ""))
        self.assertNotIn("<script>", cell)
        self.assertNotIn("](", cell.replace("\\](", ""))
        self.assertNotIn("\n", cell)
        self.assertNotIn("`", cell)

    def test_render_markdown_from_a_failed_check(self):
        report = {
            "schema_version": "2.0",
            "tool": {"name": "openai-compatible-checker", "version": checker.VERSION},
            "generated_at": "2026-09-29T00:00:00Z",
            "scope": checker.SCOPE_NOTICE,
            "configuration": {"base_url": "https://api.example.com/v1", "model": "m", "profile": None},
            "summary": {"checks_total": 1, "passed": 0, "failed": 1, "status": "failed"},
            "checks": [
                {
                    "name": "tools",
                    "ok": False,
                    "status_code": 200,
                    "latency_ms": 12.6,
                    "stage": "roundtrip",
                    "error": {"kind": "bad_request", "message": "tool round trip: HTTP 400: role|tool"},
                    "roundtrip": {"ok": False, "notes": ["nested note"]},
                }
            ],
        }
        text = checker.render_markdown(report)
        self.assertIn("0/1 passed (failed)", text)
        self.assertIn("| Profile | custom |", text)
        self.assertIn("bad\\_request (stage: roundtrip)", text)
        self.assertIn("role\\|tool", text)
        self.assertIn("tools (roundtrip): nested note", text)
        self.assertIn("13 ms", text)


class RedactionTests(unittest.TestCase):
    def test_known_secret_bearer_and_key_patterns(self):
        text = checker.redact_text(
            "api_key=abc123 Authorization: Bearer tok_987 sk-abcdefghijk mysecretvalue",
            ["mysecretvalue"],
        )
        for leaked in ("abc123", "tok_987", "sk-abcdefghijk", "mysecretvalue"):
            self.assertNotIn(leaked, text)

    def test_redact_tree_covers_keys_and_nested_values(self):
        tree = checker.redact_tree({"a": ["x SECRETKEY y"], "SECRETKEY": 1}, ["SECRETKEY"])
        self.assertEqual(tree, {"a": ["x [REDACTED] y"], "[REDACTED]": 1})


class RegistryTests(unittest.TestCase):
    def test_profiles_only_reference_known_checks(self):
        for name, checks in checker.PROFILES.items():
            with self.subTest(profile=name):
                self.assertTrue(set(checks) <= set(checker.ALL_CHECKS))
        self.assertEqual(set(checker.CHECK_DESCRIPTIONS), set(checker.ALL_CHECKS))

    def test_full_profile_covers_every_check_except_the_optional_remote_image(self):
        self.assertEqual(set(checker.ALL_CHECKS) - set(checker.PROFILES["full"]), {"image_url"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
