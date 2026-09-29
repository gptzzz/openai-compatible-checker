# Security Policy

## Supported versions

| Version | Security fixes |
|---|---|
| 2.x | Yes |
| 1.x (`ai-api-relay-checker`) | Critical fixes only, until 2026-12-31 |
| 0.x | No |

请先在最新的 `2.x` 版本上复现问题再报告。1.x 用户可以直接升级：不传 `--profile` 和 `--check` 时行为与 1.x 相同，迁移说明见 [CHANGELOG.md](CHANGELOG.md)。

## Private reporting

请通过本仓库的 **Security → Report a vulnerability** 私密提交安全问题，不要在公开 Issue、Discussion、日志或截图中粘贴：

- API Key、Authorization 头或环境变量的值；
- 可用的内部或私有 API Base URL；
- 未脱敏的模型回复、提示词、请求正文或错误正文；
- 尚未协调披露、能直接复现漏洞的细节。

报告建议包含：

1. 受影响的版本和 Python 版本；
2. 操作系统和完整命令行，域名、路径和环境变量的值用占位符替换；
3. 最小复现步骤，以及预期和实际结果；
4. 已脱敏的 JSON 报告；
5. 对 Key 泄露、跨主机重定向、报告脱敏、Job Summary 内容或解析器资源消耗的影响说明。

如果仓库没有显示私密报告入口，请不要公开漏洞细节，等待维护者启用 GitHub Private Vulnerability Reporting。

We aim to acknowledge reports within 5 working days and to agree on a disclosure date with the reporter.

## Secret exposure response

如果真实 API Key 已经进入命令历史、终端录屏、报告、Issue、Actions 日志或提交历史：

1. 立即在服务端撤销或轮换 Key；
2. 删除公开副本，但不要把"删除文件"当成撤销凭据；
3. 检查该 Key 的用量、来源 IP 和异常调用；
4. 用新 Key 重新运行，确认报告中不含旧值；
5. 必要时清理 Git 历史和缓存副本。

本工具的脱敏是纵深防御，不能替代服务端撤销、最小权限、额度限制和密钥轮换。

## Security design

- API Key 只从用户指定的环境变量读取，命令行没有明文 Key 参数；误传的未知参数值在报错时被打码。
- Authorization 头不写入报告；`error_shape` 检查使用写死的假 Key，不发送用户的 Key。
- HTTP 重定向一律拒绝，避免把 Bearer 凭据转发到其他地址。
- TLS 使用系统和 Python 默认的信任链校验，没有跳过校验的开关。
- JSON 正文、错误正文、SSE 单行、总字节数和事件数都有上限；`latency` 最多 50 次串行请求。
- 默认报告不含提示词和回复正文；最终 JSON 和 Markdown 按真实 Key、Bearer 形态和常见密钥模式再次脱敏，`--image-url` 的完整地址也会被打码。
- 上游字符串先脱敏再截断；JSON 输出只用 ASCII 转义，兼容不同终端编码。
- Markdown 报告对表格单元格转义 `|`、反引号、尖括号、方括号等字符，上游错误消息不能在 Job Summary 里注入链接或 HTML。
- 报告文件通过同目录临时文件和原子替换写入。
- GitHub Action 通过环境变量接收输入，不在脚本里直接展开 `${{ }}`，避免脚本注入。

## Out of scope

- 上游 API 服务本身的漏洞或滥用问题；
- 由已撤销测试 Key 导致的 401；
- 用户主动使用 `--include-content` 后公开模型正文；
- 本机恶意管理员、调试器或同权限进程读取环境变量；
- 压力测试、流量放大或对第三方端点的扫描。
