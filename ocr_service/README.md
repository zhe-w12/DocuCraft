# DocuCraft OCR service

这是与 DocuCraft 后端分开部署的纯 OCR HTTP 服务。服务使用 RapidOCR 的 PP-OCRv6 small 中文模型和 ONNX Runtime 在 CPU 上识别图片文字与位置；不会生成、润色或补写原文，也不会调用百炼、Remedi 或其他在线 AI 服务。

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

镜像使用 Python 3.11 slim。RapidOCR wheel 内包含 PP-OCRv6 small 的检测、方向分类和文字识别模型，首次构建会通过 pip 安装依赖；服务运行时在本机 CPU 上推理。启动预热完成后健康检查会返回 `engine: rapidocr` 和 `model: PP-OCRv6-small`。

## 配置 DocuCraft 后端

在 `/DocuCraft/DocuCraft/.env` 中设置以下值。`OCR_API_KEY` 必须和 `ocr_service/.env` 中的 `OCR_SERVICE_API_KEY` 完全相同：

```dotenv
OCR_PROVIDER=generic_http
OCR_ENDPOINT=http://127.0.0.1:5100/ocr
OCR_API_KEY=填入同一个随机密钥
```

保存后重启 DocuCraft 后端。OCR 服务只监听服务器本机回环地址，不需要在安全组或 Nginx 中开放 5100 端口。

## 接口

- `GET /health`：返回服务状态、OCR 引擎和模型。
- `POST /ocr`：需要 `Authorization: Bearer <OCR_API_KEY>`，通过 multipart 字段 `image` 上传图片。
- 成功返回识别段落、文字框坐标和置信度提示，兼容 DocuCraft 的 `generic_http` 适配器。

清晰、正向的印刷体图片通常识别效果较好。手写内容、模糊或倾斜照片、低分辨率图像和复杂表格仍可能识别不准；低置信度结果应对照原图校对。服务仅识别图像中实际出现的文字，不会用语言模型猜测或改写。

## 更新和排错

拉取新代码后，在服务目录执行：

```sh
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 docucraft-ocr
```

日志只记录服务启动和请求错误，不记录 API 密钥或上传图像内容。若容器未启动，检查模型初始化错误、容器架构和 OCR 密钥配置。
