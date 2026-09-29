# 怎样解读检测结果

> **English summary.** A pass means this endpoint, with this key and model, answered a set of minimal requests in an OpenAI-compatible way at one moment. It is not an SLA, it says nothing about output quality, and neither `/models` nor the model's own answers can prove which model or provider is behind the endpoint. That is why this tool gives no authenticity verdicts, rankings or "fake model rate" figures. This page explains what each result does and does not tell you, how to track results over time, and how to share a report safely.

## 通过说明什么

一次全绿的报告只说明：**在这个时间点，用这个 Key、这个模型，这组最小请求按 OpenAI 兼容协议得到了正确形状的响应。**

它不说明：

- **长期可用性。** 单次通过不等于 SLA。网关可能在高峰期限流、在凌晨维护，上游也可能临时切换。要看稳定性，就得在不同时段重复跑，见下文"跟踪变化"。
- **输出质量。** 检查只看结构，不评判回答好坏。`tools` 通过说明工具调用的格式对，不说明模型挑的参数总是对。
- **模型身份和来源。** 见下一节。
- **全部功能。** 没测到的能力（长上下文、`json_schema` 严格模式、Responses 下的函数调用、音频、文件输入等）不能从通过的报告里推断出来。

## 为什么 /models 证明不了模型身份

- `/models` 返回什么 ID，完全由网关决定。列表里有某个 ID，只说明网关愿意接受这个名字。
- 响应里的 `model` 字段、模型对"你是谁"的回答、回答风格，都可以被中间层改写或模仿，也会因为系统提示词、版本更新而变化。
- 用这些信号给服务商打"真/假"标签，误判的代价很高，还容易被针对性地"刷过"。

所以本工具只回答能被协议验证的问题，不输出真伪判定、红黑榜或"掺假率"一类的数字。关心来源的话，应该向服务商要书面说明（上游是谁、是否有转售或降级），并结合账单和用量记录核对。

## 每类结果的含义

| 你看到的结果 | 说明什么 | 不说明什么 |
|---|---|---|
| `models` 通过 | Key 有效，路径正确，模型 ID 在列表里 | 这个 ID 背后是哪个模型 |
| `chat`、`stream` 通过 | 基本的问答和流式传输正常，流能完整结束 | 高峰期或长回答时也不会断 |
| `stream_usage` 失败 | 客户端拿不到流式请求的用量，计费、限额类组件会失灵 | 服务商实际怎么计费 |
| `tools` 通过，round trip 失败 | 模型会发起调用，但网关不能正确转发 `role: tool` 消息 | 换一个模型会不会好（值得试） |
| `json_mode` 失败，内容被代码块包住 | 网关很可能丢掉了 `response_format` | 模型本身不能输出 JSON |
| `responses` 失败 | Codex 这类只走 Responses API 的客户端用不了 | Chat 接口有问题 |
| `image` 通过但 `color_named` 为 false | 请求被接受了，但图片可能没到视觉模型（比如被静默丢弃） | 一定被丢弃了。模型也可能只是没按要求回答 |
| `error_shape` 为 `flat` | 错误体不是 OpenAI 的嵌套形状，部分客户端只能显示 HTTP 状态码 | 接口不能用 |
| `latency` 的 p95 很高 | 这几次请求里最慢的那部分很慢 | 服务一直这么慢；样本少时 p95 近似最大值 |
| `summary.status` 为 `degraded` | 部分检查失败，看具体哪项 | 整体不可用 |

## 跟踪变化

- **定时跑。** 用仓库里的 GitHub Action 按天运行（示例见 [examples/monitor.yml](../examples/monitor.yml)），失败时会让 Job 变红，你会收到通知。报告以 artifact 形式保存，可以回看。
- **比较报告。** 看 `summary`、每项的 `ok` 和 `error.kind` 是否变化；`content_sha256` 可以判断同一提示词的回复是否改变，但不存正文；`usage` 的数量级突然变化，值得追问服务商。
- **记下条件。** 报告里有时间、Base URL、模型和工具版本。比较时，网络位置和时段也要一致。

## 向服务商反馈时带什么

- 报告里失败项的 `error.kind`、`error.message`、`status_code`；
- `request_id`（如果网关返回了 `X-Request-ID` 等头）和 `generated_at`（UTC 时间）；
- 使用的模型 ID 和 profile。

这些信息足够对方在日志里定位，不需要提供 Key。

## 安全地分享报告

- 默认报告不含 Key、提示词和回复正文，Bearer Token 和常见密钥形态也会在输出前再脱敏一次。
- 加了 `--include-content` 的报告含回复正文，可能带出提示词相关信息，不要公开。
- 分享前仍建议自己看一遍，尤其是 `error.message`：它来自服务商的错误消息，工具只能打码已知形态的密钥。
- 如果 Key 已经出现在截图、日志或 Issue 里，先去服务端撤销或轮换，再删除公开内容。流程见 [SECURITY.md](../SECURITY.md)。
