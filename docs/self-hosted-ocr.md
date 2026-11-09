# 自建 OCR 服务接入

生产环境通过 DocuCraft 后端的 `generic_http` 适配器调用你自己部署的 OCR 服务。扫描图片不会发送到百度 OCR 等第三方服务；识别失败时也不会切换到后端机器上的 Tesseract。开发环境可以使用 `mock`。

## 配置后端

在服务器部署环境文件中配置自建服务地址和可选访问密钥：

```dotenv
OCR_ENDPOINT=http://127.0.0.1:9000/ocr
OCR_API_KEY=替换为你的服务密钥
```

生产 Compose 会固定使用 `OCR_PROVIDER=generic_http`，并在缺少 `OCR_ENDPOINT` 时拒绝启动。若 OCR 服务部署在其他机器，请将 `127.0.0.1` 改为内网域名或地址。跨机器传输时使用 HTTPS。

本地开发默认使用 `OCR_PROVIDER=mock`。如需在本地连真实服务，可在启动后端前设置：

```powershell
$env:OCR_PROVIDER = "generic_http"
$env:OCR_ENDPOINT = "https://ocr.example.com/recognize"
$env:OCR_API_KEY = "替换为你的服务密钥"
Set-Location backend
python run.py
```

## 后端发送什么 | HTTP request

后端向 `OCR_ENDPOINT` 发送 `POST multipart/form-data` 请求，图片字段名固定为 `image`。图片 MIME 类型会按文件格式设置为 PNG 或 JPEG。若配置 `OCR_API_KEY`，请求会携带 `Authorization: Bearer <密钥>`。当前 HTTP 超时时间为 90 秒；你的 OCR 服务应限制单次请求、图片尺寸和并发，避免资源被无限占用。

在后端所在主机上排查接口时，可用一张你有权测试的图片请求服务：

```sh
curl -X POST "https://ocr.example.com/recognize" \
  -H "Authorization: Bearer $OCR_API_KEY" \
  -F "image=@/path/to/scan.png"
```

不要把真实密钥写进公开文档、Shell 历史或截图。若 OCR 服务不需要认证，可以省略 `Authorization` 请求头。

## OCR 服务应返回什么 | HTTP response

服务应返回 HTTP 成功状态和 UTF-8 JSON 对象。最简形式是段落数组：

```json
{
  "paragraphs": ["识别出的第一段文字", "识别出的第二段文字"]
}
```

也可以返回单个 `text` 字符串。纯文本可导出为可编辑文字，但无法恢复原页面的位置或表格边界。为支持可定位编辑和表格恢复，建议提供坐标与表格信息：

```json
{
  "paragraphs": ["申请表"],
  "positioned_words": [
    {
      "text": "申请表",
      "location": {"left": 120, "top": 48, "width": 160, "height": 36},
      "confidence": 98.5
    }
  ],
  "tables": [[["姓名", "年龄"], ["张三", "18"]]],
  "table_layouts": [
    {"column_widths": [180, 90], "row_heights": [36, 34], "source_width": 600}
  ],
  "table_cells": [[
    {"row_start": 0, "row_end": 1, "col_start": 0, "col_end": 1,
     "text": "姓名", "location": {"left": 40, "top": 120, "width": 180, "height": 36}},
    {"row_start": 0, "row_end": 1, "col_start": 1, "col_end": 2,
     "text": "年龄", "location": {"left": 220, "top": 120, "width": 90, "height": 36}}
  ]],
  "low_text_confidence": false
}
```

### 字段说明

- `paragraphs`：按阅读顺序排列的段落字符串数组。可以改用单个 `text` 字符串。若同时返回 `paragraphs`，它优先作为完整段落文字。
- `paragraph_styles`：可选的段落样式数组，值支持 `title`、`subtitle`、`body`；缺省或非法值按普通正文处理。
- `positioned_words`：可选的文字块/词语数组，每项含 `text`（也接受 `words`、`content`、`value`）和位置。兼容字段名 `words_result`、`words`、`results`。
- 位置坐标从图片左上角开始，可使用像素值，或使用 0 到 1 的归一化值。推荐 `location: {left, top, width, height}`；也支持 `x/y/w/h`、`right/bottom`、`bbox` 或四个数字 `[left, top, width, height]`。坐标必须对应后端实际收到的图像方向和尺寸。
- `confidence` 或 `score` 可用 0 到 100 的百分数；`probability` 可用 0 到 1 的小数。`low_text_confidence: true` 会提示用户核对识别文字。置信度只是 OCR 服务的估计值，不能替代人工复核。
- `tables` 的嵌套顺序为“表格 → 行 → 单元格”。请保留空单元格，不能为了紧凑而移动后续列。
- `table_layouts` 与 `table_cells` 都按表格顺序排列。布局可提供 `column_widths`、`row_heights`、`source_width`、`source_left`、`source_right`。
- `table_cells` 每个单元格可提供 `row_start`、`row_end`、`col_start`、`col_end`、`text` 和 `location`。结束索引为不包含自身的边界；例如 `row_start: 0, row_end: 2` 表示占据第 0、1 行。合并单元格应同时正确填写行列起止索引。

只提供 `tables` 也能生成表格；同时返回 `table_cells` 和准确坐标能更好地保留边框、合并单元格、宽高和文字位置。文字块坐标有缺失或超出图像边界时，后端会忽略无效坐标，避免把文字放到错误位置。

表格和图片都有尺寸上限。服务端应返回与输入图片同一坐标系的数据，不要返回大于输入页面的虚构尺寸。对于旋转、透视校正或裁剪后的图像，位置必须按照后端发送的最终图片坐标计算。

## 错误处理

遇到可重试的连接或服务错误时，后端会向自建服务重试一次；重试后仍失败会将错误返回给网页。低置信度结果会保留并提示用户校对。服务不可用或没有返回文字时，不会切换到本机或第三方 OCR。

完整对比不同样例的步骤见 [OCR 效果基准](ocr-benchmark.md)。基准图片和人工标注请放在仓库外。
