## What / 改了什么

<!-- One change per PR. 一个 PR 做一件事。 -->

## Why / 为什么

<!-- For check logic: link the protocol document or client source that justifies it. 改检测逻辑请附协议文档或客户端源码依据。 -->

## Checklist

- [ ] `python3 -m unittest discover -s tests -v` passes locally
- [ ] New or changed checks have one passing and at least three failing tests against `tests/mock_server.py`
- [ ] No third-party dependencies; still runs as a single file on Python 3.10+
- [ ] No keys, prompts or replies can reach reports, logs or the job summary
- [ ] `docs/checks.md`, `README.md` and `CHANGELOG.md` (`[Unreleased]`) updated where relevant
- [ ] The change is provider-neutral (no per-provider rules)
