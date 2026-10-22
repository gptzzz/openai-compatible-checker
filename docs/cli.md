# 命令行参考：参数、退出码、报告与错误分类

> **English summary.** All command-line options of `oacheck`, its exit codes, the structure of the JSON report and every `error.kind` value with the usual cause and next step.

单文件方式下命令是 `python3 openai_compatible_checker.py`；PyPI 上架后也可以直接用 `oacheck` 或 `openai-compatible-checker`，参数完全相同。

## 参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--base-url` | 必填 | API 前缀，例如 `https://api.example.com/v1` |
| `--model` | 无 | 模型 ID；除 `models`、`error_shape` 外的检查都需要 |
| `--api-key-env` | `OPENAI_API_KEY` | 存放 Key 的环境变量名。没有 `--api-key` 参数，这是有意的 |
| `--profile` | `basic` | `basic`、`codex`、`agent`、`full` |
| `--check` | 无 | 只跑指定检查，可重复；不能和 `--profile` 同用 |
| `--tools-roundtrip` / `--no-tools-roundtrip` | 随 profile | 把工具结果发回去，检查最终回答 |
| `--responses-stream` / `--no-responses-stream` | 随 profile | 另外流式请求 `/responses`，要求出现 `response.completed` |
| `--image-url` | 无 | 另测一张远程图片 |
| `--runs` | `5` | `latency` 的串行请求次数，1 到 50 |
| `--timeout` | `60` | 单个请求的超时秒数，0.1 到 600 |
| `--prompt` | `Reply with exactly OK.` | `chat`、`stream`、`responses`、`latency` 用的提示词，不写入报告 |
| `--include-content` | 关 | 在报告里保留最多 4096 个字符的回复正文 |
| `--format` | `json` | `json` 或 `markdown` |
| `--output` | `-` | 报告路径，`-` 表示标准输出；写文件是原子替换 |
| `--no-step-summary` | 关 | 在 GitHub Actions 里不写 Job Summary |
| `--list-checks` | | 列出全部检查和 profile |
| `--version` | | 显示版本号 |

几条组合规则：

- profile 默认开启的选项可以单独关掉，例如 `--profile full --no-responses-stream`。
- 传了 `--image-url` 且选中了 `image`，会自动在它后面加上 `image_url`。
- 工具遵循系统的 `HTTPS_PROXY` / `HTTP_PROXY` 环境变量。
- 误传的未知参数值会先打码再报错，避免 Key 出现在终端输出里。

## 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 全部检查通过 |
| `1` | 至少一项检查失败 |
| `2` | 参数、环境变量或报告写入出错 |

## 报告结构

默认输出 JSON，便于存档和程序比对；`--format markdown` 输出表格，适合贴进 Issue 或工单。Markdown 报告的样子见仓库里的 [examples/report-sample.md](https://github.com/gptzzz/openai-compatible-checker/blob/main/examples/report-sample.md)（由仓库自带的本地 mock 网关生成，数字只用来演示格式）。

JSON 报告的顶层结构：

```json
{
  "schema_version": "2.0",
  "tool": {"name": "openai-compatible-checker", "version": "2.0.0"},
  "scope": "Checks protocol compatibility only. A passing report does not prove which model or provider serves the endpoint, and a single run is not an SLA.",
  "configuration": {"base_url": "https://api.example.com/v1", "profile": "full", "model": "your-model-id", "...": "..."},
  "summary": {"checks_total": 10, "passed": 10, "failed": 0, "status": "ok"},
  "checks": [
    {"name": "tools", "ok": true, "status_code": 200, "finish_reason": "tool_calls",
     "tool_call_count": 1, "argument_keys": ["city"],
     "roundtrip": {"ok": true, "finish_reason": "stop", "mentions_tool_result": true}}
  ]
}
```

- `summary.status`：`ok` 全部通过，`degraded` 部分失败，`failed` 全部失败。
- 默认不保存提示词和模型回复，只记录长度和 SHA-256（`content_sha256`），用来比较两次回复是否变化。
- 只有显式加 `--include-content` 时才写入回复正文。这种报告可能含提示词相关数据，不要公开。
- 失败的检查带 `error.kind` 和 `error.message`；分阶段的检查（工具回传、Responses 流式）还带 `stage` 字段，指出失败发生在哪一步。
- `notes` 里是不影响通过与否的观察。
- 报告字段只增不改：改名或删除字段会升 `schema_version` 主版本，并写迁移说明。

## 错误分类

| `error.kind` | 常见原因 | 下一步 |
|---|---|---|
| `authentication_error` | 401/403，Key 无效或没有权限 | 核对环境变量、Key 状态和模型权限 |
| `blocked_by_cdn` | 错误来自 API 前面的 CDN/WAF 拦截页（如 Cloudflare `error code: 1010`），请求没有到达 API | 和 Key 无关；向服务商确认是否按 User-Agent 或 IP 拦截 |
| `bad_request` | 400/422，参数不被支持 | 看错误消息里点名的参数；常见于 `tools`、`response_format`、图片 |
| `not_found` | 404，路径错误或接口未实现 | 检查是否漏写或重复写 `/v1`；`/responses` 404 说明不支持 Codex |
| `rate_limited` | 429，额度或并发限制 | 查看 `retry_after`，降低频率并核对余额 |
| `server_error` | 网关或上游 5xx | 记下 UTC 时间和 `request_id` 后重试 |
| `redirect_rejected` | 端点返回 3xx | 直接填写最终 API 地址，不通过跳转传递 Key |
| `timeout` | 建连、读取或流式事件超时 | 调大 `--timeout`，分时段复测 |
| `dns_error` / `tls_error` / `network_error` | DNS、证书链、TLS 握手或连接失败 | 检查域名解析、系统时间、证书链和代理设置 |
| `unexpected_content_type` | JSON 接口返回 HTML，或流式接口返回普通 JSON | 检查反向代理、WAF，以及是否支持 `stream: true` |
| `invalid_json` / `invalid_schema` | 返回体损坏，或兼容结构不完整 | 对照 OpenAI 协议和网关的转换日志 |
| `api_error` | HTTP 200 里仍然带着错误对象或错误事件 | 查看错误消息和网关日志 |
| `invalid_sse_json` / `invalid_sse_schema` / `invalid_sse_encoding` | SSE 事件不是合法 JSON、缺字段或不是 UTF-8 | 检查流式协议适配层 |
| `sse_incomplete` | 流在 `[DONE]` 或 `response.completed` 之前断开 | 检查代理缓冲、空闲超时和上游中断 |
| `model_not_listed` | 指定模型不在 `/models` 里 | 以实时返回的模型 ID 为准，不要用展示名猜测 |
| `usage_missing` / `usage_invalid` | 没有 usage，或 token 字段缺失、不是整数 | 检查网关是否转发了用量块或 `usage` 对象 |
| `no_tool_call` / `unexpected_finish_reason` | 没有调用工具，或调用了但 `finish_reason` 不是 `tool_calls` | 确认模型支持工具调用，网关没有改写 `finish_reason` |
| `tool_call_missing_id` / `tool_call_wrong_name` | 工具调用缺 `id`，或函数名不对 | 缺 `id` 时工具结果无法和调用对应 |
| `tool_arguments_not_string` / `tool_arguments_not_json` / `tool_arguments_not_object` | `arguments` 不是 JSON 字符串，或解析不出对象 | 调用方通常直接对 `arguments` 做 JSON 解析，这里出错会让 Agent 中断 |
| `roundtrip_no_final_answer` | 发回工具结果后，模型又调用工具或回复为空 | 检查网关是否正确转发 `role: tool` 消息和 `tool_call_id` |
| `invalid_json_output` / `json_output_not_object` | JSON 模式下内容不是合法 JSON 对象 | 常见原因是网关丢掉了 `response_format` |
| `response_not_completed` / `response_failed` | Responses 的 `status` 不是 `completed`，或收到 `response.failed` / `response.incomplete` | 查看 `incomplete_details.reason` 和错误消息 |
| `empty_content` | 请求成功但回复为空 | 换一个模型复测，检查网关的内容过滤 |
| `error_body_not_json` / `auth_not_enforced` | 假 Key 请求返回 HTML，或假 Key 也被接受 | 前者让客户端只能显示笼统错误；后者说明鉴权没有生效 |
| `latency_errors` | `--runs` 次请求里有失败 | 看 `errors` 里的分类统计 |

HTTP 错误正文会先按 API Key 和提示词脱敏，再截取最多 300 个字符，避免长密钥被截断后残留前缀。每项检查会报告哪些 `error.kind`，见 [检查项详解](checks.md)。
