# 安全政策 | Security policy

## 报告安全问题 | Reporting a vulnerability

请不要通过公开 GitHub issue、讨论区或 pull request 报告尚未修复的漏洞。请使用 GitHub 仓库的 **Report a vulnerability / 私下报告漏洞** 功能提交报告。如果该功能不可用，请通过仓库维护者的 GitHub 个人资料私下联系维护者，并避免公开漏洞细节。

Do not report an unpatched vulnerability in a public GitHub issue, discussion, or pull request. Use the repository's **Report a vulnerability** feature to submit a private report. If that feature is unavailable, contact the maintainers privately through their GitHub profile and avoid sharing exploit details publicly.

报告中请包含受影响版本、复现步骤、影响范围，以及可行的修复建议。请勿附带真实用户数据或凭据。

Include the affected version, reproduction steps, impact, and any suggested fix. Do not attach real user data or credentials.

## 支持范围 | Supported versions

安全修复优先针对 `main` 分支的当前版本。请升级到最新版本后再确认问题是否仍然存在。

Security fixes target the current version on the `main` branch. Please upgrade to the latest version before checking whether an issue still applies.

## 部署者建议 | Deployment guidance

- 将 `.env`、模型/OCR 密钥、对象存储凭据和 SSH 私钥保存在后端环境或密钥管理服务中。
- 使用 HTTPS 保护网页与 API 流量；不要把后端调试端口直接暴露到公网。
- 限制文件上传、并发和访问速率，并为用户文件配置访问控制与备份。
- 日志中不要记录 API 密钥、访问令牌或用户上传内容。

- Keep `.env`, model/OCR keys, object-storage credentials, and SSH keys in the backend environment or a secrets manager.
- Use HTTPS for web/API traffic; do not expose backend debug ports directly to the public internet.
- Limit upload size, concurrency, and request rates; protect and back up user files.
- Do not log API keys, access tokens, or uploaded user content.
