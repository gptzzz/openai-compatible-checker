# openai-compatible-checker（oacheck）

!!! note "只检查协议兼容性"
    `oacheck` 回答的是一个可验证的问题：这个接口是否按 OpenAI 兼容协议正确工作。它**不证明模型真伪或来源**，单次通过也不代表长期 SLA。

`oacheck` 用一条命令检查任意 OpenAI 兼容接口（API 中转站、自建网关、本地推理服务都可以）的模型列表、SSE 流式、工具调用、JSON 模式、Responses API（决定 Codex 能不能用）、图片输入、错误格式和首字延迟，输出脱敏的 JSON 或 Markdown 报告。它是一个只用 Python 标准库的单文件脚本，也能作为 GitHub Action 定时运行。

源码、Issue 和版本发布都在 GitHub：[gptzzz/openai-compatible-checker](https://github.com/gptzzz/openai-compatible-checker)，MIT License。

## 30 秒快速开始

需要 Python 3.10 或更高版本、一个 OpenAI 兼容的 Base URL，以及一个有少量额度的 API Key。Base URL 填 **API 前缀**，通常形如 `https://api.example.com/v1`。工具不会自动补 `/v1`，因为路径本身就是检查对象。

=== "macOS / Linux"

    ```bash
    # 1. 把 Key 放进环境变量。read -s 不回显，也不会进 shell 历史
    read -rs OPENAI_API_KEY && export OPENAI_API_KEY

    # 2. 下载单文件（只用 Python 标准库），跑全套检查，把结果打印成 Markdown 表格
    curl -fsSLO https://raw.githubusercontent.com/gptzzz/openai-compatible-checker/v2.0.0/openai_compatible_checker.py
    python3 openai_compatible_checker.py --base-url https://api.example.com/v1 \
      --model your-model-id --profile full --format markdown
    ```

=== "Windows PowerShell 7"

    ```powershell
    $env:OPENAI_API_KEY = Read-Host -MaskInput 'API key'
    python .\openai_compatible_checker.py --base-url https://api.example.com/v1 `
      --model your-model-id --profile full --format markdown
    ```

PyPI 包（`pipx run openai-compatible-checker`）还在准备上架，上架前请用上面的单文件方式，效果相同。

退出码：`0` 全部通过，`1` 至少一项检查失败，`2` 参数、环境变量或报告写入出错。脚本和 CI 可以直接按退出码分支。

## 检查什么

"能回一句话"不等于接口可用。接入失败往往出在具体某一层：流式断在 `[DONE]` 之前、用量块被网关吞掉、工具参数不是合法 JSON、只支持 Chat 不支持 Responses、错误体是一张 HTML 页。`oacheck` 把这些层拆开，每项单独记录，失败时给出稳定的 `error.kind`。

| 检查 | 一句话说明 |
|---|---|
| `models` | `GET /models` 返回 JSON 列表；指定 `--model` 时该 ID 必须在列表里 |
| `chat` | 非流式 Chat Completions 返回完整结构，记录 `finish_reason` 和 `usage` |
| `stream` | SSE 流式：每个 `data:` 都是合法 JSON，最后是 `data: [DONE]`；记录首字延迟 |
| `stream_usage` | 设置 `stream_options.include_usage` 后，`[DONE]` 之前有一个 usage 块 |
| `tools` | 工具调用带 `id`，`arguments` 是可解析为对象的 JSON 字符串；可选回传工具结果 |
| `json_mode` | `response_format: json_object` 的回复能直接解析成 JSON 对象 |
| `responses` | `POST /responses` 正常完成；可选流式，要求出现 `response.completed` |
| `image` / `image_url` | 接受 base64 data URL 图片；可选另测一张远程图片 |
| `error_shape` | 假 Key 得到 401/403 和 JSON 错误体；识别 CDN/WAF 拦截页 |
| `latency` | 串行流式请求若干次，输出 TTFT 和总耗时的 p50、p95 与错误率 |

每项检查的请求体、通过标准和失败类型见 [检查项详解](checks.md)。

## 按场景选 profile

| profile | 适合什么时候用 | 包含的检查 | 生成回复的请求数 |
|---|---|---|---|
| `basic`（默认） | 刚拿到 Key，先确认能用 | `models` `chat` `stream` | 2 |
| `codex` | 准备接 Codex CLI | `models` `responses` `stream`，默认开启 Responses 流式 | 3 |
| `agent` | 接 Agent 框架，需要工具调用和结构化输出 | `tools` `json_mode` `stream_usage`，默认开启工具回传 | 4 |
| `full` | 全面体检、定时监控 | 除 `image_url` 外的全部检查 | 9 + `--runs`（默认共 14） |

`/models` 和假 Key 请求一般不计费，以服务商规则为准；其余请求都会消耗少量额度。只想跑某几项时用 `--check`，例如 `--check tools --check json_mode`，它不能和 `--profile` 同时用。全部参数见 [命令行参考](cli.md)。

## 安装方式

| 方式 | 命令 | 说明 |
|---|---|---|
| 单文件 | 下载 `openai_compatible_checker.py` | 零依赖，复制到任何有 Python 3.10+ 的机器都能跑 |
| GitHub Action | `uses: gptzzz/openai-compatible-checker@v2` | 见 [GitHub Action](github-action.md) |
| pipx / pip（PyPI 上架后） | `pipx install openai-compatible-checker` | 之后直接用 `oacheck` 命令 |

## 安全默认值

- API Key 只能从环境变量读取，命令行里没有 `--api-key` 参数。
- 不跟随 HTTP 重定向，避免把 `Authorization` 头带到另一个地址。
- HTTPS 始终校验证书，没有跳过校验的开关。
- `error_shape` 用一个写死的假 Key 发请求，你的 Key 不会出现在那次请求里。
- 报告默认不含提示词和回复正文；Key、Bearer Token 和常见密钥形态在输出前统一再脱敏一次。
- `latency` 串行执行，上限 50 次，工具本身不能用来压测或放大流量。

## 文档目录

- [命令行参考](cli.md)：全部参数、退出码、报告结构和 `error.kind` 错误分类。
- [GitHub Action](github-action.md)：输入、输出，以及每天定时检测自己的网关。
- [检查项详解](checks.md)：每项检查发出的请求、通过标准、失败类型和出处。
- [Codex 兼容性](codex-compatibility.md)：为什么 Codex 需要 `/v1/responses`，以及怎样对照 Codex 的报错。
- [怎样解读结果](interpreting-results.md)：通过说明什么、不说明什么，怎样安全地分享报告。
- [延迟指标](latency.md)：TTFT、总耗时和 P95 的定义与公平比较方法。
- [English](en.md)：英文概览。

## 维护方

本工具由 [GPTZZZ](https://gptzzz.ai/?utm_source=readthedocs&utm_medium=docs&utm_campaign=openai-compatible-checker&utm_content=index) 的运营方维护。GPTZZZ 是个人运营的独立第三方 AI API 中转服务，提供 OpenAI 兼容接口。本工具对任何服务商一视同仁，检测逻辑全部公开。本项目与 OpenAI 没有隶属或背书关系，名字里的 OpenAI 只用来说明它检查的是哪套接口协议。

误报、漏报和文档问题请提交 [GitHub Issue](https://github.com/gptzzz/openai-compatible-checker/issues)；安全问题请按 [SECURITY.md](https://github.com/gptzzz/openai-compatible-checker/blob/main/SECURITY.md) 私下报告。
