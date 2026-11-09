# 网页前端 | Frontend

本目录是 DocuCraft 的浏览器端应用，使用 Vue 3 和 Vite 构建。前端负责页面交互、浏览器端图片准备、PDF 编辑和调用后端 API。OCR 引擎、AI 服务密钥和文档转换逻辑由后端管理。

This directory contains the DocuCraft browser app, built with Vue 3 and Vite. The frontend handles user interaction, browser-side image preparation, PDF editing, and backend API calls. The backend owns OCR providers, AI service keys, and document conversion.

## 环境要求 | Requirements

- Node.js 22 and npm
- A running DocuCraft backend for OCR, AI, and server-side document export

## 本地开发 | Local development

从仓库根目录运行：

```sh
cd frontend
npm ci
npm run dev
```

Run these commands from the repository root:

```sh
cd frontend
npm ci
npm run dev
```

Vite 会输出开发网页地址。默认 API 地址是当前主机的 `5000` 端口。若后端地址不同，在启动开发服务器或执行构建前设置 `VITE_API_BASE_URL`：

```sh
VITE_API_BASE_URL=https://api.example.com npm run build
```

PowerShell 可使用 `$env:VITE_API_BASE_URL = "https://api.example.com"`。此地址会打包进网页，因此只能填写公开 API 地址；不得放置 OCR、AI 或对象存储密钥。

Vite prints the local web URL. By default, the API address uses port `5000` on the current host. If your backend uses another URL, set `VITE_API_BASE_URL` before starting Vite or building:

```sh
VITE_API_BASE_URL=https://api.example.com npm run build
```

In PowerShell, use `$env:VITE_API_BASE_URL = "https://api.example.com"`. This value is bundled into public browser code. It must contain only the public API address, never OCR, AI, or object-storage credentials.

## 测试与构建 | Test and build

```sh
npm test
npm run build
```

构建产物写入 `frontend/dist/`。该目录可由 Nginx 或其他静态文件服务提供；它由构建生成，不应提交到 Git。依赖更新后请提交 `package-lock.json`。

The build output is written to `frontend/dist/`. Serve it with Nginx or another static file server. It is generated and should not be committed. Commit `package-lock.json` when dependencies change.

## 代码组织 | Source layout

| 路径 | 内容 |
| --- | --- |
| `src/main.js` / `src/App.vue` | Vue 入口、应用路由和主要页面状态。 |
| `src/AssistantWorkspace.vue` | AI 对话与文档工作区。 |
| `src/DocumentWorkspace.vue` | 图片识别后的文字、表格和文档编辑区。 |
| `src/PdfEditor.vue` | 浏览器 PDF 预览、擦除、签名和导出界面。 |
| `src/image-preparation.js` | 上传前图像检查和 OCR 输入准备。 |
| `src/local-documents.js` / `src/assistant-storage.js` | 浏览器本地文件历史和 AI 对话存储。 |
| `src/` 下其他 `.js` 文件 | 可单独测试的状态、布局、格式化、下载和编辑辅助模块。 |
| `test/` | 使用 Node.js 测试运行器的前端测试。 |
| `public/` | 图标、离线 Service Worker 和 Web App Manifest。 |

## 数据与安全 | Data and security

- Browser-local history stays in that browser profile unless the app explicitly sends content to the backend.
- OCR images are uploaded to the configured backend; the backend sends them to the operator's `OCR_ENDPOINT`.
- AI prompts and conversation context are sent by the backend to the configured model provider when AI features are used.
- Frontend code is public to the browser. Never put secrets in Vue files, Vite variables, or static assets.
- The Service Worker caches static application files, not API responses or private user documents.

- 浏览器本地历史保存在当前浏览器配置文件中；只有应用明确请求时，内容才会发送到后端。
- OCR 图片上传到配置的后端，再由后端发送至部署者设置的 `OCR_ENDPOINT`。
- 使用 AI 功能时，对话提示词由后端发送到配置的模型服务。
- 前端代码会公开给浏览器；不要把密钥放进 Vue 文件、Vite 环境变量或静态资源。
- Service Worker 缓存网页静态资源，不缓存 API 响应或用户私有文档。

See the [architecture and data-flow guide](../docs/architecture.md) for the complete flow and deployment boundary.

