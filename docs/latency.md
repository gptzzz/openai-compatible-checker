# 延迟指标：TTFT、总耗时与 P95 的定义

> **English summary.** `ttft_ms` runs from the moment the request is sent to the first non-empty `delta.content`; `first_event_ms` stops at the first SSE data event of any kind; `total_ms` stops when `data: [DONE]` is parsed. Every request opens a new connection, so each timing includes DNS, TCP and TLS setup. The latency check sends `--runs` requests one after another (default 5, max 50) and reports nearest-rank p50/p95 over successful runs only; failures are counted separately in `error_rate`. With 5 runs, p95 is simply the slowest run. This is a baseline, not a load test and not an SLA.

## 三个时间点

| 字段 | 起点 | 终点 | 用途 |
|---|---|---|---|
| `first_event_ms` | 发出请求 | 收到第一个 SSE 数据事件 | 看网关多快开始回应。很多实现的第一个事件只带 `role`，不含文字 |
| `ttft_ms` | 发出请求 | 第一段非空 `delta.content` | 用户感受到的"首字延迟"（Time To First Token） |
| `total_ms`（单次检查里是 `latency_ms`） | 发出请求 | 解析到 `data: [DONE]` | 一次完整回答的耗时 |

起点在建立连接之前。工具每次请求都新建连接，不复用，所以每个数字里都包含 DNS 解析、TCP 和 TLS 握手。复用长连接的客户端，实际感受会比报告里略快，差多少取决于你到网关的网络距离。

TTFT 里还包括网关排队、转发到上游、上游生成第一段内容的时间。推理类模型会先思考再输出，TTFT 里包含思考时间，和普通模型直接比较没有意义。

## latency 检查怎么跑

- 对 `/chat/completions` 发 `--runs` 次流式请求（默认 5，上限 50），和 `stream` 检查的请求体相同。
- **串行**：上一次结束才发下一次，不并发，不自动重试。它不是压力测试，测不出并发上限。
- 每次都记录成功与否、`ttft_ms`、`total_ms`，失败时记录 `error.kind` 和状态码，全部写在 `samples` 里，方便你自己重新计算。

## 分位数怎么算

报告使用 **nearest-rank** 算法：把 n 个成功样本从小到大排序，第 p 百分位取第 ⌈p/100 × n⌉ 个。

| 样本数 n | p50 取第几个 | p95 取第几个 |
|---|---|---|
| 5 | 3 | 5（就是最慢的一次） |
| 10 | 5 | 10（仍是最慢的一次） |
| 20 | 10 | 19 |
| 50 | 25 | 48 |

几条规则：

- 分位数**只统计成功的请求**。失败的请求不进分位数，单独计入 `error_rate` 和 `errors`。只看 p95、不看错误率，会把"快但经常失败"误读成"快"。
- 某次请求成功但没有输出任何文字时，它有 `total_ms`，没有 `ttft_ms`，报告会加一条 note。
- 样本少时 p95 基本等于最大值，一次偶然的慢请求就能拉高它。想让 p95 稳定一些，至少用 `--runs 20`，并在不同时段重复。
- 有任何一次失败，`latency` 检查就判为失败，方便监控报警；统计数据照样输出。

## 怎样比较才公平

同一次比较里，以下条件要保持一致：

1. 同一个模型 ID，同一个提示词（默认的 `Reply with exactly OK.` 输出很短，适合测首字）；
2. 同一台机器、同一个网络出口，最好在同一时段内交替运行；
3. 相同的 `--runs` 和 `--timeout`；
4. 保留每次的 JSON 报告，比较时同时看 `error_rate`、样本数和 p50/p95，而不是只挑一个数字。

一次测试的结果只代表那个时间、那个网络、那个模型下的表现。把它说成"某服务商最快"或"最稳定"是不成立的。

## 延伸阅读

想做分时段、可复核的稳定性基线（成功率、流中断率、429 比例的记录口径和报告模板），可以参考维护方 GPTZZZ 撰写的 [API 中转站稳定性测试：成功率、TTFT、P95 与流式中断](https://gptzzz.ai/blog/api-relay-stability-benchmark/?utm_source=github&utm_medium=repo&utm_campaign=openai-compatible-checker&utm_content=latency_doc)。
