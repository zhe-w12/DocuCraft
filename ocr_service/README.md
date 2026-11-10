# DocuCraft OCR 服务

这是与 DocuCraft 后端分开部署的 Tesseract OCR HTTP 服务。它只从图片识别文字和位置，不生成解题步骤或答案，也不会调用 Remedi、百炼或其他在线 AI 服务。服务在 Docker 中运行，宿主机只把它绑定到 `127.0.0.1:5100`。

## 服务器部署

服务器需要 Docker Compose。代码部署到 `/DocuCraft/DocuCraft/ocr_service` 后，在服务器运行：

```sh
cd /DocuCraft/DocuCraft/ocr_service
cp .env.example .env
openssl rand -hex 32
nano .env
```

把随机命令生成的值填入 `.env` 的 `OCR_SERVICE_API_KEY`，保存后启动服务：

```sh
docker compose up -d --build
docker compose ps
curl -i http://127.0.0.1:5100/health
```

服务首次构建时会安装 Tesseract、简体中文和英文识别数据。镜像使用 Python 3.11 slim 与 Debian 系统包，可在服务器的 ARM64 架构上构建。

## 配置 DocuCraft 后端

在 `/DocuCraft/DocuCraft/.env` 中设置以下值。`OCR_API_KEY` 必须和 `ocr_service/.env` 中的 `OCR_SERVICE_API_KEY` 完全相同：

```dotenv
OCR_PROVIDER=generic_http
OCR_ENDPOINT=http://127.0.0.1:5100/ocr
OCR_API_KEY=填入同一个随机密钥
```

保存后重启 DocuCraft 后端。OCR 服务只监听服务器本机回环地址，不需要在安全组或 Nginx 中开放 5100 端口。

## 接口

- `GET /health`：返回服务状态、OCR 引擎和启用的语言。
- `POST /ocr`：需要 `Authorization: Bearer <OCR_API_KEY>`，通过 multipart 字段 `image` 上传图片。
- 成功返回识别段落、文字框坐标及低置信度提示，兼容 DocuCraft 的 `generic_http` 适配器。

简体中文与英文是默认识别语言。可以在 `.env` 中把 `OCR_LANG` 改为 Tesseract 已安装的数据组合，例如 `eng`。扩大图片大小、像素数、超时或并发量前，应结合服务器内存和 CPU 实测。表格边框与单元格结构不由此服务识别；拍照歪斜、手写文字或复杂版面需要人工校对。

## 更新和排错

拉取新代码后，在服务目录执行：

```sh
docker compose up -d --build
docker compose logs --tail=100 docucraft-ocr
```

日志只记录服务启动和请求错误，不记录 API 密钥或上传图像内容。若容器未启动，先查看日志中的 Tesseract 语言包或密钥配置错误。
