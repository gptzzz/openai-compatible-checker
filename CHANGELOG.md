# Changelog

本项目的重要变更记录在此文件中。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

All notable changes are listed here. The migration notes for 1.x users are in both Chinese and English.

## [Unreleased]

### Changed

- 暂无。

## [2.0.0] - 2026-09-29

项目从 `ai-api-relay-checker` 改名为 `openai-compatible-checker`，命令名为 `oacheck`。旧仓库地址自动跳转。

### Added

- 新检查 `stream_usage`：设置 `stream_options.include_usage`，要求 `[DONE]` 之前有 usage 块，且 token 字段是非负整数。
- 新检查 `tools`：用 `get_weather` 函数验证 `finish_reason: tool_calls`、调用 `id` 和 JSON 字符串参数；`--tools-roundtrip` 把工具结果发回去，检查最终回答。
- 新检查 `json_mode`：`response_format: json_object` 的回复必须能解析成 JSON 对象；被代码块包住时给出提示。
- 新检查 `responses`：`POST /responses` 要求 `status: completed`、message 文本和整数 `usage`；`--responses-stream` 按 Codex 的解析规则检查 `response.output_item.done` 和 `response.completed`。
- 新检查 `image` 和 `image_url`：代码现场生成 8×8 PNG 的 base64 data URL；`--image-url` 另测远程图片，报告只保留主机名。
- 新检查 `error_shape`：用写死的假 Key 请求 `/models`，要求 401/403 且响应体是 JSON，报告 `openai_nested`、`flat` 或 `other` 形状。
- 新错误类别 `blocked_by_cdn`：错误响应是 CDN/WAF 拦截页（Cloudflare 的 `error code: 1xxx` 纯文本、`"cloudflare_error": true` 的 JSON、`cf-mitigated` 响应头）时使用，不再记成 `authentication_error`；`error_shape` 遇到这类 403 判为失败，不再算通过。
- 新检查 `latency`：`--runs` 次串行流式请求（默认 5，上限 50），输出 TTFT 和总耗时的 nearest-rank p50/p95、错误率和逐次样本。
- `stream` 检查新增 `ttft_ms`（第一段非空内容），原有的 `first_event_ms` 保留。
- `--profile basic|codex|agent|full`，以及 `--list-checks`。
- `--format markdown`；在 GitHub Actions 里自动把 Markdown 摘要追加到 `$GITHUB_STEP_SUMMARY`（`--no-step-summary` 关闭）。
- 报告顶层新增 `scope` 字段，写明"只验协议兼容，不证明模型来源"；检查结果可带 `notes`（不影响通过与否的观察）和 `stage`（分阶段检查在哪一步失败）。
- `pyproject.toml`：发布到 PyPI，包名 `openai-compatible-checker`，命令 `oacheck` 和 `openai-compatible-checker`，支持 `pipx run`。
- `action.yml` composite Action：输入 `base-url`、`model`、`profile`、`api-key-env`、`runs`，上传 JSON 报告 artifact 并写 Job Summary。
- `examples/monitor.yml` 每日定时检测示例；`docs/` 下四篇文档：检查详解、结果解读、Codex 兼容性、延迟指标。
- 测试扩展到 70 多个用例：每个新检查至少覆盖通过和 3 种失败；CI 覆盖 Python 3.10–3.13 与 Ubuntu、macOS、Windows，并在 CI 里对本地 mock 网关实际运行 Action。

### Changed

- 主文件改名为 `openai_compatible_checker.py`。
- 报告 `schema_version` 从 `1.0` 升到 `2.0`，`tool.name` 改为 `openai-compatible-checker`。
- HTTP 400 和 422 的 `error.kind` 从 `http_error` 改为 `bad_request`。
- 默认 `--timeout` 从 15 秒改为 60 秒，上限从 120 秒改为 600 秒，给推理类模型留出时间。
- Base URL 校验同时拒绝指向 `/responses` 和 `/completions` 的地址。
- 最终脱敏同时覆盖 JSON 的键名。

### Deprecated

- `ai_api_relay_checker.py` 改为转发文件，运行时在标准错误提示改名，3.0.0 删除。

### Unchanged

- 退出码：0 全部通过，1 有检查失败，2 参数、环境变量或写报告出错。
- 不传 `--profile` 和 `--check` 时只跑 `models`、`chat`、`stream`，与 1.x 相同。
- 全部安全默认值：Key 只从环境变量读取；不跟随重定向；报告二次脱敏；默认只存回复的长度和 SHA-256。

### 迁移说明 / Migrating from 1.x

| 项 / Item | 1.x | 2.0 |
|---|---|---|
| 文件 / File | `ai_api_relay_checker.py` | `openai_compatible_checker.py`（旧文件仍可用 / old name still forwards） |
| 命令 / Command | `python3 ai_api_relay_checker.py` | `oacheck`，或 / or `python3 openai_compatible_checker.py` |
| `schema_version` | `"1.0"` | `"2.0"` |
| `tool.name` | `"ai-api-relay-checker"` | `"openai-compatible-checker"` |
| HTTP 400/422 `error.kind` | `http_error` | `bad_request` |
| 默认超时 / default `--timeout` | 15 s | 60 s |
| `configuration` | — | 新增 / adds `profile`, `options` |
| 报告顶层 / top level | — | 新增 / adds `scope` |
| 检查结果 / check results | — | 可能带 / may include `notes`, `stage`, `ttft_ms` |

解析报告的脚本需要：接受 `schema_version` 为 `"2.0"`；把 `bad_request` 当作原来的 400 类 `http_error` 处理；忽略不认识的字段。想保持 1.x 的超时行为，显式传 `--timeout 15`。

Scripts that parse reports should accept `schema_version` `"2.0"`, treat `bad_request` as the former 400-class `http_error`, and ignore unknown fields. Pass `--timeout 15` to keep the 1.x timeout.

## [1.0.0] - 2026-08-25

### Added

- 纯 Python 标准库的单文件 CLI；
- OpenAI-Compatible `GET /models` 探针与目标模型存在性检查；
- 非流式 `POST /chat/completions` JSON/Schema 检查；
- SSE 流式 Content-Type、UTF-8、事件 JSON、增量和 `[DONE]` 完整性检查；
- 严格拒绝 NaN、Infinity 与孤立 surrogate，并识别声明长度后的响应截断；
- 401/403/404/429/5xx、DNS、TLS、超时、JSON 与 SSE 错误分类；
- 仅从环境变量读取 API Key，并拒绝跨地址 HTTP 重定向；
- 默认不输出 Prompt 或回复正文的脱敏 JSON 报告；
- 上游错误、请求 ID 与响应元数据采用"先脱敏、后截断"；
- 显式 `--include-content`、重复 `--check` 和原子 `--output` 写入；
- 本地 mock server 回归测试。

[Unreleased]: https://github.com/gptzzz/openai-compatible-checker/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/gptzzz/openai-compatible-checker/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/gptzzz/openai-compatible-checker/tree/v1.0.0
