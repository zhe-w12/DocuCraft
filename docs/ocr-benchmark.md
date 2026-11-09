# OCR 效果基准

`backend/scripts/benchmark_ocr.py` 可用同一批本地图片和人工校对文本比较 OCR 引擎。脚本在本机读取样例，不上传图片；默认报告不保存识别出的原文。请把测试图片、标注和报告放在仓库目录之外，避免提交用户资料。

## 准备标注

创建 UTF-8 JSONL 文件，每行对应一张图片。路径可以相对于 JSONL 文件所在目录，也可以使用绝对路径：

```jsonl
{"id":"clear-text-01","image":"images/clear-text-01.png","expected_text":"此处填写人工校对的正文"}
{"id":"table-01","image":"images/table-01.png","expected_tables":[[["姓名","年龄"],["张三","18"]]]}
```

- `expected_text` 用于计算字符错误率（CER）。脚本比较识别段落；如果引擎没有返回段落，则比较表格文字或定位文字。统计前会做 Unicode NFC 规范化并忽略空白，标点仍参与比较。
- `expected_tables` 用于计算表格单元格准确率。嵌套结构为“表格 → 行 → 单元格”；单元格按行列位置逐一精确比较，缺失或多出的单元格计为错误。
- 一条记录可以同时包含两个字段。请人工复核标注，避免把 OCR 输出直接当作标准答案。

## 运行

在后端虚拟环境中，从 `backend/` 目录执行。比较自建服务前，先按[自建 OCR 接入说明](self-hosted-ocr.md)配置 `OCR_ENDPOINT` 和可选密钥：

```sh
python scripts/benchmark_ocr.py \
  --provider generic_http \
  --manifest /path/to/ocr-corpus/manifest.jsonl \
  --output /path/to/ocr-corpus/self-hosted-results.json
```

Windows PowerShell 示例：

```powershell
python scripts/benchmark_ocr.py `
  --provider generic_http `
  --manifest C:\ocr-corpus\manifest.jsonl `
  --output C:\ocr-corpus\self-hosted-results.json
```

`--provider` 支持 `mock` 和 `generic_http`。使用 `generic_http` 时，需在当前进程环境配置 `OCR_ENDPOINT` 和可选的 `OCR_API_KEY`。报告包含每张图片的 CER、表格单元格准确率和耗时，以及整体微平均 CER、准确率和平均耗时。`--limit 10` 可先抽取少量样例检查标注和配置。

默认报告不包含识别原文。只有确认报告存储位置合适、且样例不含敏感信息时，才使用 `--include-recognized-text`。

## 如何解读

CER 越低越好；表格单元格准确率越高越好。分别准备清晰印刷件、低对比度或倾斜图片、复杂表格等样例组，并分别比较结果。总体平均值可能掩盖某一类图片上的退化。每次比较应使用相同样例、相同图像版本和相同语言包设置；识别结果仍需人工核对，不能把基准分数当作实际扫描的准确率保证。
