# 后端指南

简体中文 | [English](README.en.md)

DocuCraft 后端基于 FastAPI，负责浏览器 API、OCR 服务调用、AI 请求、文档生成、文件保存和下载。AI agent 使用 LangChain 1.x；长期记忆保存在 SQLite 数据库，联网搜索通过 `langchain-tavily` 调用 Tavily。后端不会在生产环境自行选择第三方 OCR 服务：生产 Compose 使用 `generic_http`，由 `OCR_ENDPOINT` 指向部署者自己的 OCR 服务。

## 环境要求

- Python 3.13
- 仓库根目录中的 `.env`（可从 [`backend/.env.example`](.env.example) 复制）
- DOCX 转 PDF 时需要系统可用的 LibreOffice `soffice`
- 真实扫描识别时需要后端能访问的自建 OCR HTTP 服务

## 本地开发

在仓库根目录创建开发环境文件。默认的 mock OCR 便于启动与界面开发，不代表任何真实引擎的识别质量。

```powershell
Copy-Item backend/.env.example .env
Set-Location backend
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

Linux/macOS：

```sh
cp backend/.env.example .env
cd backend
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

默认 API 地址是 `http://127.0.0.1:5000`。健康检查为 `GET /health`。前端启动方式见[网页前端指南](../frontend/README.md)。

## 配置说明

后端从仓库根目录读取 `.env`。不要把 `.env` 或真实凭据复制到前端、Docker 镜像或 Git 提交中。常见变量如下，完整默认值见 [`.env.example`](.env.example)：

| 变量 | 说明 |
| --- | --- |
| `HOST` / `PORT` | API 监听地址和端口；本地通常为 `127.0.0.1:5000`，容器内部按 Compose 配置监听。 |
| `OCR_PROVIDER` | `mock` 用于本地开发；`generic_http` 调用自建 OCR HTTP 服务。 |
| `OCR_ENDPOINT` | 自建 OCR 服务的完整 HTTP(S) 识别 URL。生产部署必填。 |
| `OCR_API_KEY` | 可选 Bearer 密钥；仅存在于后端环境。 |
| `STORAGE_BACKEND` | `local` 使用后端本地目录；`aliyun_oss` 使用兼容配置的对象存储。 |
| `STORAGE_DIR` | 本地存储目录；生产部署应映射到持久化目录。 |
| `AI_PROVIDER` / `AI_API_BASE_URL` / `AI_API_KEY` / `AI_MODEL` | 可选 AI 写作模型设置。模型密钥仅放在后端环境。 |
| `AI_PLANNER_MODEL` / `AI_WRITER_MODEL` | 可选规划 agent 与写作 agent 模型名；留空时使用 `AI_MODEL`，共用同一服务地址和 API 密钥。 |
| `TAVILY_API_KEY` | 可选 LangChain 联网搜索密钥；仅放在后端环境，配置后可在助手输入框开启搜索。 |
| `MAX_UPLOAD_MB` / `MAX_SOURCE_IMAGE_MB` | API 图片上传和原始图像大小限制。 |
| `OCR_MAX_CONCURRENT` / `OCR_REQUESTS_PER_MINUTE` | OCR 并发量和速率限制。 |

前端构建变量 `VITE_API_BASE_URL` 不属于后端配置。更多 OCR 接口定义见[自建 OCR 接入指南](../docs/self-hosted-ocr.md)。

## AI 联网搜索

AI 请求由 LangChain 1.x agent 执行，`TAVILY_API_KEY` 配置后，用户开启联网搜索时 agent 才会获得 Tavily 搜索工具。依赖版本约束见 [`requirements.txt`](requirements.txt)。

在仓库根目录 `.env` 配置 `TAVILY_API_KEY`，并重启后端。Docker Compose 部署时重新构建后端镜像：

```sh
docker compose -f backend/docker-compose.yml up --build
```

随后在助手输入框勾选“联网搜索”。未勾选时助手不会调用 Tavily；勾选后，LangChain agent 可按需搜索，每次请求最多搜索 3 次、每次最多返回 5 条结果。回答会附带编号和来源链接。搜索词会发送给 Tavily，密钥只保存在后端环境。配置的聊天模型需要支持 Function Calling，以使用长期记忆和搜索工具。

## 短期与长期记忆

短期记忆是当前对话的消息列表。前端只把当前选中的对话发给后端，不会把其他对话合并进来；对话历史仍保存在当前浏览器。

长期记忆只保留适合跨对话使用的用户偏好和项目事实。LangChain agent 会在需要时调用记忆工具；记忆按当前浏览器档案保存到 `STORAGE_DIR/assistant_memory.sqlite3`，默认位于 `backend/storage/assistant_memory.sqlite3`。检索使用轻量关键词匹配，不需要 Embedding 服务；Docker Compose 的 `backend/storage` volume 会持久化数据库。助手页面的“长期记忆”按钮可以查看或删除记忆。

系统不应保存密码、API 密钥、财务或健康等敏感内容。更换浏览器档案会使用另一套记忆空间；本项目当前不提供登录账号或跨设备记忆同步。

## OCR 调用流程

1. 前端将扫描图片发送到 `POST /api/v1/documents/convert`。
2. 后端检查文件类型、大小和图像限制，再使用当前 `OCR_PROVIDER`。
3. `generic_http` 以 multipart 图片请求调用 `OCR_ENDPOINT`；可选地携带 Bearer 认证。
4. 后端解析段落、定位词语和表格结果，生成可编辑文档并保存文件及元数据。
5. 前端读取返回的文档信息，供用户校对、编辑和导出。

生产环境的 OCR 服务不可用时不会自动切换到本地 OCR 或第三方 OCR。后端会按规则重试可恢复的错误；最终失败会返回给网页并提示用户。OCR 数据类型、通用 HTTP 适配器、provider 工厂和共享识别处理均位于 `app/services/ocr.py`。provider 工厂只允许 `mock`（开发）和 `generic_http`（自建 OCR 服务）。

## 主要 API

| 方法与路径 | 用途 |
| --- | --- |
| `GET /health` | 健康状态、源码指纹和上传限制。 |
| `GET /api/v1/assistant/status` | 查看 AI 助手是否已配置。 |
| `POST /api/v1/assistant/chat` | 发送聊天或流式生成请求。 |
| `POST /api/v1/assistant/export/docx` | 导出 AI 文档为 Word。 |
| `POST /api/v1/assistant/export/pdf` | 导出 AI 文档为 PDF。 |
| `POST /api/v1/documents/convert` | 上传扫描图片并创建识别文档。 |
| `POST /api/v1/documents/export-edited` | 导出用户编辑后的识别文档。 |

请求字段和响应模型以 `backend/app/models.py` 与接口实现为准。修改 API 契约时，需要同时更新前端调用、兼容版本和相关测试。

## Docker 部署

### 本地 Compose

从仓库根目录创建 `.env` 后运行：

```sh
docker compose -f backend/docker-compose.yml up --build
```

### Linux 生产 Compose

生产示例使用 host network 和独立持久化目录。将部署目录及环境文件准备好，并在 `.env` 中设置 `OCR_ENDPOINT`：

```sh
export ISCAN_ROOT=/opt/docucraft
mkdir -p "$ISCAN_ROOT/backend/storage" "$ISCAN_ROOT/backend/storage_metadata"
# 将 backend/.env.example 复制为 "$ISCAN_ROOT/.env"，并按需填写配置。
docker compose --env-file "$ISCAN_ROOT/.env" \
  -f backend/docker-compose.production.yml up -d --build
```

生产配置把 `OCR_PROVIDER` 固定为 `generic_http`。如 OCR 服务运行在同一台主机上，确认地址和网络模式匹配；如果运行在另一台机器上，使用后端可访问的内网 DNS/IP 或 HTTPS 地址。容器内的 `127.0.0.1` 不等于其他容器或远程服务器。

首次上线前至少完成以下步骤：

1. 使用专门的运行账号和最小必要的文件权限。
2. 将 `.env` 放在源码仓库之外，确认其中 OCR、AI、对象存储密钥不进入 Git。
3. 为用户文件配置持久化存储和备份策略。
4. 使用 HTTPS 反向代理对外提供网页与 API，不直接公开调试端口。
5. 确认防火墙、上传大小、并发和速率限制适合实际负载。
6. 启动后检查 `/health`，并分别验证网页、OCR、文档导出和文件下载。

CI 会构建匹配版本的网页和后端部署包。生产环境应使用同一次 CI 运行的 release artifact，并按照包内 `DEPLOY.txt` 操作，避免网页与 API 版本不一致。Nginx 网页服务参考[部署示例](../deploy/README.md)。

## 测试与开发检查

```sh
cd backend
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
```

OCR 基准脚本见 `scripts/benchmark_ocr.py`。使用真实扫描样例时，请选择获得授权且可公开使用的图像；样例、标注、识别结果和报告应放在仓库之外。基准步骤见[OCR 效果评估](../docs/ocr-benchmark.md)。

## 常见问题

- **`OCR_ENDPOINT` 未设置**：生产 Compose 会拒绝启动；在部署环境 `.env` 中填入可访问的完整 URL。
- **OCR 返回连接错误**：从后端运行环境测试 DNS、端口、TLS 证书和访问控制；不要仅从浏览器测试，因为 OCR 请求由后端发出。
- **OCR 返回 401/403**：检查服务端是否要求 `Authorization: Bearer ...`，以及 `OCR_API_KEY` 是否匹配。
- **OCR 有文字但布局不准**：让 OCR 服务返回 `positioned_words` 和表格/单元格坐标；纯文本结果无法提供原始页面几何信息。
- **PDF 导出失败**：确认 `soffice` 已安装且后端进程有权限运行，并查看后端日志中的请求 ID。
- **重启后文件消失**：确认 Compose volume 指向主机持久化目录，而非容器临时文件系统。

