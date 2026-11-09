# 贡献指南 | Contributing

感谢你愿意帮助改进 DocuCraft。欢迎报告问题、完善文档、修复错误和提交功能。参与前请阅读本指南、[行为准则](CODE_OF_CONDUCT.md)和[安全政策](SECURITY.md)。

Thank you for helping improve DocuCraft. Bug reports, documentation updates, fixes, and features are welcome. Before participating, read this guide, the [Code of Conduct](CODE_OF_CONDUCT.md), and the [security policy](SECURITY.md).

## 报告问题 | Report an issue

提交 issue 前，请先搜索已有问题并确认当前版本仍可复现。一个清晰的问题报告通常包括：

- 运行环境：操作系统、浏览器、Python/Node 版本，以及 DocuCraft 版本或 commit。
- 复现步骤：从打开页面到看到问题的最短步骤。
- 预期结果和实际结果；有帮助时可附错误代码、请求 ID 和脱敏日志。
- 涉及 OCR 时，说明 OCR 服务类型和响应字段；不要上传没有授权公开的扫描件。

Before opening an issue, search for an existing report and check that it still applies. A useful report includes the OS, browser, Python/Node and DocuCraft versions; short reproduction steps; expected and actual results; and sanitized errors or request IDs when relevant. For OCR problems, describe the service and response fields. Do not upload scans unless you have permission to share them publicly.

不要在公开 issue、PR 或截图中粘贴 `.env`、API 密钥、访问令牌、个人信息、客户数据、私人扫描件或未脱敏日志。安全漏洞请按[安全政策](SECURITY.md)私下报告，不要公开利用细节。

Never post `.env` contents, API keys, access tokens, personal or customer data, private scans, or unsanitized logs in public issues, pull requests, or screenshots. Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md); do not publish exploit details.

## 开始开发 | Start contributing

1. Fork 仓库并从 `main` 创建一个简短、清楚命名的分支。
2. 克隆你的 fork，在本地安装前端或后端依赖；不要将依赖目录加入 Git。
3. 阅读对应模块指南：[`frontend/README.md`](frontend/README.md) 或 [`backend/README.md`](backend/README.md)。
4. 让每个变更聚焦一个主题，避免夹带无关的重排或生成文件。
5. 完成后检查 `git diff`，再提交并创建 Pull Request。

1. Fork the repository and create a clearly named branch from `main`.
2. Clone your fork and install frontend or backend dependencies locally; do not add dependency folders to Git.
3. Read the relevant module guide: [`frontend/README.md`](frontend/README.md) or [`backend/README.en.md`](backend/README.en.md).
4. Keep each change focused. Avoid unrelated formatting and generated files.
5. Review `git diff`, commit your work, and open a pull request.

## 本地开发命令 | Development commands

### 后端 | Backend

```sh
cd backend
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
```

Windows 使用项目支持的 Python 3.13；如需隔离依赖，请先创建并激活 `.venv`。真实 OCR 服务并非运行单元测试的前置条件；测试不应调用付费 API 或上传私人文件。

Use the Python version supported by the project. Create and activate `.venv` when you want isolated dependencies. Unit tests should not require a real OCR endpoint, call paid APIs, or upload private files.

### 前端 | Frontend

```sh
cd frontend
npm ci
npm test
npm run build
```

依赖有变更时提交 `frontend/package-lock.json`。`node_modules/`、`frontend/dist/` 和本机环境配置都不应提交。

Commit `frontend/package-lock.json` when dependencies change. Do not commit `node_modules/`, `frontend/dist/`, or machine-specific environment files.

## 提交 Pull Request | Open a pull request

请使用仓库提供的 PR 模板，说明问题背景、用户可见变化、实现范围、执行过的检查和暂未覆盖的部分。前端交互变化可附截图，但截图中不得含个人数据或真实用户文档。API、环境变量、OCR 响应格式或部署流程变化，需要同步更新相关文档；面向用户的指南尽量同时提供中文和英文。

Use the repository pull request template. Describe the problem, user-visible change, scope, checks performed, and anything not covered. Screenshots can help with UI changes, but must not contain personal data or real user documents. Update documentation when changing APIs, environment variables, OCR response formats, or deployment steps; keep user-facing guides available in Chinese and English when practical.

提交前请检查：

- 没有 `.env`、密钥、个人资料、真实扫描件或数据库文件。
- 没有 `node_modules/`、虚拟环境、缓存、构建输出或临时报告。
- 代码改动有相关检查；文档链接和示例命令指向正确的文件与路径。
- 新依赖说明用途并加入正确的依赖清单；运行时依赖与测试依赖保持区分。
- 修改 API 契约时，前端、后端、兼容版本和相关测试保持一致。

Before submitting, verify that the diff contains no `.env`, keys, personal data, real scans, database files, dependency directories, virtual environments, caches, build output, or temporary reports. Run relevant checks, verify documentation links and commands, list dependencies in the correct manifest, and keep API, frontend, backend, compatibility version, and checks in sync.

## 设计边界 | Project boundaries

- OCR 生产调用由后端发往部署者配置的自建 HTTP 服务；不要在网页中加入 OCR 密钥或未经讨论的新云 OCR provider。
- AI 和对象存储凭据必须保留在服务端；错误信息和日志不得泄露密钥或完整用户内容。
- 测试样例应人工构造或明确获得授权；默认将测试图像与标注保存在仓库外。
- 尊重 MIT 许可、第三方依赖许可、字体许可和用户数据边界。

- Production OCR requests go from the backend to the operator-configured self-hosted HTTP service. Do not add OCR keys to the browser or introduce a cloud OCR provider without discussing the change.
- AI and object-storage credentials stay server-side. Errors and logs must not reveal secrets or full user content.
- Use synthetic or explicitly authorized test samples; keep test images and annotations outside the repository by default.
- Respect the MIT license, third-party dependency and font licenses, and user data boundaries.

