"""Local mock of an OpenAI-compatible gateway, used by the tests and the CI Action smoke test.

The first path segment selects a behavior ("mode"), so one server can play a
healthy gateway and many broken ones:

    http://127.0.0.1:<port>/<mode>/v1/models
    http://127.0.0.1:<port>/<mode>/v1/chat/completions
    http://127.0.0.1:<port>/<mode>/v1/responses

Run standalone:  python tests/mock_server.py --port 8787
Nothing here talks to the network beyond 127.0.0.1.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MOCK_API_KEY = os.environ.get("OACHECK_MOCK_API_KEY", "sk-local-test-secret-1234567890")

FULL_USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
RESPONSES_USAGE = {
    "input_tokens": 9,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens": 2,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 11,
}


def sse(payload, event=None):
    body = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {body}\n\n".encode()


def chat_chunk(delta, finish_reason=None, **extra):
    return {
        "id": "chatcmpl-stream",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        **extra,
    }


def completion(message, finish_reason="stop", usage=None):
    return {
        "id": "chatcmpl-local",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish_reason}],
        "usage": usage or FULL_USAGE,
    }


def responses_message(text):
    return {
        "id": "msg_local",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def responses_body(status="completed", output=None, usage=RESPONSES_USAGE, **extra):
    body = {
        "id": "resp_local",
        "object": "response",
        "status": status,
        "model": "gpt-test",
        "output": [responses_message("OK")] if output is None else output,
        "error": None,
    }
    if usage is not None:
        body["usage"] = usage
    body.update(extra)
    return body


# The "demo" mode adds fixed delays so sample reports in the docs show non-zero timings.
DEMO_DELAY_SECONDS = {"models": 0.08, "json": 0.45, "first_event": 0.30, "between_events": 0.04}


def payload_streams(payload):
    return isinstance(payload, dict) and bool(payload.get("stream"))


class RelayMockHandler(BaseHTTPRequestHandler):
    api_key = MOCK_API_KEY
    requests_seen: list = []
    counters: dict = {}
    lock = threading.Lock()

    def log_message(self, _format, *_args):
        return

    # ---- routing helpers -------------------------------------------------

    def _mode(self):
        parts = [part for part in self.path.split("?", 1)[0].split("/") if part]
        return parts[0] if parts else "ok"

    def _route(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.endswith("/responses"):
            return "responses"
        if path.endswith("/chat/completions"):
            return "chat"
        if path.endswith("/models"):
            return "models"
        return "unknown"

    def _count(self, key):
        with self.lock:
            value = self.counters.get(key, 0) + 1
            self.counters[key] = value
        return value

    def _record(self, payload=None):
        self.__class__.requests_seen.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "payload": payload,
            }
        )

    # ---- low-level senders ----------------------------------------------

    def _send_bytes(self, status, body, content_type, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", "req-local-123")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _send_json(self, status, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", headers)

    def _send_sse(self, frames):
        demo = self._mode() == "demo"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Request-ID", "req-stream-local")
        self.end_headers()
        try:
            for index, frame in enumerate(frames):
                if demo:
                    time.sleep(DEMO_DELAY_SECONDS["first_event" if index == 0 else "between_events"])
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    # ---- auth and forced errors -----------------------------------------

    def _authorize_or_reject(self, mode):
        if mode == "no-auth":
            return True
        if self.headers.get("Authorization") == f"Bearer {self.api_key}":
            return True
        if mode == "flat-error":
            self._send_json(401, {"code": "INVALID_API_KEY", "message": "invalid api key"})
        elif mode == "string-error":
            self._send_json(401, {"error": "invalid api key"})
        elif mode == "html-error":
            self._send_bytes(401, b"<html><body>401 Unauthorized</body></html>", "text/html")
        else:
            self._send_json(
                401,
                {
                    "error": {
                        "message": "missing or invalid bearer token",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
            )
        return False

    def _forced_http_error(self, mode):
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "/ok/v1/models")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        if mode == "auth-error":
            leaked = self.headers.get("Authorization", "missing")
            self._send_json(401, {"error": {"message": f"rejected {leaked}"}})
            return True
        if mode in {"bare-key-error", "header-key-error"}:
            leaked = self.headers.get("Authorization", "missing")
            if leaked.startswith("Bearer "):
                leaked = leaked[len("Bearer ") :]
            if mode == "bare-key-error":
                self._send_json(401, {"error": {"message": leaked}})
            else:
                body = b'{"error":{"message":"rejected"}}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Request-ID", f"prefix-{leaked}")
                self.end_headers()
                self.wfile.write(body)
            return True
        if mode == "rate-limit":
            self._send_json(429, {"error": {"message": "quota exhausted"}}, {"Retry-After": "3"})
            return True
        # A CDN/WAF block page in front of the API, in Cloudflare's plain-text and JSON formats.
        if mode == "cdn-block":
            self._send_bytes(403, b"error code: 1010", "text/plain; charset=UTF-8", {"Server": "cloudflare"})
            return True
        if mode == "cdn-block-json":
            self._send_json(
                403,
                {
                    "title": "Error 1010: Access denied",
                    "status": 403,
                    "detail": "The site owner has blocked access based on your browser's signature.",
                    "error_code": 1010,
                    "cloudflare_error": True,
                },
                {"Server": "cloudflare"},
            )
            return True
        if mode == "server-error":
            self._send_json(503, {"error": {"message": "temporary upstream failure"}})
            return True
        return False

    # ---- GET /models ------------------------------------------------------

    def do_GET(self):
        mode = self._mode()
        self._record()
        if self._forced_http_error(mode):
            return
        if not self._authorize_or_reject(mode):
            return
        if mode == "slow":
            time.sleep(0.35)
        if mode == "demo":
            time.sleep(DEMO_DELAY_SECONDS["models"])
        if mode == "bad-json":
            self._send_bytes(200, b"{not-json", "application/json")
        elif mode == "nan-json":
            self._send_bytes(200, b'{"data":[],"value":NaN}', "application/json")
        elif mode == "surrogate-json":
            self._send_bytes(200, b'{"data":[{"id":"\\ud800"}]}', "application/json")
        elif mode == "unicode-model":
            self._send_json(200, {"data": [{"id": "模型-😀"}]})
        elif mode == "truncated-json-body":
            body = b'{"data":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body) + 100))
            self.end_headers()
            self.wfile.write(body)
        elif mode == "wrong-type":
            self._send_bytes(200, b"<html>not json</html>", "text/html")
        elif mode == "bad-model-shape":
            self._send_json(200, {"object": "list", "data": {}})
        elif mode == "models-api-error":
            self._send_json(200, {"error": {"message": "logical API failure"}})
        elif mode == "empty-api-error":
            self._send_json(200, {"error": {"message": ""}})
        elif mode == "models-missing":
            self._send_json(200, {"object": "list", "data": [{"id": "other-model"}]})
        else:
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [{"id": "gpt-test", "object": "model"}, {"id": "other-model", "object": "model"}],
                },
            )

    # ---- POST -------------------------------------------------------------

    def do_POST(self):
        mode = self._mode()
        route = self._route()
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        self._record(payload)
        if self._forced_http_error(mode):
            return
        if not self._authorize_or_reject(mode):
            return
        if mode == "slow":
            time.sleep(0.35)
        if mode == "demo" and not payload_streams(payload):
            time.sleep(DEMO_DELAY_SECONDS["json"])
        if not isinstance(payload, dict):
            self._send_json(400, {"error": {"message": "invalid request JSON"}})
            return
        if mode == "prompt-error":
            prompt = payload.get("messages", [{}])[0].get("content", "")
            self._send_json(400, {"error": {"message": prompt}})
            return
        if mode == "chat-prompt-api-error":
            prompt = payload.get("messages", [{}])[0].get("content", "")
            self._send_json(200, {"error": {"message": prompt}})
            return
        if route == "responses":
            self._responses(mode, payload)
            return
        if route != "chat":
            self._send_json(404, {"error": {"message": "unknown route"}})
            return
        if payload.get("tools"):
            self._tools(mode, payload)
        elif payload.get("response_format"):
            self._json_mode(mode)
        elif self._has_image(payload):
            self._image(mode, payload)
        elif payload.get("stream"):
            self._stream_response(mode, payload)
        else:
            self._chat_response(mode, payload)

    # ---- chat -------------------------------------------------------------

    def _chat_response(self, mode, payload):
        if mode == "bad-chat-json":
            self._send_bytes(200, b"[broken", "application/json")
        elif mode == "bad-chat-shape":
            self._send_json(200, {"id": "chatcmpl-local", "choices": []})
        elif mode == "wrong-type":
            self._send_bytes(200, b"not json", "text/plain")
        else:
            content = self.api_key if mode == "echo-key" else "assistant reply marker"
            prompt = payload.get("messages", [{}])[0].get("content", "")
            body = completion({"content": content})
            if mode == "chat-metadata-prompt":
                body["id"] = prompt
                body["choices"][0]["finish_reason"] = prompt
            self._send_json(200, body)

    def _stream_response(self, mode, payload):
        if mode == "json-stream":
            self._send_json(200, {"id": "not-an-event-stream"})
            return
        if mode == "wrong-type":
            self._send_bytes(200, b"not an event stream", "text/plain")
            return
        if mode == "flaky" and self._count("flaky-stream") % 2 == 0:
            self._send_json(503, {"error": {"message": "temporary upstream failure"}})
            return
        include_usage = bool((payload.get("stream_options") or {}).get("include_usage"))
        if mode == "bad-sse-json":
            frames = [b"data: {broken-json\n\n", b"data: [DONE]\n\n"]
        elif mode == "nan-sse":
            frames = [b'data: {"choices":[],"value":NaN}\n\n', b"data: [DONE]\n\n"]
        elif mode == "empty-sse":
            frames = [b'data: {"choices":[]}\n\n', b"data: [DONE]\n\n"]
        elif mode == "bom-cr-sse":
            frames = [
                b'\xef\xbb\xbfdata: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\r\rdata: [DONE]\r\r'
            ]
        elif mode == "sse-api-error":
            frames = [b'data: {"error":{"message":"upstream stream failed"}}\n\n', b"data: [DONE]\n\n"]
        elif mode == "invalid-utf8-sse":
            frames = [b"data: \xff\n\n"]
        elif mode == "truncated-sse":
            frames = [sse(chat_chunk({"content": "partial"}))]
        else:
            frames = [
                b": keep-alive\r\n\r\n",
                sse(chat_chunk({"role": "assistant", "content": ""})),
                sse(chat_chunk({"content": "stream "})),
                sse(chat_chunk({"content": "reply marker"})),
            ]
            if include_usage and mode == "usage-with-choices":
                frames.append(sse(chat_chunk({}, "stop", usage=FULL_USAGE)))
            else:
                frames.append(sse(chat_chunk({}, "stop")))
            if include_usage:
                if mode == "bad-usage":
                    frames.append(sse({"id": "chatcmpl-stream", "choices": [], "usage": {"total_tokens": "10"}}))
                elif mode == "usage-no-choices":
                    frames.append(sse({"id": "chatcmpl-stream", "usage": FULL_USAGE}))
                elif mode not in {"no-usage", "usage-with-choices", "usage-after-done"}:
                    frames.append(sse({"id": "chatcmpl-stream", "choices": [], "usage": FULL_USAGE}))
            frames.append(b"data: [DONE]\r\n\r\n")
            if include_usage and mode == "usage-after-done":
                frames.append(sse({"id": "chatcmpl-stream", "choices": [], "usage": FULL_USAGE}))
        self._send_sse(frames)

    # ---- tools ------------------------------------------------------------

    def _tools(self, mode, payload):
        messages = payload.get("messages") or []
        if any(isinstance(item, dict) and item.get("role") == "tool" for item in messages):
            if mode == "roundtrip-400":
                self._send_json(400, {"error": {"message": "messages with role tool are not supported"}})
            elif mode == "roundtrip-loop":
                self._send_json(200, completion({"content": None, "tool_calls": [self._call()]}, "tool_calls"))
            elif mode == "roundtrip-empty":
                self._send_json(200, completion({"content": ""}))
            else:
                self._send_json(200, completion({"content": "It is 21 C and sunny in Paris."}))
            return
        if mode == "tool-no-call":
            self._send_json(200, completion({"content": "It is probably sunny in Paris."}))
        elif mode == "tool-args-not-json":
            self._send_json(
                200, completion({"content": None, "tool_calls": [self._call(arguments="{city: Paris")]}, "tool_calls")
            )
        elif mode == "tool-args-object":
            self._send_json(
                200,
                completion({"content": None, "tool_calls": [self._call(arguments={"city": "Paris"})]}, "tool_calls"),
            )
        elif mode == "tool-no-id":
            call = self._call()
            del call["id"]
            self._send_json(200, completion({"content": None, "tool_calls": [call]}, "tool_calls"))
        elif mode == "tool-wrong-name":
            self._send_json(200, completion({"content": None, "tool_calls": [self._call(name="lookup")]}, "tool_calls"))
        elif mode == "tool-finish-stop":
            self._send_json(200, completion({"content": None, "tool_calls": [self._call()]}, "stop"))
        elif mode == "tool-legacy-function-call":
            self._send_json(
                200,
                completion(
                    {"content": None, "function_call": {"name": "get_weather", "arguments": "{}"}}, "function_call"
                ),
            )
        else:
            self._send_json(200, completion({"content": None, "tool_calls": [self._call()]}, "tool_calls"))

    @staticmethod
    def _call(arguments='{"city":"Paris"}', name="get_weather"):
        return {"id": "call_mock_1", "type": "function", "function": {"name": name, "arguments": arguments}}

    # ---- JSON mode --------------------------------------------------------

    def _json_mode(self, mode):
        if mode == "json-400":
            self._send_json(400, {"error": {"message": "response_format is not supported"}})
            return
        content = {
            "json-not-json": 'Sure! Here it is: {"status": "ok", "n": 1}',
            "json-fenced": '```json\n{"status": "ok", "n": 1}\n```',
            "json-array": "[1, 2]",
        }.get(mode, '{"status": "ok", "n": 1}')
        self._send_json(200, completion({"content": content}))

    # ---- images -----------------------------------------------------------

    @staticmethod
    def _image_urls(payload):
        urls = []
        for message in payload.get("messages") or []:
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        urls.append((part.get("image_url") or {}).get("url", ""))
        return urls

    def _has_image(self, payload):
        return bool(self._image_urls(payload))

    def _image(self, mode, payload):
        url = self._image_urls(payload)[0]
        is_data_url = url.startswith("data:image/png;base64,")
        if mode == "image-400" or (mode == "image-url-400" and not is_data_url):
            self._send_json(400, {"error": {"message": "image input is not supported for this model"}})
        elif mode == "image-empty":
            self._send_json(200, completion({"content": ""}))
        elif mode == "image-html":
            self._send_bytes(200, b"<html>gateway error</html>", "text/html")
        elif mode == "image-no-color":
            self._send_json(200, completion({"content": "I cannot see the image."}))
        else:
            self._send_json(200, completion({"content": "Red"}))

    # ---- Responses API ----------------------------------------------------

    def _responses(self, mode, payload):
        if mode == "responses-404":
            self._send_json(404, {"error": {"message": "Invalid URL (POST /v1/responses)"}})
            return
        if payload.get("stream"):
            self._responses_stream(mode)
            return
        if mode == "responses-incomplete":
            body = responses_body(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        elif mode == "responses-no-text":
            body = responses_body(output=[{"id": "rs_1", "type": "reasoning", "summary": []}])
        elif mode == "responses-output-text-only":
            body = responses_body(output=[], output_text="OK")
        elif mode == "responses-no-usage":
            body = responses_body(usage=None)
        elif mode == "responses-float-usage":
            body = responses_body(usage={"input_tokens": 9.0, "output_tokens": 2.0, "total_tokens": 11.0})
        else:
            body = responses_body()
        self._send_json(200, body)

    def _responses_stream(self, mode):
        if mode == "rs-json":
            self._send_json(200, responses_body())
            return
        created = responses_body(status="in_progress", output=[], usage=None)
        message = responses_message("OK")
        frames = [
            sse({"type": "response.created", "response": created, "sequence_number": 0}, "response.created"),
            sse({"type": "response.in_progress", "response": created, "sequence_number": 1}, "response.in_progress"),
            sse(
                {"type": "response.output_item.added", "item": {**message, "status": "in_progress", "content": []}},
                "response.output_item.added",
            ),
            sse(
                {"type": "response.output_text.delta", "item_id": "msg_local", "delta": "O"},
                "response.output_text.delta",
            ),
            sse(
                {"type": "response.output_text.delta", "item_id": "msg_local", "delta": "K"},
                "response.output_text.delta",
            ),
            sse(
                {"type": "response.output_text.done", "item_id": "msg_local", "text": "OK"}, "response.output_text.done"
            ),
        ]
        if mode != "rs-no-item":
            frames.append(sse({"type": "response.output_item.done", "item": message}, "response.output_item.done"))
        if mode == "rs-no-completed":
            pass
        elif mode == "rs-failed":
            failed = responses_body(
                status="failed", usage=None, error={"code": "server_error", "message": "upstream failed"}
            )
            frames.append(sse({"type": "response.failed", "response": failed}, "response.failed"))
        elif mode == "rs-incomplete":
            incomplete = responses_body(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
            frames.append(sse({"type": "response.incomplete", "response": incomplete}, "response.incomplete"))
        elif mode == "rs-error-event":
            frames.append(sse({"type": "error", "code": "rate_limit", "message": "slow down", "param": None}, "error"))
        elif mode == "rs-bad-usage":
            bad = responses_body(usage={"input_tokens": 9, "output_tokens": 2})
            frames.append(sse({"type": "response.completed", "response": bad}, "response.completed"))
        elif mode == "rs-no-id":
            done = responses_body()
            del done["id"]
            frames.append(sse({"type": "response.completed", "response": done}, "response.completed"))
        else:
            frames.append(sse({"type": "response.completed", "response": responses_body()}, "response.completed"))
        self._send_sse(frames)


def start_server(host="127.0.0.1", port=0):
    server = ThreadingHTTPServer((host, port), RelayMockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def main():
    parser = argparse.ArgumentParser(description="Run the local mock OpenAI-compatible gateway.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), RelayMockHandler)
    print(f"mock gateway on http://{args.host}:{server.server_port}/ok/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
