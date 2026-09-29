# 参与贡献 / Contributing

感谢你愿意改进这个工具。English follows the Chinese text.

## 项目原则

提交前请先确认改动符合这几条，不符合的 PR 会被关闭：

1. **中立。** 检测逻辑对所有服务商一视同仁。不接受针对某个服务商的特殊判定、白名单或"放宽"。
2. **只验协议。** 检查必须能用公开的协议文档或主流客户端的源码说清楚"为什么这样算通过"。不做模型真伪判定、红黑榜、排名或"掺假率"一类的统计。
3. **单文件、零依赖。** `openai_compatible_checker.py` 只用 Python 3.10+ 标准库，下载一个文件就能跑。
4. **安全默认值不退让。** Key 只从环境变量读；不跟随重定向；不关闭 TLS 校验；报告默认不含提示词和回复正文；所有读取都有上限。
5. **不做压测。** 串行、低频、有上限。

## 开发环境

不需要安装任何依赖：

```bash
git clone https://github.com/gptzzz/openai-compatible-checker.git
cd openai-compatible-checker
python3 -m unittest discover -s tests -v
```

测试只连本机的 mock 网关（`tests/mock_server.py`），不访问外网，也不需要真实 Key。想手动试 mock：

```bash
python3 tests/mock_server.py --port 8787 &
export OPENAI_API_KEY=sk-local-test-secret-1234567890   # mock 的测试值，不是真实 Key
python3 openai_compatible_checker.py --base-url http://127.0.0.1:8787/ok/v1 --model gpt-test --profile full --format markdown
```

把 URL 里的 `ok` 换成 mock 支持的其他模式（例如 `responses-404`、`tool-args-not-json`、`flat-error`），就能看到各种失败的样子。模式列表在 `tests/mock_server.py` 里。

## 新增一项检查

1. 在 `openai_compatible_checker.py` 里写 `probe_<name>(ctx, url)`：成功返回结果字典，失败抛 `ProbeFailure(kind, message, **details)`。`kind` 用小写下划线，一旦发布就不要改名。
2. 在 `CHECKS`、`ALL_CHECKS`、`CHECK_DESCRIPTIONS` 里登记；需要的话加进某个 profile。
3. 在 `tests/mock_server.py` 加 mock 模式，在 `tests/` 里至少写**一个通过用例和三个失败用例**，并确认报告里不出现测试 Key。
4. 在 `docs/checks.md` 写清请求体、通过标准、失败类型，并附上带查阅日期的出处链接。
5. 在 `README.md` 的检查表和错误分类表里补上，在 `CHANGELOG.md` 的 `[Unreleased]` 下记一笔。

报告字段只增不改：改名或删除字段属于破坏性变更，需要升 `schema_version` 主版本并写迁移说明。

## 提交误报 / 漏报

觉得某项检查判错了，请用 Issue 模板"检测结果有误"，附上：

- 脱敏后的报告（默认报告即可，不要附 `--include-content` 的）；
- 你认为正确的行为，以及依据（协议文档链接或客户端源码位置）。

请不要在 Issue 里贴 Key、完整请求头或未脱敏的回复。

## 提交规范

- 一个 PR 做一件事，附上测试。
- 提交信息用英文祈使句，例如 `Add json_schema strict check`。
- 文档以简体中文为主，关键段落附英文。

---

## Contributing (English)

**Principles.** Checks must be provider-neutral (no per-provider exceptions), must be justified by a public protocol document or a mainstream client's source code, and must not produce authenticity verdicts, rankings or "fake model" statistics. The checker stays a single file using only the Python 3.10+ standard library. Safety defaults are not negotiable: keys only from environment variables, no redirects, TLS always verified, no prompts or replies in reports by default, bounded reads. No load testing.

**Setup.** No dependencies. Run `python3 -m unittest discover -s tests -v`. Tests only talk to the local mock gateway in `tests/mock_server.py`; the first path segment selects a behavior, for example `http://127.0.0.1:8787/responses-404/v1`.

**Adding a check.** Write `probe_<name>(ctx, url)` that returns a dict or raises `ProbeFailure(kind, message, **details)`; register it in `CHECKS`, `ALL_CHECKS` and `CHECK_DESCRIPTIONS`; add mock modes and at least one passing and three failing tests; document the request body, pass criteria, failure kinds and dated sources in `docs/checks.md`; update the README tables and the `[Unreleased]` section of `CHANGELOG.md`. Report fields are append-only; renaming or removing one is a breaking change that needs a major `schema_version` bump and migration notes.

**Reporting a wrong result.** Use the "Wrong check result" issue template with a redacted report and the reference that supports the behavior you expect. Never post keys, full request headers or unredacted replies.
