# API 中转站检测工具：OpenAI 兼容接口一键体检（Responses / 工具调用 / 流式 / JSON 模式）

**检查的是协议兼容性，不证明模型真伪或来源。** `oacheck` 用一条命令检查任意 OpenAI 兼容接口（API 中转站、自建网关、本地推理服务都可以）的模型列表、SSE 流式、工具调用、JSON 模式、Responses API（决定 Codex 能不能用）、图片输入、错误格式和首字延迟，输出脱敏的 JSON 或 Markdown 报告。单文件，只用 Python 标准库，也能作为 GitHub Action 定时运行。

[![CI](https://github.com/gptzzz/openai-compatible-checker/actions/workflows/ci.yml/badge.svg)](https://github.com/gptzzz/openai-compatible-checker/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> **English:** a conformance checker for OpenAI-compatible APIs (CLI + GitHub Action). It checks protocol compatibility only and does not prove which model or provider serves an endpoint. See the [English section](#english).

## 目录

- [30 秒快速开始](#30-秒快速开始)
- [OpenAI 兼容接口测试覆盖哪些检查](#openai-兼容接口测试覆盖哪些检查)
- [按场景选 profile](#按场景选-profile)
- [中转站能否跑 Codex：Responses 兼容性检测](#中转站能否跑-codexresponses-兼容性检测)
- [SSE 流式检测与首字延迟 TTFT](#sse-流式检测与首字延迟-ttft)
- [报告示例](#报告示例)
- [错误分类与处理](#错误分类与处理)
- [用 GitHub Actions 定时检测中转站](#用-github-actions-定时检测中转站)
- [安装方式](#安装方式)
- [参数参考](#参数参考)
- [安全默认值](#安全默认值)
- [结果解读边界](#结果解读边界)
- [从 ai-api-relay-checker 升级](#从-ai-api-relay-checker-升级)
- [常见问题](#常见问题)
- [English](#english)
- [参与贡献](#参与贡献)

## 30 秒快速开始

需要 Python 3.10 或更高版本，一个 OpenAI 兼容的 Base URL，和一个有少量额度的 API Key。Base URL 填 **API 前缀**，通常形如 `https://api.example.com/v1`。工具不会自动补 `/v1`，因为路径本身就是检查对象。

**macOS / Linux：**

```bash
# 1. 把 Key 放进环境变量。read -s 不回显，也不会进 shell 历史
read -rs OPENAI_API_KEY && export OPENAI_API_KEY

# 2. 下载单文件（只用 Python 标准库），跑全套检查，把结果打印成 Markdown 表格
curl -fsSLO https://raw.githubusercontent.com/gptzzz/openai-compatible-checker/v2.0.0/openai_compatible_checker.py
python3 openai_compatible_checker.py --base-url https://api.example.com/v1 \
  --model your-model-id --profile full --format markdown
```

> PyPI 包（`pipx run openai-compatible-checker`）还在准备上架，上架前请用上面的单文件方式，效果相同。

**Windows PowerShell 7：**

```powershell
$env:OPENAI_API_KEY = Read-Host -MaskInput 'API key'
python .\openai_compatible_checker.py --base-url https://api.example.com/v1 `
  --model your-model-id --profile full --format markdown
```

退出码：`0` 全部通过，`1` 至少一项检查失败，`2` 参数、环境变量或报告写入出错。脚本和 CI 可以直接按退出码分支。

<details>
<summary>示例：检测维护方自己的网关</summary>

下面这条命令检测的是本仓库维护方 GPTZZZ 运营的网关，只作为一个例子。换成任何 OpenAI 兼容地址，报告格式完全相同，检测逻辑也没有任何针对它的特殊处理。

```bash
read -rs GPTZZZ_API_KEY && export GPTZZZ_API_KEY
oacheck --base-url https://gptzzz.ai/v1 --model gpt-5.6 \
  --api-key-env GPTZZZ_API_KEY --profile full --format markdown
```

> **2026-09-29 复测：10/10 通过**（`gpt-5.6`，full 档）。当天早些时候，工具用 `openai-compatible-checker/2.0.0` 作 User-Agent，被 gptzzz.ai 前面的 CDN 返回 HTTP 403（`error code: 1010`）；现在工具改用简短的 `occ/2.0.0 (...)` 标识，也能把这类拦截识别为 `blocked_by_cdn`，不会误报成 Key 错误。

</details>

## OpenAI 兼容接口测试覆盖哪些检查

"能回一句话"不等于接口可用。接入失败往往出在具体某一层：流式断在 `[DONE]` 之前、用量块被网关吞掉、工具参数不是合法 JSON、只支持 Chat 不支持 Responses、错误体是一张 HTML 页。每项检查单独记录，失败时给出稳定的 `error.kind`。

| 检查 | 发出的请求 | 通过标准 | 常见失败原因 |
|---|---|---|---|
| `models` | `GET /models` | JSON 列表，每项有 `id`；指定 `--model` 时目标 ID 在列表中 | Base URL 漏写或重复写 `/v1`；Key 没有该模型权限 |
| `chat` | `POST /chat/completions` | 有 `choices[0].message.content`，记录 `finish_reason` 和 `usage` | 返回体不是 JSON；字段结构不完整 |
| `stream` | 同上，`stream: true` | `text/event-stream`，每个 `data:` 都是合法 JSON，最后是 `data: [DONE]`；记录首字延迟 | 反向代理缓冲或截断；中途断流 |
| `stream_usage` | 流式请求加 `stream_options.include_usage` | `[DONE]` 之前有一个 usage 块，三个 token 字段都是非负整数 | 网关丢掉 usage 块，客户端统计不到用量 |
| `tools` | 带一个 `get_weather` 函数 | `finish_reason` 为 `tool_calls`，调用带 `id`，`arguments` 是 JSON 字符串且解析为对象；`--tools-roundtrip` 会把工具结果发回去，要求模型给出最终回答 | 参数不是 JSON；缺 `id`；仍在用旧的 `function_call` |
| `json_mode` | `response_format: {"type": "json_object"}` | 回复内容能直接解析成 JSON 对象 | 内容被 Markdown 代码块包起来；网关忽略了该参数 |
| `responses` | `POST /responses` | `status` 为 `completed`，`output` 里有 message 文本，`usage` 字段齐全；`--responses-stream` 另外要求流里出现 `response.output_item.done` 和 `response.completed` | 404（只支持 Chat）；流在 `response.completed` 之前结束 |
| `image` | 代码现场生成的 8×8 PNG，base64 data URL | HTTP 200 且回复非空；模型有没有说出颜色只记录，不判定 | 400，模型或网关不支持图片 |
| `image_url` | `--image-url` 给出的远程图片 | 同上 | 图床不允许网关抓取 |
| `error_shape` | 用写死的假 Key 请求 `/models` | 返回 401 或 403，响应体是 JSON；报告错误体形状（`openai_nested`、`flat` 或 `other`），形状本身不算失败 | 返回 HTML 错误页；假 Key 也能通过；被 CDN 拦截（`blocked_by_cdn`） |
| `latency` | `--runs` 次串行流式请求 | 全部成功；输出首字延迟（TTFT）和总耗时的 p50、p95，以及错误率 | 429 限流、超时、偶发 5xx |

每项检查的请求体、判定细节和出处见 [docs/checks.md](docs/checks.md)。

## 按场景选 profile

| profile | 适合什么时候用 | 包含的检查 | 默认额外开启 | 生成回复的请求数 |
|---|---|---|---|---|
| `basic`（默认） | 刚拿到 Key，先确认能用 | `models` `chat` `stream` | 无 | 2 |
| `codex` | 准备接 Codex CLI | `models` `responses` `stream` | `--responses-stream` | 3 |
| `agent` | 接 Agent 框架，需要工具调用和结构化输出 | `tools` `json_mode` `stream_usage` | `--tools-roundtrip` | 4 |
| `full` | 全面体检、定时监控 | 除 `image_url` 外的全部检查 | 两项都开 | 9 + `--runs`（默认共 14） |

- `/models` 和假 Key 请求一般不计费，以服务商规则为准；其余请求都会消耗少量额度。
- 用 `--check` 可以只跑某几项，例如 `--check tools --check json_mode`。`--check` 和 `--profile` 不能同时用。
- 默认开启的选项可以关掉，例如 `--profile full --no-responses-stream`。
- 传了 `--image-url` 且选中了 `image`，会自动在它后面加上 `image_url`。
- `oacheck --list-checks` 列出全部检查和 profile。

## 中转站能否跑 Codex：Responses 兼容性检测

Codex CLI 只走 Responses API：它的配置参考写明 `wire_api` 只支持 `responses`（2026-09-29 查阅）。所以 Chat 接口再稳，只要 `POST /v1/responses` 不通，或者流式响应在 `response.completed` 之前就结束，Codex 就用不了。

```bash
oacheck --base-url https://api.example.com/v1 --model your-model-id --profile codex
```

`codex` profile 会非流式、流式各请求一次 `/responses`，并按 Codex 源码里的解析规则检查 `response.completed` 的 `id` 和 `usage` 字段。原理、出处和常见报错对照见 [docs/codex-compatibility.md](docs/codex-compatibility.md)。

## SSE 流式检测与首字延迟 TTFT

- `stream` 检查逐字节解析 SSE：兼容 `\r\n`、`\r`、UTF-8 BOM 和注释行，拒绝非法 UTF-8、损坏的 JSON 和没有 `[DONE]` 的流。
- `stream_usage` 检查 usage 块是否在 `[DONE]` 之前送达。OpenAI 文档说明，流被中断时可能收不到这个块，所以它缺失通常意味着网关或代理截断了流尾。
- `latency` 串行发出 `--runs` 次请求（默认 5，上限 50），不做并发压测。TTFT 从发出请求计到第一段非空内容，不是第一个 SSE 事件；p50 和 p95 用 nearest-rank 算法，只统计成功的请求，失败单独计数。

指标定义、样本量的影响和对比方法见 [docs/latency.md](docs/latency.md)。

## 报告示例

默认输出 JSON，便于存档和程序比对；`--format markdown` 输出表格，适合贴进 Issue 或工单。在 GitHub Actions 里运行时，Markdown 摘要还会自动写进 Job Summary。

下表由仓库自带的本地 mock 网关生成（demo 模式人为加了延迟），数字只用来演示格式：

| Check | Result | HTTP | Time | Details |
|---|---|---|---|---|
| models | PASS | 200 | 86 ms | 2 models, target model listed |
| chat | PASS | 200 | 451 ms | finish\_reason=stop, total\_tokens=10 |
| stream | PASS | 200 | 531 ms | 5 events, \[DONE\] received, TTFT 390 ms |
| stream\_usage | PASS | 200 | 563 ms | usage chunk before \[DONE\], total\_tokens=10 |
| tools | PASS | 200 | 455 ms | 1 call(s) to get\_weather with JSON arguments; round trip answered |
| json\_mode | PASS | 200 | 456 ms | content parsed as a JSON object |
| responses | PASS | 200 | 456 ms | status=completed, tokens in/out 9/2; stream reached response.completed |
| image | PASS | 200 | 456 ms | 8x8 PNG data URL accepted |
| error\_shape | PASS | 401 | 3 ms | HTTP 401, body shape openai\_nested |
| latency | PASS | - | 513 ms | 5 runs, TTFT p50/p95 387 ms / 396 ms, total p50/p95 513 ms / 529 ms, errors 0 |

JSON 报告的顶层结构（完整样例见 [examples/report-sample.json](examples/report-sample.json)）：

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
- 只有显式加 `--include-content` 时才写入最多 4096 个字符的回复正文。这种报告可能含提示词相关数据，不要公开。
- 失败的检查带 `error.kind` 和 `error.message`；分阶段的检查（工具回传、Responses 流式）还带 `stage` 字段，指出失败发生在哪一步。

## 错误分类与处理

| `error.kind` | 常见原因 | 下一步 |
|---|---|---|
| `authentication_error` | 401/403，Key 无效或没有权限 | 核对环境变量、Key 状态和模型权限 |
| `blocked_by_cdn` | 403 等错误来自 API 前面的 CDN/WAF 拦截页（如 Cloudflare `error code: 1010`），请求没有到达 API | 和 Key 无关；向服务商确认是否按 User-Agent 或 IP 拦截 |
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
| `tool_arguments_not_string` / `tool_arguments_not_json` / `tool_arguments_not_object` | `arguments` 不是 JSON 字符串，或解析不出对象 | 按协议 `arguments` 是 JSON 字符串，调用方通常直接做 JSON 解析，这里出错会让 Agent 中断 |
| `roundtrip_no_final_answer` | 发回工具结果后，模型又调用工具或回复为空 | 检查网关是否正确转发 `role: tool` 消息和 `tool_call_id` |
| `invalid_json_output` / `json_output_not_object` | JSON 模式下内容不是合法 JSON 对象 | 常见原因是网关丢掉了 `response_format` |
| `response_not_completed` / `response_failed` | Responses 的 `status` 不是 `completed`，或收到 `response.failed` / `response.incomplete` | 查看 `incomplete_details.reason` 和错误消息 |
| `empty_content` | 请求成功但回复为空 | 换一个模型复测，检查网关的内容过滤 |
| `error_body_not_json` / `auth_not_enforced` | 假 Key 请求返回 HTML，或假 Key 也被接受 | 前者让客户端只能显示笼统错误；后者说明鉴权没有生效 |
| `latency_errors` | `--runs` 次请求里有失败 | 看 `errors` 里的分类统计 |

HTTP 错误正文会先按 API Key 和提示词脱敏，再截取最多 300 个字符，避免长密钥被截断后残留前缀。

## 用 GitHub Actions 定时检测中转站

仓库本身就是一个 composite Action：

```yaml
- uses: gptzzz/openai-compatible-checker@v2
  env:
    OPENAI_API_KEY: ${{ secrets.GATEWAY_API_KEY }}
  with:
    base-url: https://api.example.com/v1
    model: your-model-id
    profile: full
    runs: 5
```

- Key 只从环境变量读，所以请用 `env:` 从 Secrets 传入，不要写成 Action 输入。
- 每次运行都会产生少量计费请求（`full` 默认 14 次），定时频率请按预算设置。
- 结果写进 Job Summary，完整 JSON 报告作为 artifact 上传；输出 `status`、`passed`、`failed`、`report-path` 可供后续步骤使用。
- 默认任何检查失败都会让这一步失败，方便收到通知。只想记录不想报红，就设 `fail-on-error: "false"`。
- Runner 上需要 Python 3.10+。GitHub 托管的 runner 通常自带；找不到合适版本时 Action 会报错，提示你先加 `actions/setup-python`。

完整的每日定时示例见 [examples/monitor.yml](examples/monitor.yml)。

## 安装方式

| 方式 | 命令 | 说明 |
|---|---|---|
| pipx 临时运行（PyPI 上架后） | `pipx run openai-compatible-checker --help` | 不污染环境 |
| pipx 安装（PyPI 上架后） | `pipx install openai-compatible-checker` | 之后直接用 `oacheck` |
| pip（PyPI 上架后） | `pip install openai-compatible-checker` | 也可以 `python -m openai_compatible_checker` |
| 单文件 | 下载 `openai_compatible_checker.py` | 零依赖，复制到任何有 Python 3.10+ 的机器都能跑 |
| GitHub Action | `uses: gptzzz/openai-compatible-checker@v2` | 见上一节 |

## 参数参考

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
| `--list-checks` | | 列出检查和 profile |

工具遵循系统的 `HTTPS_PROXY` / `HTTP_PROXY` 环境变量。

## 安全默认值

- API Key **只能**从环境变量读取，命令行里没有 `--api-key`；误传的未知参数值会被打码后再报错。
- 不跟随 HTTP 重定向，避免把 `Authorization` 头带到另一个地址。
- HTTPS 使用 Python 默认的证书校验，没有跳过校验的开关。
- `error_shape` 用一个写死的假 Key 发请求，你的 Key 不会出现在那次请求里。
- 报告默认不含提示词和回复正文；Key、Bearer Token 和常见密钥形态在最终输出前统一再脱敏一次，`--image-url` 的完整地址也会被打码，只保留主机名。
- JSON 大小、错误正文、SSE 单行、总字节数和事件数都有上限。
- `latency` 串行执行，上限 50 次，工具本身不能用来压测或放大流量。

漏洞报告方式见 [SECURITY.md](SECURITY.md)。

## 结果解读边界

- 单次通过只说明"这个时间点、这个 Key、这个模型、这组最小请求"可用，不代表长期 SLA。
- `/models` 返回的 ID 和模型自报的身份，都不能证明底层模型的来源。本工具不做真伪判定，也不输出任何"掺假率"一类的结论。
- 首字延迟包含本地网络、网关排队和上游生成时间，推理类模型还包含思考时间，不能直接当成服务端的计算耗时。
- 工具不做压力测试、计费复算和长上下文测试。

更多说明见 [docs/interpreting-results.md](docs/interpreting-results.md)。如果要把单次检测扩展成可重复的协议、性能、故障和账单验收，可以参考 [API 中转站检测指南：协议、性能、故障与账单四步验收](https://gptzzz.ai/blog/api-zhongzhuan-testing-guide/?utm_source=github&utm_medium=repo&utm_campaign=openai-compatible-checker&utm_content=further_reading)（维护方 GPTZZZ 撰写）。

## 从 ai-api-relay-checker 升级

本项目原名 `ai-api-relay-checker`，2.0.0 起改名。旧仓库地址会自动跳转到这里。

- 旧脚本 `ai_api_relay_checker.py` 仍然保留，内部转发到新文件，会在标准错误里提示改名；3.0.0 删除。
- 报告 `schema_version` 从 `1.0` 升到 `2.0`，`tool.name` 改为 `openai-compatible-checker`，HTTP 400/422 的 `error.kind` 从 `http_error` 改为 `bad_request`，默认超时从 15 秒改为 60 秒。
- 不传 `--profile` 和 `--check` 时，行为和 1.x 一样：只跑 `models`、`chat`、`stream`。退出码含义不变。

完整迁移说明见 [CHANGELOG.md](CHANGELOG.md)。

## 常见问题

**能判断中转站给的是不是"真模型"吗？**
不能，也不打算做。`/models` 的列表、模型自报的名字、回答风格都可以被中间层改写或模仿，拿它们当证据会误判。本工具只回答一个可验证的问题：这个接口是否按 OpenAI 兼容协议正确工作。

**Chat 接口正常，为什么 Codex 连不上？**
Codex 只走 Responses API。跑一次 `--profile codex`，看 `responses` 检查是 404、字段不全，还是流在 `response.completed` 之前断了。详见 [docs/codex-compatibility.md](docs/codex-compatibility.md)。

**首字延迟多少算正常？**
没有通用阈值。它取决于模型（尤其是推理类模型）、地区、网络和时段。只在同一模型、同一提示词、同一网络、同一时段下比较才有意义，见 [docs/latency.md](docs/latency.md)。

**跑一次要花多少钱？**
每次检查只发很短的请求。`basic` 2 次生成请求，`full` 默认 14 次，具体价格按服务商的计费规则。

**报告能直接贴到 Issue 或发给客服吗？**
默认报告不含 Key、提示词和回复正文，一般可以。贴之前仍建议自己看一遍；加了 `--include-content` 的报告不要公开。

**支持其他厂商的原生协议吗？**
不支持。本工具只覆盖 OpenAI 兼容的 Chat Completions 和 Responses 两套接口。

**为什么不做中转站排行榜？**
这是一个自查工具，不是评测平台。不同时间、地区、Key 和模型的结果不能横向排名，我们也不发布任何服务商的检测结果。

## English

**openai-compatible-checker** (`oacheck`) is a conformance checker for OpenAI-compatible APIs: API relays, self-hosted gateways and local inference servers. **It checks protocol compatibility only. It does not prove which model or provider serves an endpoint.**

Quick start (Python 3.10+, no dependencies):

```bash
read -rs OPENAI_API_KEY && export OPENAI_API_KEY
curl -fsSLO https://raw.githubusercontent.com/gptzzz/openai-compatible-checker/v2.0.0/openai_compatible_checker.py
python3 openai_compatible_checker.py --base-url https://api.example.com/v1 \
  --model your-model-id --profile full --format markdown
```

The PyPI package (`pipx run openai-compatible-checker`) is not published yet; until it is, use the single file above.

What it checks:

- `models`: `GET /models` returns a JSON list and, with `--model`, lists that ID.
- `chat`, `stream`: non-streaming and SSE chat completions; the stream must end with `data: [DONE]`.
- `stream_usage`: with `stream_options.include_usage`, a usage chunk arrives before `[DONE]`.
- `tools`: a `get_weather` call with `finish_reason: tool_calls`, an `id`, and JSON-string arguments; `--tools-roundtrip` sends the tool result back and expects a final answer.
- `json_mode`: `response_format: json_object` yields a parseable JSON object.
- `responses`: `POST /responses` completes with message text and usage; `--responses-stream` requires `response.output_item.done` and `response.completed`, the events Codex CLI relies on.
- `image`, `image_url`: an in-memory 8x8 PNG data URL, and optionally a remote URL.
- `error_shape`: a fixed fake key must get 401/403 with a JSON body; the shape (`openai_nested`, `flat`, `other`) is reported, not failed. A CDN/WAF block page (for example Cloudflare `error code: 1010`) fails as `blocked_by_cdn`, in this check and in all others, instead of being reported as a key problem.
- `latency`: `--runs` sequential streaming requests (default 5, max 50) with TTFT and total p50/p95 (nearest-rank) and error rate. Not a load test.

Profiles: `basic` (models, chat, stream; the default), `codex` (models, responses with streaming, stream), `agent` (tools with round trip, json_mode, stream_usage) and `full` (everything except `image_url`). Output is a redacted JSON report or Markdown (`--format markdown`); inside GitHub Actions a Markdown summary is added to the job summary automatically. Exit codes: 0 all passed, 1 a check failed, 2 usage or environment error.

Safety defaults: the key is read only from an environment variable (there is no `--api-key` flag), redirects are not followed, TLS is always verified, reports omit prompts and replies by default and are redacted again before output, and every read is size-limited.

GitHub Action:

```yaml
- uses: gptzzz/openai-compatible-checker@v2
  env:
    OPENAI_API_KEY: ${{ secrets.GATEWAY_API_KEY }}
  with:
    base-url: https://api.example.com/v1
    model: your-model-id
    profile: full
```

Each run sends a few billed requests. See [docs/](docs/) for per-check details (in Chinese, with English summaries), [CONTRIBUTING.md](CONTRIBUTING.md) to add a check, and [CHANGELOG.md](CHANGELOG.md) for the 1.x to 2.0 migration notes. The project was previously named `ai-api-relay-checker`.

Maintained by the GPTZZZ team, who run an OpenAI-compatible gateway. The checker treats every provider the same way, and all of its logic is in this repository. It is not affiliated with or endorsed by OpenAI; the name only says which API protocol it checks.

## 参与贡献

欢迎提交新的检查项、误报案例和文档修正，流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。测试不需要真实 Key，也不访问外网，只连仓库自带的本地 mock 网关：`python3 -m unittest discover -s tests -v`。版本变化见 [CHANGELOG.md](CHANGELOG.md)，安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。本项目使用 [MIT License](LICENSE)。

---

由 [GPTZZZ](https://gptzzz.ai/?utm_source=github&utm_medium=repo&utm_campaign=openai-compatible-checker&utm_content=footer) 团队维护。我们运营一个 OpenAI 兼容网关；本工具对任何服务商一视同仁，检测逻辑全部公开。本项目与 OpenAI 没有隶属或背书关系，名字里的 OpenAI 只用来说明它检查的是哪套接口协议。
