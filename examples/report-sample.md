## OpenAI-compatible API check: 10/10 passed (ok)

| | |
|---|---|
| Endpoint | https://api.example.com/v1 |
| Model | your-model-id |
| Profile | full |
| Generated | 2026-09-28T22:06:10Z |
| Tool | openai-compatible-checker 2.0.0, report schema 2.0 |

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

> Checks protocol compatibility only. A passing report does not prove which model or provider serves the endpoint, and a single run is not an SLA.
