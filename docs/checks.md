# 检查项详解：请求体、通过标准与常见失败

> **English summary.** For every check this page lists the exact request body, what counts as a pass, the `error.kind` values it can report, and the usual causes of failure. Rules follow the OpenAI API reference and, for the Responses stream, the parser in the Codex CLI source. Sources and access dates are at the end.

所有检查都遵循同一套规则：

- 请求只发往 `--base-url` 下的固定路径，不跟随重定向。
- JSON 响应必须带 `application/json`（或 `+json`）的 Content-Type，按 UTF-8 严格解析，拒绝 `NaN`、`Infinity` 和孤立的 UTF-16 代理字符。
- 流式响应必须是 `text/event-stream`，逐字节解析，兼容 `\n`、`\r\n`、`\r` 换行、UTF-8 BOM 和 `:` 注释行。
- HTTP 状态码先于内容判定：400/422 记为 `bad_request`，401/403 为 `authentication_error`，404 为 `not_found`，429 为 `rate_limited`，5xx 为 `server_error`，3xx 为 `redirect_rejected`。
- 例外：错误响应如果是 CDN/WAF 的拦截页（目前识别 Cloudflare 的三种格式：纯文本 `error code: 1xxx`、带 `"cloudflare_error": true` 的 JSON、`cf-mitigated` 响应头），记为 `blocked_by_cdn`，不再按状态码归类。这类请求没有到达 API，结果和 Key 无关。
- `notes` 里是不影响通过与否的观察，例如"usage 块没有 choices 字段"。

默认提示词是 `Reply with exactly OK.`，可以用 `--prompt` 替换；替换后的提示词不会写入报告，出现在上游错误消息里时也会被打码。

## models

```http
GET {base_url}/models
Authorization: Bearer <key>
```

**通过：** HTTP 200，JSON 对象，`data` 是数组，每项都有非空字符串 `id`。指定了 `--model` 时，该 ID 必须出现在列表里。报告记录模型数量和前 25 个 ID。

| 失败 | 含义 |
|---|---|
| `model_not_listed` | 列表里没有 `--model` 指定的 ID。以实时返回的 ID 为准，不要用网页上的展示名 |
| `invalid_schema` | `data` 不是数组，或某项缺 `id` |
| `api_error` | HTTP 200 但返回体里带 `error` 对象 |
| `unexpected_content_type` | 返回了 HTML，常见于 Base URL 指到了网站首页或 WAF 拦截页 |

## chat

```json
{"model": "<model>", "messages": [{"role": "user", "content": "<prompt>"}], "stream": false}
```

**通过：** `choices[0].message.content` 存在，是字符串、文本片段数组或 `null`。报告记录 `finish_reason`、`usage`、回复长度和 SHA-256，不记录正文。

常见失败：`invalid_json`（返回体损坏）、`invalid_schema`（没有 `choices` 或 `message`）、`api_error`。

## stream

```json
{"model": "<model>", "messages": [{"role": "user", "content": "<prompt>"}], "stream": true}
```

**通过：** Content-Type 为 `text/event-stream`；每个 `data:` 都能解析成带 `choices` 数组的 JSON 对象；至少有一个非空 `choices` 事件；最后收到 `data: [DONE]`。

报告里的两个时间：

- `first_event_ms`：从发出请求到收到第一个 SSE 数据事件。很多网关第一个事件只带 `role`，没有文字。
- `ttft_ms`：从发出请求到第一段非空 `delta.content`，这才是用户感受到的"首字"。

| 失败 | 含义 |
|---|---|
| `sse_incomplete` | 连接在 `[DONE]` 之前结束。常见原因是代理空闲超时、缓冲区截断或上游中断 |
| `unexpected_content_type` | `stream: true` 却返回普通 JSON，说明网关不支持流式或把它关掉了 |
| `invalid_sse_json` / `invalid_sse_schema` | 事件不是 JSON，或缺 `choices` |
| `invalid_sse_encoding` | 流里有非法 UTF-8，常见于网关按字节切分多字节字符 |

## stream_usage

```json
{"model": "<model>", "messages": [{"role": "user", "content": "<prompt>"}], "stream": true,
 "stream_options": {"include_usage": true}}
```

OpenAI 的 Chat Completions 参考写明：设置 `include_usage` 后，会在 `data: [DONE]` 之前多发一个块，其中 `usage` 是整个请求的用量，`choices` 恒为空数组；其他块的 `usage` 为 `null`；流被中断时可能收不到这个块。

**通过：** 满足 `stream` 的全部条件，并且 `[DONE]` 之前出现过一个 `usage` 对象，其中 `prompt_tokens`、`completion_tokens`、`total_tokens` 都是非负整数。

| 结果 | 含义 |
|---|---|
| `usage_missing` | 没有 usage 块，或它出现在 `[DONE]` 之后。客户端（包括很多计费和监控组件）会统计不到用量 |
| `usage_invalid` | 有 usage 块，但字段缺失或不是整数 |
| 通过，带 note | usage 块没有 `choices` 字段，或 `choices` 不为空。按索引读取 `choices[0]` 的客户端可能出错 |
| 通过，带 note | `total_tokens` 不等于 `prompt_tokens + completion_tokens` |

## tools

```json
{
  "model": "<model>",
  "messages": [{"role": "user", "content": "What is the weather in Paris right now? Call the get_weather tool; do not answer from memory."}],
  "tools": [{
    "type": "function",
    "function": {
      "name": "get_weather",
      "description": "Get the current weather for a city.",
      "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name, for example Paris"}},
        "required": ["city"],
        "additionalProperties": false
      }
    }
  }],
  "tool_choice": "auto"
}
```

这里用 `tool_choice: "auto"` 加明确的指令，而不是强制指定某个函数。这样测的是最常见的调用方式，也避开了各家实现在强制调用时对 `finish_reason` 的不同处理。

**通过：**

1. `message.tool_calls` 是非空数组；
2. 每个调用都有非空字符串 `id`，`function.name` 为 `get_weather`；
3. `function.arguments` 是**字符串**，按 JSON 解析后是对象。OpenAI 参考写明它是"JSON 格式的参数"，并提醒模型不一定总生成合法 JSON、调用方要自行校验；
4. `finish_reason` 为 `tool_calls`。

报告记录调用数、参数的键名（不记录值）、`id` 是否都以 `call_` 开头（只记录，不判定）。

**`--tools-roundtrip`（`agent`、`full` 默认开启）** 会再发一次请求，把工具结果按标准格式发回去：

```json
[
  {"role": "user", "content": "..."},
  {"role": "assistant", "content": null, "tool_calls": [{"id": "<id>", "type": "function", "function": {"name": "get_weather", "arguments": "<原样返回>"}}]},
  {"role": "tool", "tool_call_id": "<id>", "content": "{\"city\": \"Paris\", \"temperature_c\": 21, \"condition\": \"sunny\"}"}
]
```

要求模型给出非空的最终回答，且不再调用工具。回答里是否出现 `21` 只记录（`mentions_tool_result`），不判定。

| 失败 | 含义 |
|---|---|
| `no_tool_call` | 没有 `tool_calls`。若回复用了已弃用的 `function_call` 字段，错误消息会点明 |
| `tool_call_missing_id` | 调用缺 `id`，工具结果无法对应到调用 |
| `tool_call_wrong_name` | 调用了别的函数名 |
| `tool_arguments_not_string` | `arguments` 是对象而不是 JSON 字符串。按字符串解析的客户端会直接报错 |
| `tool_arguments_not_json` / `tool_arguments_not_object` | 参数不是合法 JSON，或解析出来不是对象 |
| `unexpected_finish_reason` | 有调用，但 `finish_reason` 不是 `tool_calls`。依赖它决定下一步的 Agent 框架会停住 |
| `roundtrip_no_final_answer`（stage `roundtrip`） | 发回结果后又调用工具，或回复为空 |
| `bad_request`（stage `roundtrip`） | 网关不接受 `role: tool` 消息或 `tool_call_id` |

## json_mode

```json
{
  "model": "<model>",
  "messages": [
    {"role": "system", "content": "You are a JSON API. Respond with a single JSON object and nothing else."},
    {"role": "user", "content": "Return a JSON object with the keys \"status\" (the string \"ok\") and \"n\" (the integer 1)."}
  ],
  "response_format": {"type": "json_object"}
}
```

OpenAI 的 Structured Outputs 指南要求：使用 JSON 模式时，必须在对话里明确要求模型输出 JSON，并且上下文中没有 "JSON" 这个词时 API 会报错。所以系统消息和用户消息都写了 JSON。

**通过：** 回复内容去掉首尾空白后能直接解析成 JSON 对象。报告记录键名。

| 失败 | 含义 |
|---|---|
| `invalid_json_output` | 内容不是 JSON。若内容以 ```` ``` ```` 开头，错误消息会提示"被 Markdown 代码块包起来"，这通常说明网关丢掉了 `response_format` |
| `json_output_not_object` | 是 JSON，但不是对象（比如数组） |
| `bad_request` | 网关或模型不支持 `response_format` |

JSON 模式只保证输出是合法 JSON，不保证符合某个 schema。本检查不测 `json_schema` 严格模式。

## responses

```json
{"model": "<model>", "input": "<prompt>", "store": false}
```

`store: false` 是为了不在服务端留存这次探测。Responses API 的 `store` 省略时默认为 `true`，响应会保存至少 30 天（OpenAI 参考，2026-09-29 查阅）。Codex 的请求体里总是带着 `store` 字段，所以网关若拒绝这个字段，Codex 同样会出错。

**通过（非流式）：**

1. `status` 为 `completed`；
2. `output` 是数组，其中有 `type: "message"` 的项，内容里有 `type: "output_text"` 的文本；
3. 有 `usage` 对象，`input_tokens`、`output_tokens`、`total_tokens` 都是非负整数；若带 `input_tokens_details` 或 `output_tokens_details`，其中的 `cached_tokens`、`reasoning_tokens` 也必须是整数。

第 3 条按 Codex 源码里 `ResponseCompleted` 的反序列化规则来定：这些字段类型不对，Codex 会报 "failed to parse ResponseCompleted"。

注意：SDK 里常用的 `response.output_text` 是 SDK 拼出来的便捷属性，原始 API 返回体里的文字在 `output[].content[]` 中。只有顶层 `output_text` 的网关会被判为 `invalid_schema`，错误消息会说明原因。

**`--responses-stream`（`codex`、`full` 默认开启）** 再发一次 `"stream": true` 的请求，要求：

1. Content-Type 为 `text/event-stream`，每个事件的 JSON 都有字符串 `type`；
2. 出现过 `response.output_item.done`，其中的 `item.type` 为 `message`。Codex 从这类事件里取完整的输出项；
3. 最终收到 `response.completed`，其 `response.id` 是字符串，`status`（若有）为 `completed`，`usage`（若有）符合上面的整数规则；
4. 有文本输出（来自 `response.output_text.delta` 或完成的 message 项）。

| 失败 | 含义 |
|---|---|
| `not_found` | `/responses` 返回 404，网关只实现了 Chat Completions |
| `response_not_completed` | `status` 不是 `completed`，或流里出现 `response.incomplete`；错误消息带上 `incomplete_details.reason` |
| `response_failed` | 流里出现 `response.failed` |
| `api_error` | 流里出现 `type: "error"` 事件 |
| `sse_incomplete`（stage `stream`） | 流在 `response.completed` 之前结束。Codex 此时报 "stream closed before response.completed" |
| `invalid_sse_schema`（stage `stream`） | 事件缺 `type`，没有 message 的 `output_item.done`，或 `response.completed` 缺 `id` |
| `usage_missing` / `usage_invalid` | 非流式缺 `usage`，或任一处 token 字段不是整数 |

为什么 Codex 离不开这一项，见 [codex-compatibility.md](codex-compatibility.md)。

## image 与 image_url

```json
{
  "model": "<model>",
  "messages": [{"role": "user", "content": [
    {"type": "text", "text": "This image is one solid color. Reply with the name of the color only."},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo..."}}
  ]}]
}
```

图片是代码里现场生成的 8×8 纯红 PNG（`solid_png()`，不依赖任何图像库，74 字节）。OpenAI 参考写明 `image_url.url` 可以是图片 URL，也可以是 base64 编码的图片数据。

**通过：** HTTP 200，回复非空。回复里有没有 "red" 记录在 `color_named` 里，没说出颜色时加一条 note，提示图片可能没有真正送到视觉模型，但不判失败：颜色判断属于模型行为，不属于协议。

`image_url` 检查把 data URL 换成 `--image-url` 给的远程地址。远程图片要由网关（或上游）去下载，结果取决于图床是否允许对方抓取：防盗链、需要 Cookie、地区限制都会导致失败。报告里只保留图床的主机名，完整 URL 会被打码，因为签名链接里常带令牌。

| 失败 | 含义 |
|---|---|
| `bad_request` | 模型或网关不支持图片输入，或无法抓取远程图片 |
| `empty_content` | 请求成功但回复为空 |
| `unexpected_content_type` | 返回了 HTML 错误页 |

## error_shape

```http
GET {base_url}/models
Authorization: Bearer oacheck-invalid-key-for-error-shape-probe
```

这一项**不使用你的 Key**，而是用写死在代码里的假 Key，看网关怎样拒绝无效凭据。

**通过：** 返回 401 或 403，响应体能解析成 JSON。报告错误体的形状：

| `error_shape` | 形状 | 说明 |
|---|---|---|
| `openai_nested` | `{"error": {"message": "...", "type": "...", "code": "..."}}` | 与 OpenAI 一致 |
| `flat` | `{"code": "...", "message": "..."}` | 常见于自建网关。OpenAI 的 Python SDK（openai-python）取 `body.get("error", body)`，没有 `error` 键时退回整个对象，所以仍能拿到 `code`；其他客户端可能只显示 HTTP 状态码 |
| `other` | 其他 JSON，比如 `{"error": "..."}` | 客户端能否读出消息取决于实现 |

形状只报告，不算失败：协议没有规定兼容网关的错误体必须和 OpenAI 一模一样。

| 失败 | 含义 |
|---|---|
| `auth_not_enforced` | 假 Key 也拿到了 2xx，鉴权没有生效 |
| `error_body_not_json` | 401/403 的响应体是 HTML 或纯文本，客户端只能显示笼统的错误 |
| `blocked_by_cdn` | 401/403 来自 API 前面的 CDN/WAF 拦截页，不是网关对假 Key 的回应，所以无法判断错误体形状 |
| 其他 HTTP 类 | 例如 404（路径错）、5xx，或 3xx 跳转到登录页 |

## latency

对 `/chat/completions` 串行发送 `--runs` 次（默认 5，上限 50）和 `stream` 相同的请求，一次结束再发下一次，不并发，也不自动重试。

**通过：** 所有请求都成功。有任何一次失败，检查就失败（`latency_errors`），但报告照样给出成功请求的统计和每类错误的次数。

报告字段：

- `ttft_ms` 和 `total_ms`：各含 `count`、`p50`、`p95`、`min`、`max`；
- `error_rate`：失败次数 / 总次数；`errors`：按 `error.kind` 计数；
- `samples`：每次请求的结果和耗时，便于自己重新计算。

分位数的算法和解读见 [latency.md](latency.md)。

## 资料来源

均于 2026-09-29 查阅：

- OpenAI API Reference, Create chat completion（`stream_options.include_usage`、`response_format`、`image_url`、`tool_calls`、`finish_reason`）：<https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create>
- OpenAI API Reference, Chat Completions streaming events：<https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events>
- OpenAI API Reference, Create a response（`store` 默认值、`status`、`output_text` 为 SDK 便捷属性）：<https://developers.openai.com/api/reference/resources/responses/methods/create>
- OpenAI API Reference, Responses streaming events（`response.completed`、`response.failed`、`response.incomplete`、`error`）：<https://developers.openai.com/api/reference/resources/responses/streaming-events>
- OpenAI Structured Outputs 指南（JSON 模式必须明确要求输出 JSON）：<https://developers.openai.com/api/docs/guides/structured-outputs>
- OpenAI Images and vision 指南（base64 data URL 和图片 URL 输入）：<https://developers.openai.com/api/docs/guides/images-vision>
- OpenAI Error codes 指南：<https://developers.openai.com/api/docs/guides/error-codes>
- openai-python 按 `body.get("error", body)` 解析错误体（提交 `68b173a`）：<https://github.com/openai/openai-python/blob/68b173a24fa9ebfc6b84694a39a0dd0f9a1e6087/src/openai/_client.py#L858>
- Codex CLI 解析 Responses 流（提交 `88e9a83`）：<https://github.com/openai/codex/blob/88e9a8329d17bd141e4f3f0e7c78d07a7baa8b23/codex-rs/codex-api/src/sse/responses.rs>
- Codex CLI 请求体结构，`store` 字段总是序列化（同一提交）：<https://github.com/openai/codex/blob/88e9a8329d17bd141e4f3f0e7c78d07a7baa8b23/codex-rs/codex-api/src/common.rs#L289>
- WHATWG HTML Standard, Server-sent events（SSE 的行与字段规则）：<https://html.spec.whatwg.org/multipage/server-sent-events.html>
