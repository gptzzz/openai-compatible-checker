#!/usr/bin/env python3
"""openai-compatible-checker (oacheck): conformance checks for OpenAI-compatible APIs.

It checks protocol compatibility only. A passing report does not prove which
model or provider serves an endpoint.

One file, Python 3.10+ standard library only. Download it and run it, or
install it with pip/pipx to get the ``oacheck`` command.

    export OPENAI_API_KEY=...        # the key is read only from the environment
    python3 openai_compatible_checker.py \
        --base-url https://api.example.com/v1 --model your-model-id --profile full

Exit codes: 0 all selected checks passed, 1 at least one check failed,
2 invalid arguments, missing environment variable, or report write error.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import hashlib
import http.client
import json
import math
import os
import re
import socket
import ssl
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Mapping

__version__ = "2.0.0"
VERSION = __version__
SCHEMA_VERSION = "2.0"
TOOL_NAME = "openai-compatible-checker"
# Short product token: some CDN bot rules block User-Agents containing "compatible-checker".
USER_AGENT = f"occ/{VERSION} (OpenAI-compatible API checker; +https://github.com/gptzzz)"

SCOPE_NOTICE = (
    "Checks protocol compatibility only. A passing report does not prove which "
    "model or provider serves the endpoint, and a single run is not an SLA."
)

DEFAULT_PROMPT = "Reply with exactly OK."
DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 600.0
DEFAULT_RUNS = 5
MAX_RUNS = 50

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_ERROR_BYTES = 32 * 1024
MAX_SSE_BYTES = 4 * 1024 * 1024
MAX_SSE_LINE_BYTES = 256 * 1024
MAX_SSE_EVENTS = 10_000
MAX_INCLUDED_CONTENT_CHARS = 4_096

# A fixed, obviously invalid key used only by the error_shape check. The
# user's real key is never sent on that request.
INVALID_PROBE_KEY = "oacheck-invalid-key-for-error-shape-probe"

ALL_CHECKS = (
    "models",
    "chat",
    "stream",
    "stream_usage",
    "tools",
    "json_mode",
    "responses",
    "image",
    "image_url",
    "error_shape",
    "latency",
)

PROFILES: dict[str, tuple[str, ...]] = {
    "basic": ("models", "chat", "stream"),
    "codex": ("models", "responses", "stream"),
    "agent": ("tools", "json_mode", "stream_usage"),
    "full": (
        "models",
        "chat",
        "stream",
        "stream_usage",
        "tools",
        "json_mode",
        "responses",
        "image",
        "error_shape",
        "latency",
    ),
}
DEFAULT_PROFILE = "basic"
# Profiles that switch optional stages on unless the user turns them off.
PROFILE_TOOLS_ROUNDTRIP = frozenset({"agent", "full"})
PROFILE_RESPONSES_STREAM = frozenset({"codex", "full"})

CHECK_DESCRIPTIONS = {
    "models": "GET /models: JSON list, data[].id, target model listed",
    "chat": "POST /chat/completions: non-streaming JSON, message.content, usage",
    "stream": "POST /chat/completions stream=true: SSE, JSON chunks, [DONE]",
    "stream_usage": "stream with stream_options.include_usage: usage chunk before [DONE]",
    "tools": "tool call to get_weather: finish_reason tool_calls, id, JSON arguments",
    "json_mode": "response_format json_object: content parses as a JSON object",
    "responses": "POST /responses: status completed, message text, usage (Codex)",
    "image": "image_url content part with an 8x8 PNG base64 data URL",
    "image_url": "image_url content part with the remote URL from --image-url",
    "error_shape": "GET /models with a fake key: 401/403 JSON; reports the body shape",
    "latency": "--runs sequential streaming requests: TTFT and total p50/p95, error rate",
}

WEATHER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name, for example Paris"},
            },
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}
TOOLS_PROMPT = "What is the weather in Paris right now? Call the get_weather tool; do not answer from memory."
TOOL_RESULT = {"temperature_c": 21, "condition": "sunny"}

JSON_MODE_SYSTEM = "You are a JSON API. Respond with a single JSON object and nothing else."
JSON_MODE_PROMPT = 'Return a JSON object with the keys "status" (the string "ok") and "n" (the integer 1).'

IMAGE_PROMPT = "This image is one solid color. Reply with the name of the color only."
IMAGE_EXPECTED_COLOR = "red"


class ProbeFailure(Exception):
    """A check failed. ``kind`` is a stable machine-readable error class."""

    def __init__(self, kind: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.details = details

    def add_details(self, **details: Any) -> ProbeFailure:
        self.details = {**details, **self.details}
        return self


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep bearer credentials on the original endpoint only."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class SafeArgumentParser(argparse.ArgumentParser):
    """Never echo unknown argument values: they may be a pasted API key."""

    def parse_args(self, args=None, namespace=None):  # noqa: ANN001
        parsed, unknown = self.parse_known_args(args, namespace)
        if unknown:
            safe_unknown = []
            for item in unknown:
                if item.startswith("-"):
                    option, separator, _value = item.partition("=")
                    safe_unknown.append(option + "=[REDACTED]" if separator else option)
                else:
                    safe_unknown.append("[REDACTED]")
            self.error("unrecognized arguments: " + " ".join(safe_unknown))
        return parsed

    def error(self, message: str) -> None:  # type: ignore[override]
        super().error(redact_text(message, ()))


class ListChecksAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):  # noqa: ANN001,A002
        super().__init__(option_strings=option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):  # noqa: ANN001
        lines = ["checks:"]
        for name in ALL_CHECKS:
            lines.append(f"  {name:<13} {CHECK_DESCRIPTIONS[name]}")
        lines.append("")
        lines.append("profiles:")
        for name, checks in PROFILES.items():
            extras = []
            if name in PROFILE_TOOLS_ROUNDTRIP:
                extras.append("--tools-roundtrip")
            if name in PROFILE_RESPONSES_STREAM:
                extras.append("--responses-stream")
            suffix = f"  (turns on {', '.join(extras)})" if extras else ""
            lines.append(f"  {name:<6} {', '.join(checks)}{suffix}")
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
        parser.exit(0)


@dataclasses.dataclass(frozen=True)
class Context:
    """Everything a check needs. Built once from validated arguments."""

    base_url: str
    api_key: str
    timeout: float
    model: str | None
    prompt: str
    include_content: bool
    tools_roundtrip: bool = False
    responses_stream: bool = False
    image_url: str | None = None
    runs: int = DEFAULT_RUNS

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self.api_key, self.prompt)


# --------------------------------------------------------------------------
# Redaction and small helpers
# --------------------------------------------------------------------------


def redact_text(value: str, secrets: Iterable[str]) -> str:
    redacted = value
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(
        r"(?i)\bBearer\s+[^\s\"'\\]+",
        "Bearer [REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(\b(?:api[-_ ]?key|access[-_ ]?token|authorization|secret)"
        r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]+)",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", redacted)
    return redacted


def redact_tree(value: Any, secrets: Iterable[str]) -> Any:
    secrets = tuple(secrets)
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, (list, tuple)):
        return [redact_tree(item, secrets) for item in value]
    if isinstance(value, dict):
        return {redact_text(str(key), secrets): redact_tree(item, secrets) for key, item in value.items()}
    return value


def redacted_excerpt(value: str, secrets: Iterable[str], limit: int = 300) -> str:
    # Redact first, then truncate, so a long secret cannot leave a prefix behind.
    return redact_text(value, secrets)[:limit]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def is_token_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def nearest_rank(values: Iterable[float], percentile: float) -> float | None:
    """Nearest-rank percentile: the smallest sample with at least p% of samples <= it."""
    ordered = sorted(values)
    if not ordered:
        return None
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    rank = max(1, math.ceil(percentile / 100 * len(ordered)))
    return ordered[rank - 1]


def distribution(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "p50": nearest_rank(values, 50),
        "p95": nearest_rank(values, 95),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def solid_png(width: int = 8, height: int = 8, rgb: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    """Build a tiny solid-color RGB PNG without any imaging library."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    row = b"\x00" + bytes(rgb) * width  # filter type 0 + pixels
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * height, 9))
        + chunk(b"IEND", b"")
    )


PROBE_PNG = solid_png()
PROBE_PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PROBE_PNG).decode("ascii")


# --------------------------------------------------------------------------
# Argument parsing and validation
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(
        prog="oacheck",
        allow_abbrev=False,
        description=(
            "Check an OpenAI-compatible API for protocol conformance: models, chat, "
            "SSE streaming, stream usage, tool calls, JSON mode, the Responses API, "
            "image input, error bodies and latency. Checks protocol compatibility only; "
            "does not prove which model or provider serves the endpoint. "
            "The API key is read only from an environment variable."
        ),
        epilog="Exit codes: 0 all checks passed, 1 a check failed, 2 usage or environment error.",
    )
    parser.add_argument("--base-url", required=True, help="API prefix, usually https://host/v1")
    parser.add_argument("--model", help="model ID used by every check except models and error_shape")
    parser.add_argument(
        "--api-key-env",
        default="OPENAI_API_KEY",
        help="environment variable that holds the API key (default: OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        help=f"preset group of checks (default: {DEFAULT_PROFILE}); see --list-checks",
    )
    parser.add_argument(
        "--check",
        action="append",
        choices=ALL_CHECKS,
        help="run only this check; repeat for several. Cannot be combined with --profile",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"per-request timeout in seconds, 0.1 to {MAX_TIMEOUT:g} (default: {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="prompt for chat, stream, responses and latency; never written to reports",
    )
    parser.add_argument(
        "--include-content",
        action="store_true",
        help="include up to 4096 characters of each reply in the report (off by default)",
    )
    parser.add_argument(
        "--tools-roundtrip",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="after the tool call, send the tool result back and check the final answer "
        "(on by default in the agent and full profiles)",
    )
    parser.add_argument(
        "--responses-stream",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="also stream /responses and require response.completed, as Codex does "
        "(on by default in the codex and full profiles)",
    )
    parser.add_argument(
        "--image-url",
        help="also test a remote http(s) image URL; the gateway must be able to fetch it",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=DEFAULT_RUNS,
        help=f"sequential requests for the latency check, 1 to {MAX_RUNS} (default: {DEFAULT_RUNS})",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="report format (default: json)",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="report path, or - for stdout (default: -)",
    )
    parser.add_argument(
        "--no-step-summary",
        action="store_true",
        help="do not append a Markdown summary to $GITHUB_STEP_SUMMARY in GitHub Actions",
    )
    parser.add_argument("--list-checks", action=ListChecksAction, help="list checks and profiles, then exit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


ENDPOINT_SUFFIXES = ("/models", "/chat/completions", "/responses", "/completions")


def validate_http_url(
    raw_url: str, parser: argparse.ArgumentParser, label: str, *, allow_query: bool
) -> urllib.parse.SplitResult:
    if not raw_url or len(raw_url) > 2_048:
        parser.error(f"{label} must contain between 1 and 2048 characters")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw_url):
        parser.error(f"{label} must not contain whitespace or control characters")
    if not raw_url.isascii():
        parser.error(f"{label} must use ASCII; encode IDN hosts and URL paths first")
    try:
        parts = urllib.parse.urlsplit(raw_url)
    except ValueError as exc:
        parser.error(f"invalid {label}: {exc}")
    if parts.scheme.lower() not in {"http", "https"}:
        parser.error(f"{label} must use http:// or https://")
    if not parts.netloc or not parts.hostname:
        parser.error(f"{label} must include a hostname")
    if parts.username is not None or parts.password is not None:
        parser.error(f"{label} must not include credentials")
    if parts.fragment or (parts.query and not allow_query):
        parser.error(f"{label} must not include a query string or fragment")
    try:
        _ = parts.port
    except ValueError as exc:
        parser.error(f"invalid {label} port: {exc}")
    return parts


def validate_base_url(raw_url: str, parser: argparse.ArgumentParser) -> str:
    parts = validate_http_url(raw_url, parser, "base URL", allow_query=False)
    path = parts.path.rstrip("/")
    if path.lower().endswith(ENDPOINT_SUFFIXES):
        parser.error("base URL must be an API prefix such as https://host/v1, not an endpoint path")
    normalized = urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, path, "", ""))
    return normalized.rstrip("/")


@dataclasses.dataclass(frozen=True)
class Selection:
    profile: str | None
    checks: tuple[str, ...]
    tools_roundtrip: bool
    responses_stream: bool


def resolve_selection(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Selection:
    if args.check and args.profile:
        parser.error("use either --profile or --check, not both")
    profile = args.profile or (None if args.check else DEFAULT_PROFILE)
    checks = list(args.check) if args.check else list(PROFILES[profile])  # type: ignore[index]
    if len(set(checks)) != len(checks):
        parser.error("duplicate check values are not allowed")
    if args.image_url and "image" in checks and "image_url" not in checks:
        checks.insert(checks.index("image") + 1, "image_url")
    if "image_url" in checks and not args.image_url:
        parser.error("--image-url is required when the image_url check is selected")
    tools_roundtrip = args.tools_roundtrip
    if tools_roundtrip is None:
        tools_roundtrip = profile in PROFILE_TOOLS_ROUNDTRIP
    responses_stream = args.responses_stream
    if responses_stream is None:
        responses_stream = profile in PROFILE_RESPONSES_STREAM
    return Selection(profile, tuple(checks), bool(tools_roundtrip), bool(responses_stream))


def validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[Context, Selection]:
    base_url = validate_base_url(args.base_url, parser)
    selection = resolve_selection(args, parser)
    needs_model = [name for name in selection.checks if CHECKS[name].needs_model]
    if needs_model and not args.model:
        parser.error("--model is required for the selected checks: " + ", ".join(needs_model))
    if args.model is not None:
        if not args.model.strip() or args.model != args.model.strip():
            parser.error("model ID must be non-empty and have no surrounding whitespace")
        if len(args.model) > 256 or any(ord(char) < 32 for char in args.model):
            parser.error("model ID must be at most 256 characters without control characters")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env or ""):
        parser.error("API key environment variable name is invalid")
    if not math.isfinite(args.timeout) or not 0.1 <= args.timeout <= MAX_TIMEOUT:
        parser.error(f"timeout must be between 0.1 and {MAX_TIMEOUT:g} seconds")
    if not 1 <= args.runs <= MAX_RUNS:
        parser.error(f"runs must be between 1 and {MAX_RUNS}")
    if not args.prompt.strip():
        parser.error("prompt must not be empty")
    if len(args.prompt) > 4_096:
        parser.error("prompt must be at most 4096 characters")
    if "\x00" in args.prompt:
        parser.error("prompt must not contain NUL characters")
    if not args.output or "\x00" in args.output:
        parser.error("output path must not be empty or contain NUL characters")
    image_url = None
    if args.image_url:
        validate_http_url(args.image_url, parser, "image URL", allow_query=True)
        image_url = args.image_url

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        parser.error(f"environment variable {args.api_key_env} is not set or empty")
    if len(api_key) > 4_096:
        parser.error(f"environment variable {args.api_key_env} exceeds 4096 characters")
    if any(not 33 <= ord(char) <= 126 for char in api_key):
        parser.error(f"environment variable {args.api_key_env} must contain printable ASCII without whitespace")
    context = Context(
        base_url=base_url,
        api_key=api_key,
        timeout=args.timeout,
        model=args.model,
        prompt=args.prompt,
        include_content=args.include_content,
        tools_roundtrip=selection.tools_roundtrip,
        responses_stream=selection.responses_stream,
        image_url=image_url,
        runs=args.runs,
    )
    return context, selection


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

NETWORK_ERRORS = (socket.timeout, TimeoutError, ssl.SSLError, http.client.HTTPException, OSError)


def endpoint(base_url: str, suffix: str) -> str:
    return f"{base_url}/{suffix.lstrip('/')}"


def request_headers(api_key: str, accept: str) -> dict[str, str]:
    return {
        "Accept": accept,
        "Authorization": f"Bearer {api_key}",
        "User-Agent": USER_AGENT,
    }


def response_metadata(response: Any, started: float, secrets: Iterable[str] = ()) -> dict[str, Any]:
    secrets = tuple(secrets)
    content_type = redact_text(response.headers.get("Content-Type", ""), secrets)
    metadata: dict[str, Any] = {
        "status_code": int(response.status),
        "latency_ms": elapsed_ms(started),
        "content_type": content_type,
    }
    for header_name in ("X-Request-ID", "Request-ID", "CF-Ray"):
        value = response.headers.get(header_name)
        if value:
            metadata["request_id"] = redacted_excerpt(value, secrets, 512)
            break
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        metadata["retry_after"] = redacted_excerpt(retry_after, secrets, 128)
    return metadata


def media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def is_json_content_type(content_type: str) -> bool:
    value = media_type(content_type)
    return value == "application/json" or value.endswith("+json")


def read_limited(stream: BinaryIO, limit: int, started: float, timeout: float) -> bytes:
    chunks: list[bytes] = []
    total = 0
    read_method = getattr(stream, "read1", stream.read)
    while True:
        if time.perf_counter() - started > timeout:
            raise ProbeFailure("timeout", "response exceeded the configured deadline")
        chunk = read_method(min(65_536, limit + 1 - total))
        if time.perf_counter() - started > timeout:
            raise ProbeFailure("timeout", "response exceeded the configured deadline")
        if not chunk:
            remaining = getattr(stream, "length", None)
            if isinstance(remaining, int) and remaining > 0:
                raise ProbeFailure("network_error", "response ended before the declared Content-Length")
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise ProbeFailure("response_too_large", f"response exceeded the {limit}-byte safety limit")


def decode_error_message(body: bytes, secrets: Iterable[str]) -> str:
    secrets = tuple(secrets)
    if not body:
        return "empty response body"
    text = body.decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        compact = " ".join(text.split())
        return redacted_excerpt(compact, secrets) if compact else "unreadable response body"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return redacted_excerpt(error["message"], secrets)
        if isinstance(error, str):
            return redacted_excerpt(error, secrets)
        if isinstance(payload.get("message"), str):
            return redacted_excerpt(payload["message"], secrets)
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return redacted_excerpt(compact, secrets)


_CDN_PLAIN_ERROR = re.compile(r"\s*error code:\s*(1\d{3})\s*", re.IGNORECASE)


def cdn_block_label(headers: Any, body: bytes) -> str | None:
    """Name the CDN/WAF block page an error response came from, or return None.

    Only Cloudflare's own error formats are recognised: the plain-text body
    "error code: 1xxx", the JSON error body with "cloudflare_error": true, and the
    cf-mitigated response header. Such a response was produced before the request
    reached the API, so it says nothing about the key or the endpoint's protocol.
    """
    mitigated = headers.get("cf-mitigated") if headers is not None else None
    if mitigated:
        return f"Cloudflare cf-mitigated: {' '.join(str(mitigated).split())[:40]}"
    text = body[:4096].decode("utf-8", errors="replace").strip()
    match = _CDN_PLAIN_ERROR.fullmatch(text)
    if match:
        return f"Cloudflare error {match.group(1)}"
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(payload, dict) and payload.get("cloudflare_error") is True:
            code = payload.get("error_code")
            if isinstance(code, int) and not isinstance(code, bool):
                return f"Cloudflare error {code}"
            return "Cloudflare error"
    return None


def cdn_block_failure(http_status: int, label: str, **details: Any) -> ProbeFailure:
    return ProbeFailure(
        "blocked_by_cdn",
        f"HTTP {http_status}: {label}. A CDN/WAF in front of the API refused the request before it "
        "reached the API, so this result says nothing about the key; ask the provider whether "
        "it filters on User-Agent or IP",
        **details,
    )


def classify_http_status(status_code: int) -> str:
    if status_code in {401, 403}:
        return "authentication_error"
    if status_code in {400, 422}:
        return "bad_request"
    if status_code == 404:
        return "not_found"
    if status_code == 429:
        return "rate_limited"
    if 500 <= status_code <= 599:
        return "server_error"
    if 300 <= status_code <= 399:
        return "redirect_rejected"
    return "http_error"


def network_failure(exc: BaseException, started: float) -> ProbeFailure:
    reason: Any = exc
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
    details = {"latency_ms": elapsed_ms(started)}
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return ProbeFailure("timeout", "request timed out", **details)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return ProbeFailure("tls_error", "TLS certificate verification failed", **details)
    if isinstance(reason, ssl.SSLError):
        return ProbeFailure("tls_error", f"TLS handshake failed: {reason}", **details)
    if isinstance(reason, socket.gaierror):
        return ProbeFailure("dns_error", f"DNS resolution failed: {reason}", **details)
    return ProbeFailure("network_error", f"network request failed: {reason}", **details)


def _send(url: str, api_key: str, timeout: float, payload: Mapping[str, Any] | None, accept: str) -> tuple[Any, float]:
    body: bytes | None = None
    headers = request_headers(api_key, accept)
    method = "GET"
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(NoRedirectHandler())
    started = time.perf_counter()
    return opener.open(request, timeout=timeout), started


def open_request(
    url: str,
    api_key: str,
    timeout: float,
    *,
    payload: Mapping[str, Any] | None = None,
    accept: str,
    sensitive_values: Iterable[str] = (),
) -> tuple[Any, float]:
    all_sensitive = (api_key, *tuple(sensitive_values))
    started = time.perf_counter()
    try:
        return _send(url, api_key, timeout, payload, accept)
    except urllib.error.HTTPError as exc:
        metadata = response_metadata(exc, started, all_sensitive)
        try:
            try:
                body_bytes = read_limited(exc, MAX_ERROR_BYTES, started, timeout)
            except ProbeFailure as body_failure:
                body_bytes = b""
                metadata["error_body"] = body_failure.kind
            except NETWORK_ERRORS as body_exc:
                body_bytes = b""
                metadata["error_body"] = network_failure(body_exc, started).kind
        finally:
            exc.close()
        status_code = int(exc.code)
        metadata["latency_ms"] = elapsed_ms(started)
        blocked = cdn_block_label(exc.headers, body_bytes)
        if blocked:
            raise cdn_block_failure(status_code, blocked, **metadata) from None
        message = decode_error_message(body_bytes, all_sensitive)
        raise ProbeFailure(
            classify_http_status(status_code),
            f"HTTP {status_code}: {message}",
            **metadata,
        ) from None
    except (urllib.error.URLError, *NETWORK_ERRORS) as exc:
        raise network_failure(exc, started) from None


class InvalidJSONConstant(ValueError):
    pass


class InvalidUnicodeScalar(ValueError):
    pass


def reject_json_constant(value: str) -> None:
    raise InvalidJSONConstant(value)


def strict_json_loads(value: str) -> Any:
    """json.loads that rejects NaN/Infinity and unpaired UTF-16 surrogates."""
    parsed = json.loads(value, parse_constant=reject_json_constant)
    validate_unicode_scalars(parsed)
    return parsed


def validate_unicode_scalars(value: Any) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise InvalidUnicodeScalar("unpaired UTF-16 surrogate")
        return
    if isinstance(value, list):
        for item in value:
            validate_unicode_scalars(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            validate_unicode_scalars(key)
            validate_unicode_scalars(item)


def request_json(
    url: str,
    api_key: str,
    timeout: float,
    payload: Mapping[str, Any] | None = None,
    sensitive_values: Iterable[str] = (),
) -> tuple[Any, dict[str, Any]]:
    sensitive = tuple(sensitive_values)
    response, started = open_request(
        url,
        api_key,
        timeout,
        payload=payload,
        accept="application/json",
        sensitive_values=sensitive,
    )
    try:
        metadata = response_metadata(response, started, (api_key, *sensitive))
        if not is_json_content_type(metadata["content_type"]):
            raise ProbeFailure(
                "unexpected_content_type",
                f"expected JSON but received {metadata['content_type'] or 'no Content-Type'}",
                **metadata,
            )
        try:
            body = read_limited(response, MAX_JSON_BYTES, started, timeout)
        except ProbeFailure as exc:
            metadata["latency_ms"] = elapsed_ms(started)
            raise exc.add_details(**metadata)
        except NETWORK_ERRORS as exc:
            raise network_failure(exc, started).add_details(**metadata)
    finally:
        response.close()
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProbeFailure(
            "invalid_json_encoding",
            f"response is not valid UTF-8 at byte {exc.start}",
            **metadata,
        ) from None
    metadata["latency_ms"] = elapsed_ms(started)
    try:
        return strict_json_loads(text), metadata
    except (InvalidJSONConstant, InvalidUnicodeScalar) as exc:
        raise ProbeFailure("invalid_json", f"invalid JSON value: {exc}", **metadata) from None
    except json.JSONDecodeError as exc:
        raise ProbeFailure(
            "invalid_json",
            f"invalid JSON at line {exc.lineno}, column {exc.colno}",
            **metadata,
        ) from None


def fetch_raw(url: str, bearer: str, timeout: float, secrets: Iterable[str]) -> tuple[dict[str, Any], bytes]:
    """GET without raising on HTTP errors. Returns metadata and a size-limited body."""
    secrets = (bearer, *tuple(secrets))
    started = time.perf_counter()
    try:
        response, started = _send(url, bearer, timeout, None, "application/json")
    except urllib.error.HTTPError as exc:
        response = exc
    except (urllib.error.URLError, *NETWORK_ERRORS) as exc:
        raise network_failure(exc, started) from None
    try:
        metadata = response_metadata(response, started, secrets)
        try:
            body = read_limited(response, MAX_ERROR_BYTES, started, timeout)
        except ProbeFailure as exc:
            raise exc.add_details(**metadata)
        except NETWORK_ERRORS as exc:
            raise network_failure(exc, started).add_details(**metadata)
        blocked = cdn_block_label(response.headers, body) if metadata["status_code"] >= 400 else None
    finally:
        response.close()
    metadata["latency_ms"] = elapsed_ms(started)
    if blocked:
        raise cdn_block_failure(metadata["status_code"], blocked, **metadata)
    return metadata, body


# --------------------------------------------------------------------------
# Response parsing helpers
# --------------------------------------------------------------------------


def api_error_message(payload: Mapping[str, Any], secrets: Iterable[str]) -> str | None:
    error = payload.get("error")
    if error is None:
        return None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        if not error["message"]:
            return "response contained an empty API error message"
        return redacted_excerpt(error["message"], secrets)
    if isinstance(error, str):
        if not error:
            return "response contained an empty API error string"
        return redacted_excerpt(error, secrets)
    return "response contained an API error object"


USAGE_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
)


def normalize_usage(value: Any) -> dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    safe_usage: dict[str, int | float] = {}
    for key in USAGE_KEYS:
        number = value.get(key)
        if isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number):
            safe_usage[key] = number
    for detail_key, inner_key in (
        ("input_tokens_details", "cached_tokens"),
        ("prompt_tokens_details", "cached_tokens"),
        ("output_tokens_details", "reasoning_tokens"),
        ("completion_tokens_details", "reasoning_tokens"),
    ):
        details = value.get(detail_key)
        if isinstance(details, dict) and is_token_count(details.get(inner_key)):
            safe_usage[f"{detail_key}.{inner_key}"] = details[inner_key]
    return safe_usage


def extract_text_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                raise ProbeFailure("invalid_schema", "assistant content parts must contain string text fields")
            parts.append(part["text"])
        return "".join(parts)
    raise ProbeFailure("invalid_schema", "assistant content must be a string, list, or null")


def content_fields(content: str, include: bool, secrets: Iterable[str]) -> dict[str, Any]:
    safe_content = redact_text(content, secrets)
    fields: dict[str, Any] = {
        "content_chars": len(safe_content),
        "content_sha256": hashlib.sha256(safe_content.encode("utf-8")).hexdigest(),
    }
    if include:
        fields["content"] = safe_content[:MAX_INCLUDED_CONTENT_CHARS]
        fields["content_truncated"] = len(safe_content) > MAX_INCLUDED_CONTENT_CHARS
    return fields


def parse_chat_completion(
    payload: Any,
    metadata: dict[str, Any],
    secrets: Iterable[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate the common chat.completion envelope. Returns (choice, message, result)."""
    secrets = tuple(secrets)
    if not isinstance(payload, dict):
        raise ProbeFailure("invalid_schema", "chat response must be a JSON object", **metadata)
    error_message = api_error_message(payload, secrets)
    if error_message is not None:
        raise ProbeFailure("api_error", error_message, **metadata)
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProbeFailure("invalid_schema", "chat response must contain at least one choice", **metadata)
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ProbeFailure("invalid_schema", "first chat choice must contain a message object", **metadata)
    result: dict[str, Any] = dict(metadata)
    response_id = payload.get("id")
    if isinstance(response_id, str):
        result["response_id"] = redacted_excerpt(response_id, secrets, 512)
    finish_reason = choice.get("finish_reason")
    if isinstance(finish_reason, str):
        result["finish_reason"] = redacted_excerpt(finish_reason, secrets, 128)
    usage = normalize_usage(payload.get("usage"))
    if usage:
        result["usage"] = usage
    return choice, message, result


def add_note(result: dict[str, Any], note: str) -> None:
    notes = result.setdefault("notes", [])
    if note not in notes:
        notes.append(note)


# --------------------------------------------------------------------------
# Server-sent events
# --------------------------------------------------------------------------


def sse_fields(lines: list[str]) -> tuple[str | None, str] | None:
    data_lines: list[str] = []
    event_name: str | None = None
    for line in lines:
        if not line or line.startswith(":"):
            continue
        if ":" in line:
            field, value = line.split(":", 1)
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""
        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event_name = value
    return (event_name, "\n".join(data_lines)) if data_lines else None


def iter_sse_lines(response: BinaryIO, started: float, timeout: float) -> Iterator[str]:
    total_bytes = 0
    line = bytearray()
    skip_lf = False
    first_line = True
    read_method = getattr(response, "read1", response.read)

    def decode(raw: bytearray) -> str:
        try:
            return bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProbeFailure(
                "invalid_sse_encoding",
                f"SSE stream is not valid UTF-8 at byte {exc.start}",
            ) from None

    while True:
        if time.perf_counter() - started > timeout:
            raise ProbeFailure("timeout", "SSE probe exceeded the configured deadline")
        chunk = read_method(8_192)
        if time.perf_counter() - started > timeout:
            raise ProbeFailure("timeout", "SSE probe exceeded the configured deadline")
        if not chunk:
            if line:
                decoded = decode(line)
                if first_line and decoded.startswith("﻿"):
                    decoded = decoded[1:]
                yield decoded
            return
        for byte in chunk:
            total_bytes += 1
            if total_bytes > MAX_SSE_BYTES:
                raise ProbeFailure("response_too_large", "SSE response exceeded the safety limit")
            if skip_lf:
                skip_lf = False
                if byte == 0x0A:
                    continue
            if byte in {0x0A, 0x0D}:
                decoded = decode(line)
                line.clear()
                if first_line and decoded.startswith("﻿"):
                    decoded = decoded[1:]
                first_line = False
                yield decoded
                skip_lf = byte == 0x0D
            else:
                line.append(byte)
                if len(line) > MAX_SSE_LINE_BYTES:
                    raise ProbeFailure("sse_line_too_large", "SSE line exceeded the safety limit")


def iter_sse_events(response: BinaryIO, started: float, timeout: float) -> Iterator[tuple[str | None, str]]:
    """Yield (event name, data) for every SSE event that carries data."""
    event_lines: list[str] = []
    for line in iter_sse_lines(response, started, timeout):
        if line == "":
            parsed = sse_fields(event_lines)
            event_lines = []
            if parsed is not None:
                yield parsed
        else:
            event_lines.append(line)
    parsed = sse_fields(event_lines)
    if parsed is not None:
        yield parsed


def parse_sse_json(data: str) -> Any:
    try:
        return strict_json_loads(data)
    except (InvalidJSONConstant, InvalidUnicodeScalar) as exc:
        raise ProbeFailure("invalid_sse_json", f"invalid SSE JSON value: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ProbeFailure(
            "invalid_sse_json",
            f"invalid SSE JSON at line {exc.lineno}, column {exc.colno}",
        ) from None


def open_sse(url: str, ctx: Context, payload: Mapping[str, Any]) -> tuple[Any, float, dict[str, Any]]:
    response, started = open_request(
        url,
        ctx.api_key,
        ctx.timeout,
        payload=payload,
        accept="text/event-stream",
        sensitive_values=(ctx.prompt,),
    )
    try:
        metadata = response_metadata(response, started, ctx.secrets)
        if media_type(metadata["content_type"]) != "text/event-stream":
            raise ProbeFailure(
                "unexpected_content_type",
                f"expected text/event-stream but received {metadata['content_type'] or 'no Content-Type'}",
                **metadata,
            )
    except BaseException:
        response.close()
        raise
    return response, started, metadata


NOTE_USAGE_WITHOUT_CHOICES = (
    "the usage chunk has no choices field; OpenAI sends choices: [] on that chunk, "
    "and clients that index choices may fail"
)


def consume_chat_stream(
    response: Any,
    started: float,
    metadata: dict[str, Any],
    ctx: Context,
    *,
    expect_usage: bool,
) -> dict[str, Any]:
    secrets = ctx.secrets
    event_count = 0
    json_event_count = 0
    choice_event_count = 0
    delta_event_count = 0
    completed = False
    first_event_ms: float | None = None
    ttft_ms: float | None = None
    content_parts: list[str] = []
    finish_reason: str | None = None
    usage_event: dict[str, Any] | None = None
    notes: list[str] = []

    def progress() -> dict[str, Any]:
        return {
            **metadata,
            "latency_ms": elapsed_ms(started),
            "event_count": event_count,
            "completed": completed,
        }

    try:
        for _event_name, data in iter_sse_events(response, started, ctx.timeout):
            event_count += 1
            if event_count > MAX_SSE_EVENTS:
                raise ProbeFailure("too_many_sse_events", "SSE event count exceeded the safety limit")
            if first_event_ms is None:
                first_event_ms = elapsed_ms(started)
            if data == "[DONE]":
                completed = True
                break
            event = parse_sse_json(data)
            if not isinstance(event, dict):
                raise ProbeFailure("invalid_sse_schema", "SSE data must decode to a JSON object")
            json_event_count += 1
            error_message = api_error_message(event, secrets)
            if error_message is not None:
                raise ProbeFailure("api_error", error_message)
            usage_value = event.get("usage")
            choices = event.get("choices")
            if choices is None and isinstance(usage_value, dict):
                choices = []
                if NOTE_USAGE_WITHOUT_CHOICES not in notes:
                    notes.append(NOTE_USAGE_WITHOUT_CHOICES)
            if not isinstance(choices, list):
                raise ProbeFailure("invalid_sse_schema", "SSE event must contain a choices array")
            if isinstance(usage_value, dict):
                usage_event = {"usage": usage_value, "choices_empty": not choices}
            if choices:
                choice_event_count += 1
            for choice in choices:
                if not isinstance(choice, dict):
                    raise ProbeFailure("invalid_sse_schema", "SSE choice must be an object")
                delta = choice.get("delta")
                if delta is not None:
                    if not isinstance(delta, dict):
                        raise ProbeFailure("invalid_sse_schema", "SSE choice delta must be an object")
                    if "content" in delta:
                        try:
                            text = extract_text_content(delta["content"])
                        except ProbeFailure as exc:
                            exc.kind = "invalid_sse_schema"
                            raise
                        if text:
                            if ttft_ms is None:
                                ttft_ms = elapsed_ms(started)
                            content_parts.append(text)
                            delta_event_count += 1
                candidate = choice.get("finish_reason")
                if isinstance(candidate, str):
                    finish_reason = redacted_excerpt(candidate, secrets, 128)
    except ProbeFailure as exc:
        raise exc.add_details(**progress())
    except NETWORK_ERRORS as exc:
        raise network_failure(exc, started).add_details(**metadata, event_count=event_count, completed=completed)
    if not completed:
        raise ProbeFailure("sse_incomplete", "SSE stream ended before the [DONE] marker", **progress())
    if json_event_count == 0:
        raise ProbeFailure("invalid_sse_schema", "SSE stream completed without a JSON data event", **progress())
    if choice_event_count == 0:
        raise ProbeFailure(
            "invalid_sse_schema",
            "SSE stream completed without a non-empty choices event",
            **progress(),
        )
    result: dict[str, Any] = {
        **metadata,
        "latency_ms": elapsed_ms(started),
        "first_event_ms": first_event_ms,
        "ttft_ms": ttft_ms,
        "event_count": event_count,
        "json_event_count": json_event_count,
        "choice_event_count": choice_event_count,
        "delta_event_count": delta_event_count,
        "completed": True,
    }
    if finish_reason:
        result["finish_reason"] = finish_reason
    for note in notes:
        add_note(result, note)
    if usage_event is not None:
        usage = normalize_usage(usage_event["usage"])
        if usage:
            result["usage"] = usage
    if expect_usage:
        if usage_event is None:
            raise ProbeFailure(
                "usage_missing",
                "no chunk with a usage object arrived before [DONE] although stream_options.include_usage was set",
                **result,
            )
        raw_usage = usage_event["usage"]
        missing = [
            key
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if not is_token_count(raw_usage.get(key))
        ]
        if missing:
            raise ProbeFailure(
                "usage_invalid",
                "usage chunk is missing non-negative integer fields: " + ", ".join(missing),
                **result,
            )
        result["usage_chunk_choices_empty"] = usage_event["choices_empty"]
        if not usage_event["choices_empty"]:
            add_note(result, "the usage chunk carried non-empty choices; OpenAI sends choices: [] on that chunk")
        if raw_usage["total_tokens"] != raw_usage["prompt_tokens"] + raw_usage["completion_tokens"]:
            add_note(result, "usage.total_tokens is not prompt_tokens + completion_tokens")
    result.update(content_fields("".join(content_parts), ctx.include_content, secrets))
    return result


def stream_chat_once(ctx: Context, url: str, *, include_usage: bool) -> dict[str, Any]:
    assert ctx.model is not None
    payload: dict[str, Any] = {
        "model": ctx.model,
        "messages": [{"role": "user", "content": ctx.prompt}],
        "stream": True,
    }
    if include_usage:
        payload["stream_options"] = {"include_usage": True}
    response, started, metadata = open_sse(url, ctx, payload)
    try:
        return consume_chat_stream(response, started, metadata, ctx, expect_usage=include_usage)
    finally:
        response.close()


# --------------------------------------------------------------------------
# Checks. Each takes (ctx, url) and returns a result dict or raises ProbeFailure.
# --------------------------------------------------------------------------


def probe_models(ctx: Context, url: str) -> dict[str, Any]:
    payload, metadata = request_json(url, ctx.api_key, ctx.timeout)
    if not isinstance(payload, dict):
        raise ProbeFailure("invalid_schema", "models response must be a JSON object", **metadata)
    error_message = api_error_message(payload, (ctx.api_key,))
    if error_message is not None:
        raise ProbeFailure("api_error", error_message, **metadata)
    if not isinstance(payload.get("data"), list):
        raise ProbeFailure("invalid_schema", "models response must contain a data array", **metadata)
    model_ids: list[str] = []
    for item in payload["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise ProbeFailure(
                "invalid_schema",
                "every models data item must contain a non-empty string id",
                **metadata,
            )
        model_ids.append(item["id"])
    safe_model_ids = [redact_text(item, (ctx.api_key,)) for item in model_ids]
    result: dict[str, Any] = {
        **metadata,
        "model_count": len(model_ids),
        "model_ids_sample": safe_model_ids[:25],
        "model_ids_truncated": len(model_ids) > 25,
    }
    if ctx.model is not None:
        present = ctx.model in model_ids
        result["target_model_present"] = present
        if not present:
            raise ProbeFailure("model_not_listed", "the requested model was not present in /models", **result)
    return result


def probe_chat(ctx: Context, url: str) -> dict[str, Any]:
    assert ctx.model is not None
    payload, metadata = request_json(
        url,
        ctx.api_key,
        ctx.timeout,
        {"model": ctx.model, "messages": [{"role": "user", "content": ctx.prompt}], "stream": False},
        sensitive_values=(ctx.prompt,),
    )
    _choice, message, result = parse_chat_completion(payload, metadata, ctx.secrets)
    if "content" not in message:
        raise ProbeFailure("invalid_schema", "first chat choice must contain message.content", **metadata)
    try:
        content = extract_text_content(message["content"])
    except ProbeFailure as exc:
        raise exc.add_details(**metadata)
    result.update(content_fields(content, ctx.include_content, ctx.secrets))
    return result


def probe_stream(ctx: Context, url: str) -> dict[str, Any]:
    return stream_chat_once(ctx, url, include_usage=False)


def probe_stream_usage(ctx: Context, url: str) -> dict[str, Any]:
    return stream_chat_once(ctx, url, include_usage=True)


def validate_tool_calls(
    choice: dict[str, Any], message: dict[str, Any], result: dict[str, Any]
) -> list[dict[str, Any]]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        hint = ""
        if isinstance(message.get("function_call"), dict):
            hint = "; the reply used the deprecated function_call field instead of tool_calls"
        raise ProbeFailure(
            "no_tool_call",
            f"response contained no tool_calls (finish_reason={result.get('finish_reason', 'missing')}){hint}",
            **result,
        )
    parsed_calls: list[dict[str, Any]] = []
    for index, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            raise ProbeFailure("invalid_schema", f"tool_calls[{index}] must be an object", **result)
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise ProbeFailure(
                "tool_call_missing_id",
                f"tool_calls[{index}] has no id; the tool result cannot be matched to the call",
                **result,
            )
        if call.get("type") != "function":
            add_note(result, 'a tool call has no type "function"; OpenAI always sets it')
        function = call.get("function")
        if not isinstance(function, dict):
            raise ProbeFailure("invalid_schema", f"tool_calls[{index}].function must be an object", **result)
        if function.get("name") != WEATHER_TOOL["function"]["name"]:
            raise ProbeFailure(
                "tool_call_wrong_name",
                f"tool_calls[{index}] called a function other than get_weather",
                **result,
            )
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            raise ProbeFailure(
                "tool_arguments_not_string",
                f"tool_calls[{index}].function.arguments must be a JSON-encoded string, got {type(arguments).__name__}",
                **result,
            )
        try:
            parsed = strict_json_loads(arguments)
        except (ValueError, json.JSONDecodeError):
            raise ProbeFailure(
                "tool_arguments_not_json",
                f"tool_calls[{index}].function.arguments is not valid JSON",
                **result,
            ) from None
        if not isinstance(parsed, dict):
            raise ProbeFailure(
                "tool_arguments_not_object",
                f"tool_calls[{index}].function.arguments must decode to a JSON object",
                **result,
            )
        parsed_calls.append({"id": call_id, "arguments": arguments, "parsed": parsed})
    if choice.get("finish_reason") != "tool_calls":
        raise ProbeFailure(
            "unexpected_finish_reason",
            f"finish_reason is {result.get('finish_reason', 'missing')!s}, expected tool_calls",
            **result,
        )
    return parsed_calls


def probe_tools(ctx: Context, url: str) -> dict[str, Any]:
    assert ctx.model is not None
    user_message = {"role": "user", "content": TOOLS_PROMPT}
    request = {
        "model": ctx.model,
        "messages": [user_message],
        "tools": [WEATHER_TOOL],
        "tool_choice": "auto",
    }
    payload, metadata = request_json(url, ctx.api_key, ctx.timeout, request)
    choice, message, result = parse_chat_completion(payload, metadata, (ctx.api_key,))
    calls = validate_tool_calls(choice, message, result)
    first_args = calls[0]["parsed"]
    result["tool_call_count"] = len(calls)
    result["tool_call_ids_have_call_prefix"] = all(call["id"].startswith("call_") for call in calls)
    result["argument_keys"] = sorted(redacted_excerpt(str(key), (ctx.api_key,), 64) for key in first_args)[:10]
    result["city_argument_present"] = isinstance(first_args.get("city"), str) and bool(first_args.get("city"))
    if ctx.include_content:
        result["arguments"] = redacted_excerpt(calls[0]["arguments"], (ctx.api_key,), MAX_INCLUDED_CONTENT_CHARS)
    if not ctx.tools_roundtrip:
        return result

    assistant_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call["id"],
                "type": "function",
                "function": {"name": WEATHER_TOOL["function"]["name"], "arguments": call["arguments"]},
            }
            for call in calls
        ],
    }
    tool_messages = [
        {
            "role": "tool",
            "tool_call_id": call["id"],
            "content": json.dumps({"city": str(call["parsed"].get("city", "Paris"))[:64], **TOOL_RESULT}),
        }
        for call in calls
    ]
    follow_up = {
        "model": ctx.model,
        "messages": [user_message, assistant_message, *tool_messages],
        "tools": [WEATHER_TOOL],
    }
    try:
        payload2, metadata2 = request_json(url, ctx.api_key, ctx.timeout, follow_up)
        _choice2, message2, roundtrip = parse_chat_completion(payload2, metadata2, (ctx.api_key,))
        if isinstance(message2.get("tool_calls"), list) and message2["tool_calls"]:
            raise ProbeFailure(
                "roundtrip_no_final_answer",
                "after receiving the tool result the model called a tool again instead of answering",
                **roundtrip,
            )
        try:
            answer = extract_text_content(message2.get("content"))
        except ProbeFailure as exc:
            raise exc.add_details(**roundtrip)
        if not answer.strip():
            raise ProbeFailure(
                "roundtrip_no_final_answer",
                "the final answer after the tool result was empty",
                **roundtrip,
            )
        roundtrip["mentions_tool_result"] = str(TOOL_RESULT["temperature_c"]) in answer
        roundtrip.update(content_fields(answer, ctx.include_content, (ctx.api_key,)))
        roundtrip["ok"] = True
    except ProbeFailure as exc:
        raise ProbeFailure(
            exc.kind,
            f"tool round trip: {exc.message}",
            **result,
            stage="roundtrip",
            roundtrip={"ok": False, **exc.details},
        ) from None
    result["roundtrip"] = roundtrip
    return result


def probe_json_mode(ctx: Context, url: str) -> dict[str, Any]:
    assert ctx.model is not None
    request = {
        "model": ctx.model,
        "messages": [
            {"role": "system", "content": JSON_MODE_SYSTEM},
            {"role": "user", "content": JSON_MODE_PROMPT},
        ],
        "response_format": {"type": "json_object"},
    }
    payload, metadata = request_json(url, ctx.api_key, ctx.timeout, request)
    _choice, message, result = parse_chat_completion(payload, metadata, (ctx.api_key,))
    try:
        content = extract_text_content(message.get("content"))
    except ProbeFailure as exc:
        raise exc.add_details(**result)
    result.update(content_fields(content, ctx.include_content, (ctx.api_key,)))
    stripped = content.strip()
    try:
        parsed = strict_json_loads(stripped)
    except (ValueError, json.JSONDecodeError):
        hint = ""
        if stripped.startswith("```"):
            hint = "; the content is wrapped in a Markdown code fence, so response_format was probably ignored"
        elif not stripped:
            hint = "; the content is empty"
        raise ProbeFailure("invalid_json_output", f"message content is not valid JSON{hint}", **result) from None
    if not isinstance(parsed, dict):
        raise ProbeFailure(
            "json_output_not_object",
            f"message content is JSON but not an object ({type(parsed).__name__})",
            **result,
        )
    result["json_keys"] = sorted(redacted_excerpt(str(key), (ctx.api_key,), 64) for key in parsed)[:10]
    if result.get("finish_reason") == "length":
        add_note(result, "finish_reason is length; the JSON may be cut short on longer outputs")
    return result


def validate_responses_usage(usage: Any, where: str) -> None:
    """Mirror the usage fields Codex deserializes from response.completed."""
    if not isinstance(usage, dict):
        raise ProbeFailure("usage_invalid", f"{where}.usage must be an object")
    missing = [key for key in ("input_tokens", "output_tokens", "total_tokens") if not is_token_count(usage.get(key))]
    if missing:
        raise ProbeFailure(
            "usage_invalid",
            f"{where}.usage is missing non-negative integer fields: " + ", ".join(missing),
        )
    for detail_key, inner_key in (
        ("input_tokens_details", "cached_tokens"),
        ("output_tokens_details", "reasoning_tokens"),
    ):
        details = usage.get(detail_key)
        if details is None:
            continue
        if not isinstance(details, dict) or not is_token_count(details.get(inner_key)):
            raise ProbeFailure(
                "usage_invalid",
                f"{where}.usage.{detail_key} is present but has no integer {inner_key}",
            )


def responses_output_text(output: list[Any]) -> tuple[str, list[str], bool]:
    """Return (text, item types, has message item) from a Responses output array."""
    texts: list[str] = []
    types: list[str] = []
    has_message = False
    for item in output:
        if not isinstance(item, dict):
            raise ProbeFailure("invalid_schema", "every output item must be an object")
        item_type = item.get("type")
        types.append(item_type if isinstance(item_type, str) else "?")
        if item_type != "message":
            continue
        has_message = True
        content = item.get("content")
        if not isinstance(content, list):
            raise ProbeFailure("invalid_schema", "a message output item must contain a content array")
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
    return "".join(texts), types[:20], has_message


def probe_responses(ctx: Context, url: str) -> dict[str, Any]:
    assert ctx.model is not None
    request = {"model": ctx.model, "input": ctx.prompt, "store": False}
    payload, metadata = request_json(url, ctx.api_key, ctx.timeout, request, sensitive_values=(ctx.prompt,))
    secrets = ctx.secrets
    if not isinstance(payload, dict):
        raise ProbeFailure("invalid_schema", "responses body must be a JSON object", **metadata)
    # A Response object always has an "error" key; it is null (or empty) on success.
    error_message = api_error_message(payload, secrets) if payload.get("error") else None
    if error_message is not None:
        raise ProbeFailure("api_error", error_message, **metadata)
    result: dict[str, Any] = dict(metadata)
    if payload.get("object") != "response":
        add_note(result, 'object is not "response"')
    response_id = payload.get("id")
    if isinstance(response_id, str) and response_id:
        result["response_id"] = redacted_excerpt(response_id, secrets, 512)
    else:
        add_note(result, "the response has no string id")
    status = payload.get("status")
    result["status"] = redacted_excerpt(str(status), secrets, 64)
    if status != "completed":
        reason = ""
        details = payload.get("incomplete_details")
        if isinstance(details, dict) and isinstance(details.get("reason"), str):
            reason = f" (reason: {redacted_excerpt(details['reason'], secrets, 64)})"
        raise ProbeFailure(
            "response_not_completed",
            f"status is {result['status']}, expected completed{reason}",
            **result,
        )
    output = payload.get("output")
    if not isinstance(output, list):
        raise ProbeFailure("invalid_schema", "responses body must contain an output array", **result)
    try:
        text, item_types, has_message = responses_output_text(output)
    except ProbeFailure as exc:
        raise exc.add_details(**result)
    result["output_item_types"] = item_types
    if not has_message or not text:
        hint = ""
        if isinstance(payload.get("output_text"), str):
            hint = "; top-level output_text is an SDK convenience, the raw API carries text in output[].content[]"
        raise ProbeFailure(
            "invalid_schema",
            f"output contains no message item with output_text{hint}",
            **result,
        )
    usage = payload.get("usage")
    if usage is None:
        raise ProbeFailure("usage_missing", "responses body has no usage object", **result)
    try:
        validate_responses_usage(usage, "response")
    except ProbeFailure as exc:
        raise exc.add_details(**result)
    result["usage"] = normalize_usage(usage)
    result.update(content_fields(text, ctx.include_content, secrets))
    if not ctx.responses_stream:
        return result
    try:
        stream_result = stream_responses_once(ctx, url)
    except ProbeFailure as exc:
        raise ProbeFailure(
            exc.kind,
            f"responses stream: {exc.message}",
            **result,
            stage="stream",
            stream={"ok": False, **exc.details},
        ) from None
    result["stream"] = {"ok": True, **stream_result}
    return result


def stream_responses_once(ctx: Context, url: str) -> dict[str, Any]:
    assert ctx.model is not None
    request = {"model": ctx.model, "input": ctx.prompt, "store": False, "stream": True}
    response, started, metadata = open_sse(url, ctx, request)
    try:
        return consume_responses_stream(response, started, metadata, ctx)
    finally:
        response.close()


def consume_responses_stream(response: Any, started: float, metadata: dict[str, Any], ctx: Context) -> dict[str, Any]:
    secrets = ctx.secrets
    event_count = 0
    first_event_ms: float | None = None
    ttft_ms: float | None = None
    delta_count = 0
    text_parts: list[str] = []
    item_text_parts: list[str] = []
    type_counts: dict[str, int] = {}
    done_item_types: list[str] = []
    completed_response: Any = None
    saw_completed = False
    notes: list[str] = []

    def progress() -> dict[str, Any]:
        return {
            **metadata,
            "latency_ms": elapsed_ms(started),
            "event_count": event_count,
            "event_types": dict(sorted(type_counts.items())),
        }

    try:
        for event_name, data in iter_sse_events(response, started, ctx.timeout):
            event_count += 1
            if event_count > MAX_SSE_EVENTS:
                raise ProbeFailure("too_many_sse_events", "SSE event count exceeded the safety limit")
            if first_event_ms is None:
                first_event_ms = elapsed_ms(started)
            if data == "[DONE]":
                break
            event = parse_sse_json(data)
            if not isinstance(event, dict):
                raise ProbeFailure("invalid_sse_schema", "SSE data must decode to a JSON object")
            event_type = event.get("type")
            if not isinstance(event_type, str) or not event_type:
                raise ProbeFailure(
                    "invalid_sse_schema",
                    "Responses SSE data must contain a string type field; Codex reads the event type from the data",
                )
            if event_name is not None and event_name != event_type:
                note = "an SSE event: line does not match the type field in its data"
                if note not in notes:
                    notes.append(note)
            safe_type = redacted_excerpt(event_type, secrets, 80)
            if safe_type in type_counts or len(type_counts) < 64:
                type_counts[safe_type] = type_counts.get(safe_type, 0) + 1
            if event_type == "error":
                message = event.get("message")
                if not isinstance(message, str):
                    message = api_error_message(event, secrets) or "error event without a message"
                raise ProbeFailure("api_error", redacted_excerpt(message, secrets))
            if event_type == "response.failed":
                failed = event.get("response")
                message = "response.failed"
                if isinstance(failed, dict):
                    message = api_error_message(failed, secrets) or message
                raise ProbeFailure("response_failed", redacted_excerpt(message, secrets))
            if event_type == "response.incomplete":
                reason = "unknown"
                incomplete = event.get("response")
                if isinstance(incomplete, dict):
                    details = incomplete.get("incomplete_details")
                    if isinstance(details, dict) and isinstance(details.get("reason"), str):
                        reason = redacted_excerpt(details["reason"], secrets, 64)
                raise ProbeFailure(
                    "response_not_completed", f"stream ended with response.incomplete (reason: {reason})"
                )
            if event_type == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    if ttft_ms is None:
                        ttft_ms = elapsed_ms(started)
                    text_parts.append(delta)
                    delta_count += 1
            elif event_type == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict):
                    item_type = item.get("type")
                    done_item_types.append(item_type if isinstance(item_type, str) else "?")
                    if item_type == "message":
                        text, _types, _has = responses_output_text([item])
                        item_text_parts.append(text)
            elif event_type == "response.completed":
                saw_completed = True
                completed_response = event.get("response")
                break
    except ProbeFailure as exc:
        raise exc.add_details(**progress())
    except NETWORK_ERRORS as exc:
        raise network_failure(exc, started).add_details(**metadata, event_count=event_count)
    if not saw_completed:
        raise ProbeFailure(
            "sse_incomplete",
            "stream ended before response.completed (Codex reports: stream closed before response.completed)",
            **progress(),
        )
    result: dict[str, Any] = {
        **metadata,
        "latency_ms": elapsed_ms(started),
        "first_event_ms": first_event_ms,
        "ttft_ms": ttft_ms,
        "event_count": event_count,
        "event_types": dict(sorted(type_counts.items())),
        "output_text_delta_count": delta_count,
        "output_item_done_types": done_item_types[:20],
        "response_completed": True,
    }
    for note in notes:
        add_note(result, note)
    if not isinstance(completed_response, dict):
        raise ProbeFailure("invalid_sse_schema", "response.completed must carry a response object", **result)
    if not isinstance(completed_response.get("id"), str):
        raise ProbeFailure(
            "invalid_sse_schema",
            "response.completed has no string response.id; Codex cannot parse it",
            **result,
        )
    status = completed_response.get("status")
    if status is not None and status != "completed":
        raise ProbeFailure(
            "response_not_completed",
            f"response.completed carries status {redacted_excerpt(str(status), secrets, 64)}",
            **result,
        )
    usage = completed_response.get("usage")
    if usage is None:
        add_note(result, "response.completed has no usage; clients cannot show token counts")
    else:
        try:
            validate_responses_usage(usage, "response.completed.response")
        except ProbeFailure as exc:
            raise exc.add_details(**result)
        result["usage"] = normalize_usage(usage)
    if "message" not in done_item_types:
        raise ProbeFailure(
            "invalid_sse_schema",
            "no response.output_item.done event with a message item; Codex records assistant output from these events",
            **result,
        )
    text = "".join(text_parts) or "".join(item_text_parts)
    if not text:
        raise ProbeFailure("empty_content", "the streamed response contained no output text", **result)
    result.update(content_fields(text, ctx.include_content, secrets))
    return result


def image_request(ctx: Context, image_url: str) -> dict[str, Any]:
    assert ctx.model is not None
    return {
        "model": ctx.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": IMAGE_PROMPT},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
    }


def run_image_probe(ctx: Context, url: str, image_url: str) -> tuple[dict[str, Any], str]:
    secrets = (ctx.api_key, image_url)
    payload, metadata = request_json(
        url, ctx.api_key, ctx.timeout, image_request(ctx, image_url), sensitive_values=(image_url,)
    )
    _choice, message, result = parse_chat_completion(payload, metadata, secrets)
    try:
        content = extract_text_content(message.get("content"))
    except ProbeFailure as exc:
        raise exc.add_details(**result)
    if not content.strip():
        raise ProbeFailure("empty_content", "the reply to the image request was empty", **result)
    result.update(content_fields(content, ctx.include_content, secrets))
    return result, content


def probe_image(ctx: Context, url: str) -> dict[str, Any]:
    result, content = run_image_probe(ctx, url, PROBE_PNG_DATA_URL)
    result["image_bytes"] = len(PROBE_PNG)
    result["image_format"] = "png 8x8 base64 data URL"
    result["color_named"] = IMAGE_EXPECTED_COLOR in content.lower()
    if not result["color_named"]:
        add_note(
            result,
            "the reply did not name the image color; the image may not have reached a vision model (informational)",
        )
    return result


def probe_image_url(ctx: Context, url: str) -> dict[str, Any]:
    assert ctx.image_url is not None
    result, _content = run_image_probe(ctx, url, ctx.image_url)
    result["image_url_host"] = urllib.parse.urlsplit(ctx.image_url).hostname or ""
    add_note(result, "remote image results depend on whether the image host lets the gateway fetch the URL")
    return result


def classify_error_shape(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return "openai_nested"
        if (
            "error" not in payload
            and isinstance(payload.get("message"), str)
            and ("code" in payload or "type" in payload)
        ):
            return "flat"
    return "other"


def probe_error_shape(ctx: Context, url: str) -> dict[str, Any]:
    metadata, body = fetch_raw(url, INVALID_PROBE_KEY, ctx.timeout, (ctx.api_key,))
    result: dict[str, Any] = {**metadata, "probe_key": "fixed invalid key (not yours)"}
    status = metadata["status_code"]
    if 200 <= status < 300:
        raise ProbeFailure(
            "auth_not_enforced",
            f"/models accepted an invalid API key (HTTP {status})",
            **result,
        )
    if status not in (401, 403):
        message = decode_error_message(body, (ctx.api_key,))
        raise ProbeFailure(
            classify_http_status(status),
            f"expected HTTP 401 or 403 for an invalid key, got {status}: {message}",
            **result,
        )
    try:
        parsed = strict_json_loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ProbeFailure(
            "error_body_not_json",
            f"the {status} error body is not JSON ({metadata.get('content_type') or 'no Content-Type'}); "
            "clients will show a generic error",
            **result,
        ) from None
    shape = classify_error_shape(parsed)
    result["error_shape"] = shape
    if not is_json_content_type(metadata.get("content_type", "")):
        add_note(result, "the error body parses as JSON but Content-Type is not application/json")
    source = parsed.get("error") if shape == "openai_nested" else parsed
    if isinstance(source, dict):
        for field in ("code", "type"):
            value = source.get(field)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                result[f"error_{field}"] = redacted_excerpt(str(value), (ctx.api_key,), 128)
        result["error_message_present"] = isinstance(source.get("message"), str) and bool(source.get("message"))
    if shape == "flat":
        add_note(
            result,
            'error body is flat {"code","message"}, not {"error":{...}}; the official Python SDK falls back '
            "to the top-level object, other clients may only show the HTTP status",
        )
    elif shape == "other":
        add_note(result, "error body is JSON but neither the OpenAI nested shape nor flat {code,message}")
    return result


def probe_latency(ctx: Context, url: str) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for run in range(1, ctx.runs + 1):
        try:
            outcome = stream_chat_once(ctx, url, include_usage=False)
            samples.append(
                {
                    "run": run,
                    "ok": True,
                    "ttft_ms": outcome.get("ttft_ms"),
                    "total_ms": outcome["latency_ms"],
                }
            )
        except ProbeFailure as exc:
            sample: dict[str, Any] = {"run": run, "ok": False, "error_kind": exc.kind}
            if "status_code" in exc.details:
                sample["status_code"] = exc.details["status_code"]
            samples.append(sample)
    successes = [sample for sample in samples if sample["ok"]]
    failures = len(samples) - len(successes)
    error_kinds: dict[str, int] = {}
    for sample in samples:
        if not sample["ok"]:
            error_kinds[sample["error_kind"]] = error_kinds.get(sample["error_kind"], 0) + 1
    ttfts = [sample["ttft_ms"] for sample in successes if sample["ttft_ms"] is not None]
    totals = [sample["total_ms"] for sample in successes]
    result: dict[str, Any] = {
        "runs": len(samples),
        "succeeded": len(successes),
        "failed": failures,
        "error_rate": round(failures / len(samples), 4),
        "errors": dict(sorted(error_kinds.items())),
        "ttft_ms": distribution(ttfts),
        "total_ms": distribution(totals),
        "percentile_method": "nearest-rank over successful runs",
        "mode": "sequential, one request at a time",
        "samples": samples,
    }
    if totals:
        result["latency_ms"] = result["total_ms"]["p50"]
    if len(ttfts) < len(successes):
        add_note(result, "some successful runs streamed no content text, so they have no TTFT")
    if failures:
        raise ProbeFailure(
            "latency_errors",
            f"{failures} of {len(samples)} sequential runs failed",
            **result,
        )
    return result


@dataclasses.dataclass(frozen=True)
class CheckSpec:
    name: str
    path: str
    needs_model: bool
    function: Callable[[Context, str], dict[str, Any]]


CHECKS: dict[str, CheckSpec] = {
    spec.name: spec
    for spec in (
        CheckSpec("models", "models", False, probe_models),
        CheckSpec("chat", "chat/completions", True, probe_chat),
        CheckSpec("stream", "chat/completions", True, probe_stream),
        CheckSpec("stream_usage", "chat/completions", True, probe_stream_usage),
        CheckSpec("tools", "chat/completions", True, probe_tools),
        CheckSpec("json_mode", "chat/completions", True, probe_json_mode),
        CheckSpec("responses", "responses", True, probe_responses),
        CheckSpec("image", "chat/completions", True, probe_image),
        CheckSpec("image_url", "chat/completions", True, probe_image_url),
        CheckSpec("error_shape", "models", False, probe_error_shape),
        CheckSpec("latency", "chat/completions", True, probe_latency),
    )
}
assert tuple(CHECKS) == ALL_CHECKS


# --------------------------------------------------------------------------
# Running checks and building the report
# --------------------------------------------------------------------------


def failure_result(name: str, url: str, failure: ProbeFailure, secrets: Iterable[str]) -> dict[str, Any]:
    return {
        "name": name,
        "endpoint": url,
        "ok": False,
        **failure.details,
        "error": {
            "kind": failure.kind,
            "message": redact_text(failure.message, secrets),
        },
    }


def run_check(name: str, ctx: Context) -> dict[str, Any]:
    spec = CHECKS[name]
    url = endpoint(ctx.base_url, spec.path)
    secrets = (*ctx.secrets, *((ctx.image_url,) if ctx.image_url else ()))
    try:
        details = spec.function(ctx, url)
        return {"name": name, "endpoint": url, "ok": True, **details}
    except ProbeFailure as exc:
        return failure_result(name, url, exc, secrets)
    except Exception as exc:  # Last-resort report stability; never expose a traceback or object repr.
        failure = ProbeFailure("internal_error", f"unexpected internal error ({type(exc).__name__})")
        return failure_result(name, url, failure, secrets)


def build_report(ctx: Context, selection: Selection, api_key_env: str) -> dict[str, Any]:
    results = [run_check(name, ctx) for name in selection.checks]
    passed = sum(1 for item in results if item["ok"])
    failed = len(results) - passed
    status = "ok" if failed == 0 else "failed" if passed == 0 else "degraded"
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": TOOL_NAME, "version": VERSION},
        "generated_at": utc_now(),
        "scope": SCOPE_NOTICE,
        "configuration": {
            "base_url": ctx.base_url,
            "profile": selection.profile,
            "checks": list(selection.checks),
            "api_key_env": api_key_env,
            "timeout_seconds": ctx.timeout,
            "model": ctx.model,
            "prompt_chars": len(ctx.prompt),
            "include_content": ctx.include_content,
            "options": {
                "tools_roundtrip": ctx.tools_roundtrip,
                "responses_stream": ctx.responses_stream,
                "image_url_host": urllib.parse.urlsplit(ctx.image_url).hostname if ctx.image_url else None,
                "runs": ctx.runs,
            },
        },
        "summary": {
            "checks_total": len(results),
            "failed": failed,
            "passed": passed,
            "status": status,
        },
        "checks": results,
    }
    extra = (ctx.image_url,) if ctx.image_url else ()
    return redact_tree(report, (ctx.api_key, *extra))


def render_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n"


_MD_ESCAPES = str.maketrans(
    {
        "\\": "\\\\",
        "|": "\\|",
        "`": "'",
        "*": "\\*",
        "_": "\\_",
        "[": "\\[",
        "]": "\\]",
        "<": "&lt;",
        ">": "&gt;",
        "#": "\\#",
    }
)


def md(value: Any) -> str:
    """Escape a value for a single Markdown table cell."""
    return " ".join(str(value).translate(_MD_ESCAPES).split())


def _ms(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:,.0f} ms"
    return "-"


def describe_check(check: Mapping[str, Any]) -> str:
    name = check.get("name")
    if not check.get("ok"):
        error = check.get("error") or {}
        stage = check.get("stage")
        prefix = f"{error.get('kind', 'error')}"
        if stage:
            prefix += f" (stage: {stage})"
        return f"{prefix}: {error.get('message', '')}"
    usage = check.get("usage") or {}
    if name == "models":
        text = f"{check.get('model_count')} models"
        if check.get("target_model_present"):
            text += ", target model listed"
        return text
    if name == "chat":
        return f"finish_reason={check.get('finish_reason', '-')}, total_tokens={usage.get('total_tokens', '-')}"
    if name == "stream":
        return f"{check.get('event_count')} events, [DONE] received, TTFT {_ms(check.get('ttft_ms'))}"
    if name == "stream_usage":
        return f"usage chunk before [DONE], total_tokens={usage.get('total_tokens', '-')}"
    if name == "tools":
        text = f"{check.get('tool_call_count')} call(s) to get_weather with JSON arguments"
        roundtrip = check.get("roundtrip")
        if isinstance(roundtrip, Mapping) and roundtrip.get("ok"):
            text += "; round trip answered"
        return text
    if name == "json_mode":
        return "content parsed as a JSON object"
    if name == "responses":
        text = f"status=completed, tokens in/out {usage.get('input_tokens', '-')}/{usage.get('output_tokens', '-')}"
        stream = check.get("stream")
        if isinstance(stream, Mapping) and stream.get("ok"):
            text += "; stream reached response.completed"
        return text
    if name == "image":
        return "8x8 PNG data URL accepted" + ("" if check.get("color_named") else " (color not named)")
    if name == "image_url":
        return f"remote image from {check.get('image_url_host', '-')} accepted"
    if name == "error_shape":
        return f"HTTP {check.get('status_code')}, body shape {check.get('error_shape')}"
    if name == "latency":
        ttft = check.get("ttft_ms") or {}
        total = check.get("total_ms") or {}
        return (
            f"{check.get('runs')} runs, TTFT p50/p95 {_ms(ttft.get('p50'))} / {_ms(ttft.get('p95'))}, "
            f"total p50/p95 {_ms(total.get('p50'))} / {_ms(total.get('p95'))}, errors {check.get('failed')}"
        )
    return ""


def render_markdown(report: Mapping[str, Any]) -> str:
    config = report.get("configuration") or {}
    summary = report.get("summary") or {}
    checks = report.get("checks") or []
    tool = report.get("tool") or {}
    lines = [
        f"## OpenAI-compatible API check: {summary.get('passed')}/{summary.get('checks_total')} passed "
        f"({md(summary.get('status'))})",
        "",
        "| | |",
        "|---|---|",
        f"| Endpoint | {md(config.get('base_url'))} |",
        f"| Model | {md(config.get('model') or '-')} |",
        f"| Profile | {md(config.get('profile') or 'custom')} |",
        f"| Generated | {md(report.get('generated_at'))} |",
        f"| Tool | {md(tool.get('name'))} {md(tool.get('version'))}, report schema {md(report.get('schema_version'))} |",
        "",
        "| Check | Result | HTTP | Time | Details |",
        "|---|---|---|---|---|",
    ]
    notes: list[str] = []
    for check in checks:
        latency = check.get("latency_ms")
        lines.append(
            f"| {md(check.get('name'))} | {'PASS' if check.get('ok') else 'FAIL'} | "
            f"{md(check.get('status_code', '-'))} | {md(_ms(latency))} | {md(describe_check(check))} |"
        )
        for note in check.get("notes") or []:
            notes.append(f"- {md(check.get('name'))}: {md(note)}")
        for nested_key in ("roundtrip", "stream"):
            nested = check.get(nested_key)
            if isinstance(nested, Mapping):
                for note in nested.get("notes") or []:
                    notes.append(f"- {md(check.get('name'))} ({nested_key}): {md(note)}")
    if notes:
        lines += ["", "**Notes**", "", *notes]
    lines += ["", f"> {md(report.get('scope', SCOPE_NOTICE))}", ""]
    return "\n".join(lines)


def write_report(destination: str, rendered: str) -> None:
    if destination == "-":
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe = rendered.encode(encoding, errors="backslashreplace").decode(encoding, errors="replace")
        sys.stdout.write(safe)
        sys.stdout.flush()
        return
    target = Path(destination).expanduser()
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def append_step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(markdown + "\n")
    except OSError as exc:
        sys.stderr.write(f"warning: could not write GITHUB_STEP_SUMMARY: {exc.strerror or type(exc).__name__}\n")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx, selection = validate_args(args, parser)
    report = build_report(ctx, selection, args.api_key_env)
    markdown = redact_text(render_markdown(report), (ctx.api_key,))
    rendered = render_json(report) if args.format == "json" else markdown
    try:
        write_report(args.output, rendered)
    except OSError as exc:
        parser.error(f"unable to write report: {exc}")
    if not args.no_step_summary:
        append_step_summary(markdown)
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
