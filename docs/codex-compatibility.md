# Codex 兼容性：为什么 Codex 需要 /v1/responses

> **English summary.** Codex CLI talks to model providers only through the Responses API: its configuration reference says `wire_api` accepts `responses` only (checked 2026-09-29). It POSTs to `{base_url}/responses` with `Accept: text/event-stream`, collects output items from `response.output_item.done`, and treats a stream that closes before `response.completed` as an error. A gateway whose Chat Completions endpoint works perfectly can therefore still be unusable from Codex. `oacheck --profile codex` checks exactly these points. It does not test Responses-API function calls, which Codex uses for shell and file edits, so finish with a real Codex smoke test.

## 结论

Codex CLI 只通过 Responses API 和模型服务通信。一个中转站或网关要能给 Codex 用，至少要满足：

1. 实现 `POST {base_url}/responses`；
2. 支持 `"stream": true`，按 Responses 的事件格式输出 SSE；
3. 流里出现 `response.output_item.done`（带完整的 message 或工具调用项），最后以 `response.completed` 结束；
4. `response.completed` 里的 `response.id` 是字符串，`usage` 如果有，token 字段都是整数。

Chat Completions 接口再稳定，也替代不了上面任何一条。

## Codex 怎样发请求

Codex 的配置参考（`~/.codex/config.toml`，2026-09-29 查阅）对自定义服务商的说明：

| 配置项 | 配置参考里的说明（摘要） |
|---|---|
| `model_provider` | 使用 `model_providers` 里的哪个服务商，默认 `openai` |
| `model_providers.<id>.base_url` | 服务商的 API 地址 |
| `model_providers.<id>.env_key` | 提供 API Key 的环境变量名 |
| `model_providers.<id>.wire_api` | 协议。`responses` 是唯一支持的值，省略时也是它 |
| `model_providers.<id>.stream_idle_timeout_ms` | SSE 流的空闲超时，默认 300000 毫秒 |
| `model_providers.<id>.stream_max_retries` | 流式中断后的重试次数，默认 5 |

`openai`、`ollama`、`lmstudio` 是保留的服务商 ID，自定义时要换一个名字。一个最小配置：

```toml
model = "your-model-id"
model_provider = "example"

[model_providers.example]
name = "Example gateway"
base_url = "https://api.example.com/v1"   # 包含 /v1，Codex 会在后面拼 /responses
env_key = "EXAMPLE_API_KEY"
wire_api = "responses"
```

从 Codex 源码看（提交 `88e9a83`，2026-09-28）：请求发往服务商 `base_url` 后面拼上 `/responses` 的地址，`Accept` 头是 `text/event-stream`；请求体里总是带 `stream` 和 `store` 两个字段。

## Codex 怎样读响应

同一提交的 `codex-rs/codex-api/src/sse/responses.rs` 里，和兼容性直接相关的规则：

| 事件 | Codex 的处理 |
|---|---|
| `response.output_text.delta` | 实时显示的文字增量 |
| `response.output_item.done` | 取出完整的输出项（消息、工具调用等） |
| `response.completed` | 结束本轮；反序列化 `response`，其中 `id` 必须是字符串，`usage` 若存在必须含整数的 `input_tokens`、`output_tokens`、`total_tokens` |
| `response.incomplete` | 除非原因是 `interrupted`，否则报错 "Incomplete response returned, reason: ..." |
| `response.failed` / `error` | 作为 API 错误返回 |
| 流结束但没见到 `response.completed` | 报错 "stream closed before response.completed" |
| 超过空闲超时没有新事件 | 报错 "idle timeout waiting for SSE" |

## 用 oacheck 对照 Codex 的报错

```bash
read -rs OPENAI_API_KEY && export OPENAI_API_KEY
oacheck --base-url https://api.example.com/v1 --model your-model-id --profile codex --format markdown
```

`codex` profile 跑 `models`、`responses`（非流式加流式）和 Chat 的 `stream`，共 3 次生成请求。

| Codex 的现象或报错 | oacheck 里对应的结果 | 常见原因 |
|---|---|---|
| 请求 `/responses` 返回 404 | `responses` 失败，`not_found` | 网关只实现了 Chat Completions |
| 401 | `models` 失败，`authentication_error` | `env_key` 指定的环境变量没导出，或 Key 无效 |
| "stream closed before response.completed" | `responses` 失败，stage `stream`，`sse_incomplete` | 网关没发 `response.completed`；反向代理截断了流尾 |
| "failed to parse ResponseCompleted" | `usage_invalid`，或 `invalid_sse_schema`（缺 `id`） | `usage` 里 token 是小数或字符串，缺 `total_tokens`，或者 `response.id` 缺失 |
| "Incomplete response returned, reason: ..." | `response_not_completed` | 输出长度上限、内容过滤等，原因写在错误消息里 |
| Codex 拿不到完整的输出项 | `invalid_sse_schema`：没有 message 的 `output_item.done` | 网关只转发了文字增量，没有发 `response.output_item.done`；Codex 从这个事件取完整的消息和工具调用 |
| "idle timeout waiting for SSE" | `timeout`（可调大 `--timeout` 复测） | 上游思考时间长、网关不发心跳；可以在 Codex 里调大 `stream_idle_timeout_ms` |

## 通过之后还要做什么

`codex` profile 通过，只说明 Codex 最基本的一问一答能走通。它**没有**测：

- Responses API 下的函数调用。Codex 执行命令、修改文件都靠它；
- 推理项（reasoning items）的往返、上下文压缩等更深的协议细节；
- 长时间会话的稳定性。

所以最后请用真实的 Codex 跑一条非交互命令，再让它执行一个会调用 shell 的小任务：

```bash
codex exec "Reply with the single word: ready"
```

## 资料来源

均于 2026-09-29 查阅：

- Codex Configuration Reference（`wire_api`、`env_key`、`stream_idle_timeout_ms`、保留的服务商 ID）：<https://learn.chatgpt.com/docs/config-file/config-reference>（原地址 <https://developers.openai.com/codex/config-reference> 308 跳转至此）
- Codex 源码，Responses 端点路径与 `Accept` 头：<https://github.com/openai/codex/blob/88e9a8329d17bd141e4f3f0e7c78d07a7baa8b23/codex-rs/codex-api/src/endpoint/responses.rs#L123-L150>
- Codex 源码，Responses SSE 解析（`ResponseCompleted` 结构、各事件处理、"stream closed before response.completed"）：<https://github.com/openai/codex/blob/88e9a8329d17bd141e4f3f0e7c78d07a7baa8b23/codex-rs/codex-api/src/sse/responses.rs>
- Codex 源码，请求体中的 `store`、`stream` 字段：<https://github.com/openai/codex/blob/88e9a8329d17bd141e4f3f0e7c78d07a7baa8b23/codex-rs/codex-api/src/common.rs#L280-L295>
- OpenAI API Reference, Responses streaming events：<https://developers.openai.com/api/reference/resources/responses/streaming-events>
