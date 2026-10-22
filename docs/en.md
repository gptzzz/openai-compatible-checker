# English overview

**openai-compatible-checker** (`oacheck`) is a conformance checker for OpenAI-compatible APIs: API relays, self-hosted gateways and local inference servers. It is a single Python file that uses only the standard library, and it also runs as a GitHub Action.

!!! note "Protocol compatibility only"
    `oacheck` checks whether an endpoint follows the OpenAI-compatible protocol. It does not prove which model or provider serves the endpoint, and a single passing run is not an SLA.

The detailed pages of this site are written in Simplified Chinese; each one starts with a short English summary.

## Quick start

Python 3.10+, an OpenAI-compatible base URL and an API key with a small balance. The base URL is the API prefix, usually ending in `/v1`; the tool never appends `/v1` for you, because the path is part of what it checks.

```bash
read -rs OPENAI_API_KEY && export OPENAI_API_KEY
curl -fsSLO https://raw.githubusercontent.com/gptzzz/openai-compatible-checker/v2.0.0/openai_compatible_checker.py
python3 openai_compatible_checker.py --base-url https://api.example.com/v1 \
  --model your-model-id --profile full --format markdown
```

The PyPI package (`pipx run openai-compatible-checker`) is not published yet; until it is, use the single file above.

Exit codes: `0` all checks passed, `1` at least one check failed, `2` usage, environment or report-writing error.

## What it checks

- `models`: `GET /models` returns a JSON list and, with `--model`, lists that ID.
- `chat`, `stream`: non-streaming and SSE chat completions; the stream must end with `data: [DONE]`.
- `stream_usage`: with `stream_options.include_usage`, a usage chunk arrives before `[DONE]`.
- `tools`: a `get_weather` call with `finish_reason: tool_calls`, an `id`, and JSON-string arguments; `--tools-roundtrip` sends the tool result back and expects a final answer.
- `json_mode`: `response_format: json_object` yields a parseable JSON object.
- `responses`: `POST /responses` completes with message text and usage; `--responses-stream` requires `response.output_item.done` and `response.completed`, the events Codex CLI relies on.
- `image`, `image_url`: an in-memory 8x8 PNG data URL, and optionally a remote URL.
- `error_shape`: a fixed fake key must get 401/403 with a JSON body; the body shape (`openai_nested`, `flat`, `other`) is reported, not failed. A CDN/WAF block page fails as `blocked_by_cdn` instead of being reported as a key problem.
- `latency`: `--runs` sequential streaming requests (default 5, max 50) with TTFT and total p50/p95 (nearest-rank) and error rate. Not a load test.

## Profiles

| Profile | Checks | Generation requests |
|---|---|---|
| `basic` (default) | models, chat, stream | 2 |
| `codex` | models, responses with streaming, stream | 3 |
| `agent` | tools with round trip, json_mode, stream_usage | 4 |
| `full` | everything except `image_url` | 9 + `--runs` (14 by default) |

Use `--check` to run individual checks instead of a profile. The full option list is on the [command-line reference](cli.md) page.

## Safety defaults

- The API key is read only from an environment variable; there is no `--api-key` flag.
- HTTP redirects are not followed, so the `Authorization` header never goes to another host.
- TLS certificates are always verified.
- Reports omit prompts and replies by default and are redacted again before output.
- Every read is size-limited, and `latency` runs sequentially with a hard cap of 50 requests.

## GitHub Action

```yaml
- uses: gptzzz/openai-compatible-checker@v2
  env:
    OPENAI_API_KEY: ${{ secrets.GATEWAY_API_KEY }}
  with:
    base-url: https://api.example.com/v1
    model: your-model-id
    profile: full
```

Each run sends a few billed requests. Inputs, outputs and a daily monitoring example are on the [GitHub Action](github-action.md) page.

## Project

Source code, issues and releases: [github.com/gptzzz/openai-compatible-checker](https://github.com/gptzzz/openai-compatible-checker) (MIT License). The project was previously named `ai-api-relay-checker`.

Maintained by the operator of [GPTZZZ](https://gptzzz.ai/en/?utm_source=readthedocs&utm_medium=docs&utm_campaign=openai-compatible-checker&utm_content=en). GPTZZZ is an independent API relay run by an individual, with an OpenAI-compatible API. The checker treats every provider the same way, and all of its logic is public. It is not affiliated with or endorsed by OpenAI; the name only says which API protocol it checks.
