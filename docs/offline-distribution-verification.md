# 离线验证安装包 / Offline distribution verification

这套测试只使用本仓库的 `127.0.0.1` mock 网关。通过表示安装包可运行，
并能按预期处理预设的正常和异常响应；**不表示任何真实服务、模型、账户或客户端集成已经通过测试**。
不需要 API 凭据，也不会发布包、调用真实服务或产生 API 费用。

## 为什么要单独测试 wheel

直接在源码目录运行测试，可能掩盖缺失模块、错误入口点或源码目录遮蔽安装包的问题。
`tools/smoke_distribution.py` 创建一个临时虚拟环境，只从指定本地 wheel 安装，
然后在源码目录之外运行。它验证导入确实来自该环境，以 `-I` 隔离模式运行模块，
并剔除调用者的 API Key、代理、`PYTHONPATH` 和 GitHub Summary 配置。
安装使用 `--no-index --no-deps`，验证阶段不下载任何软件。

## 执行

需要 Python 3.10+、`venv`、`ensurepip`。构建时还需要 PyPA `build` 和
`pyproject.toml` 中的构建后端；这些构建依赖应从正常软件源安装。

```sh
python -m unittest discover -s tests -v
python -m build
python tools/smoke_distribution.py --wheel dist/openai_compatible_checker-2.0.0-py3-none-any.whl
```

将 wheel 文件名替换成刚构建的实际版本。只验证你信任并审查过的构建：虚拟环境不是安全沙箱，安装包中的代码会实际执行。测试器不会下载 wheel、访问索引或自动发布。
如果机器缺少 `ensurepip`，由环境管理员通过其正常软件源提供 Python 的 venv 支持。
不应在错误情况下改用生产 API 来替代 mock 测试。

当前检查内容：

- wheel 的项目名与零运行时依赖约束
- 隔离安装后的真实模块路径
- 两个命令行入口的准确版本、检查列表及一次 basic profile 执行
- `basic`、`codex`、`agent`、`full` 四个 profile 的精确检查顺序、逐项成功状态、报告身份与默认选项
- 缺失 Responses、非法工具参数、截断 SSE 的失败分类与退出码
- 模拟 Key 被上游反射时的脱敏，以及默认报告不泄露响应正文
- 缺少 Key 时返回参数错误退出码 `2`

成功输出的 JSON 标有 `verification_scope: offline_mock_only` 和
`live_service_tested: false`，并记录被验证 wheel 的 SHA-256。异常行为也必须测试通过，
不能把“每个输入都返回成功”当作合格。

## 重建公开报告样例

```sh
python tools/smoke_distribution.py \
  --wheel dist/openai_compatible_checker-2.0.0-py3-none-any.whl \
  --sample-json examples/report-sample.json
```

只有全部离线检查通过后才写样例。生成的报告额外带有显眼的 `sample_provenance` 字段，
说明来源是本地 mock；检查器自身的报告结构未改变。时间、端口和耗时来自实际运行，
每次会不同，不能用于评价公网服务性能。提交更新前检查 diff，保持离线标记。

## 适用边界

CI 在构建后运行此验证，release workflow 在上传构建产物前运行同一验证。
测试不会改版本、推 tag、配置发布凭据或授予权限。
本地生成的 wheel 和 sdist 是构建产物，不意味着 PyPI、Docker Hub 或 Snap Store 已发布。
Docker/Snap 的容器构建与安装仍需要分别验证。

## English

The verifier installs an explicitly supplied wheel into a new temporary venv with
`pip --no-index --no-deps`, then tests both entry points and all four profiles
against the bundled loopback mock. It verifies the installed module path and tests
three negative protocol fixtures, secret redaction, and the missing-key exit code.
No real API credentials or external endpoints are accepted by the verifier.
An optional sample report is clearly labeled `offline_mock_only`. These checks
verify a distribution and its mock behavior, never a live service's compatibility.
