# GitHub Action：在 CI 里定时检测 OpenAI 兼容接口

> **English summary.** The repository is a composite GitHub Action. This page lists its inputs and outputs, shows how to pass the API key from a secret, and explains the daily monitoring example and what each run costs.

仓库本身就是一个 composite Action，在 GitHub Actions 里跑的是同一个 `openai_compatible_checker.py`，报告格式和命令行完全相同。

## 最小用法

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

- Key 只从环境变量读，所以请用 `env:` 从 Secrets 传入，不要写成 Action 输入。GitHub 会在日志里遮住它，工具也不会把它写进报告、Job Summary 或 artifact。
- Runner 上需要 Python 3.10+。GitHub 托管的 runner 通常自带；找不到合适版本时 Action 会报错，提示你先加 `actions/setup-python`。
- 想要更强的供应链保证，可以把 `@v2` 换成完整的 commit SHA。

## 输入

| 输入 | 默认值 | 说明 |
|---|---|---|
| `base-url` | 必填 | 要检测的 API 前缀，例如 `https://api.example.com/v1` |
| `model` | 空 | 模型 ID。除了只查 `/models` 的情况，都需要填 |
| `profile` | `basic` | `basic`、`codex`、`agent` 或 `full`，含义见[首页](index.md) |
| `api-key-env` | `OPENAI_API_KEY` | 存放 Key 的环境变量名。在这一步用 `env:` 从 Secret 设置它，Key 永远不是输入 |
| `runs` | `"5"` | `latency` 检查的串行请求次数（1 到 50），只在 profile 包含 `latency` 时使用 |
| `artifact-name` | `openai-compatible-checker-report` | 上传的报告 artifact 名称，同一次 workflow 运行内必须唯一 |
| `fail-on-error` | `"true"` | 任一检查失败时让这一步失败（`"true"` 或 `"false"`） |

## 输出

| 输出 | 说明 |
|---|---|
| `status` | `ok`、`degraded` 或 `failed` |
| `passed` | 通过的检查数 |
| `failed` | 失败的检查数 |
| `report-path` | runner 上 JSON 报告的路径 |

结果会写进 Job Summary（Markdown 表格），完整 JSON 报告作为 artifact 上传。默认任何检查失败都会让这一步失败，方便收到通知；只想记录不想报红，就设 `fail-on-error: "false"`，再在后续步骤里按 `status` 自己判断。

## 每天定时检测自己的网关

仓库里有一个可以直接复制的示例：[examples/monitor.yml](https://github.com/gptzzz/openai-compatible-checker/blob/main/examples/monitor.yml)。用法：

1. 把它复制到你自己仓库的 `.github/workflows/api-gateway-monitor.yml`。
2. 在 Settings → Secrets and variables → Actions 里新建 Secret `GATEWAY_API_KEY`，建议用一把单独的、设了小额消费上限的 Key。
3. 在同一页的 Variables 里新建 `GATEWAY_BASE_URL`（例如 `https://api.example.com/v1`）和 `GATEWAY_MODEL`。

示例每天 01:17 UTC 运行一次（避开整点的排队高峰），也可以在 Actions 页手动触发。

几点注意：

- **每次运行都会产生真实的计费请求。** `full` 配 `runs: 5` 每次发 14 个短生成请求，按示例的每日频率约每月 420 个。预算紧就改用 `basic`（2 个请求）或降低频率。`GET /models` 和假 Key 请求通常不计费，以服务商规则为准。
- 公开仓库连续 60 天没有活动时，GitHub 会停用定时 workflow，需要到 Actions 页重新启用。
- 定时任务在高峰时段可能延迟启动，单次延迟不代表接口有问题。

报告该怎么看、怎样比较前后两次结果，见[怎样解读结果](interpreting-results.md)。
