# 网页部署示例 | Web deployment examples

`nginx/iscan-web.conf` 是 DocuCraft 网页静态文件服务的 Nginx 示例。它只服务前端构建产物，不包含 TLS 证书配置，也不会自动启动 FastAPI 后端或 OCR 服务。完整服务至少需要网页、后端 API、自建 OCR 服务和持久化数据目录。

`nginx/iscan-web.conf` is an Nginx example for serving the DocuCraft static web build. It serves frontend files only; it does not configure TLS certificates or start the FastAPI backend or OCR service. A complete deployment also needs the backend API, your OCR service, and persistent data storage.

## 1. 构建网页 | Build the frontend

在仓库根目录执行：

```sh
cd frontend
npm ci
npm run build
```

网页文件生成在 `frontend/dist/`。请将其部署到 Nginx 配置中 `root` 指向的目录；默认示例路径为 `/opt/docucraft/frontend/dist`。生产构建时，如后端不在当前主机的 `5000` 端口，先设置正确的 `VITE_API_BASE_URL`，再运行构建。

Run the commands above from the repository root. The build is written to `frontend/dist/`; deploy it to the directory configured by Nginx `root` (the example uses `/opt/docucraft/frontend/dist`). If the backend is not reachable at the browser's default host and port `5000`, set `VITE_API_BASE_URL` before building.

## 2. 配置站点 | Configure the site

复制 `nginx/iscan-web.conf` 到当前 Nginx 安装的站点配置目录，并按实际环境调整：

- `root`：网页构建目录。
- `server_name`：站点域名。
- `listen`：站点监听端口。
- TLS：通过现有 HTTPS 入口、证书管理工具或上游负载均衡配置。
- API 路由：本示例只服务静态文件。请在站点配置或前置反向代理中，将 `/api/`、`/files/`、`/health` 转发到后端，并按实际路径保留 URI。

Copy `nginx/iscan-web.conf` into your Nginx site configuration directory and adjust `root`, `server_name`, and `listen` for your installation. Configure HTTPS through your existing TLS terminator or load balancer. This example serves static files only; add proxy routes for `/api/`, `/files/`, and `/health` in the site or an upstream proxy, preserving the request paths expected by the backend.

生产网页应使用 HTTPS；不要把后端调试端口直接暴露到公网。后端部署和 OCR 网络要求见[后端部署指南](../backend/README.md#docker-部署)。

Use HTTPS for the production site and do not expose an unprotected backend port to the public internet. See the [backend deployment guide](../backend/README.en.md#docker-deployment) for backend and OCR networking requirements.

## 3. 校验与重载 | Validate and reload

在 Nginx 服务器上先校验配置，再按系统服务管理方式重载：

```sh
sudo nginx -t
sudo systemctl reload nginx
```

Then check the site in a browser and verify that the frontend can reach `/health` and API routes. Nginx installation and service management differ by distribution; use the commands appropriate for your server.

## 可选 MIME 配置 | Optional MIME configuration

`nginx/iscan-mjs.conf` 是 PDF.js ES module worker 的 MIME 类型配置片段。如果 Nginx 已将 `.mjs` 识别为 JavaScript，则无需添加；否则将该片段放进站点的 `server` 配置块中。

`nginx/iscan-mjs.conf` sets a JavaScript MIME type for the PDF.js ES module worker. Include it inside the site's `server` block only if your Nginx installation does not already serve `.mjs` as JavaScript.

## 部署安全 | Deployment safety

- 将后端 `.env`、OCR/AI 密钥和对象存储凭据放在源码目录之外。
- 用户上传、生成文档和后端元数据应存放在持久化目录，且不位于 Nginx 公开网页目录中。
- 在更新前备份用户数据；更新后检查健康状态、OCR 连通性、文档导出和下载。
- 不要将缓存、日志、识别样例或用户文件加入发布包或 Git 提交。

- Keep backend `.env`, OCR/AI keys, and object-storage credentials outside the source directory.
- Store uploaded files, generated documents, and backend metadata in persistent directories outside the public web root.
- Back up user data before upgrades. After an upgrade, check health, OCR connectivity, document export, and downloads.
- Do not add caches, logs, OCR samples, or user files to release artifacts or Git commits.

