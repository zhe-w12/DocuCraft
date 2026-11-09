# DocuCraft 项目级功能、字段与接口设计

> **用途**：作为需求评审、开发拆分、接口联调和数据设计依据。内容基于本仓库代码及项目文档。
>
> **状态标记**：【已实现】表示代码中存在相应实现，不代表已经通过生产验收；【设计建议】表示扩展方案，当前代码未必实现；【部署配置】表示由运维人员在后端环境设置。
>
> 当前 DocuCraft 没有账号登录。本文中的“用户”指当前浏览器配置；浏览器档案 ID 不是身份凭据。项目的核心能力是 AI 写作、扫描图片 OCR、识别结果校对、文档导出、浏览器内 PDF 编辑和历史管理。

---

## 一、项目角色、范围与业务边界

### 1.1 使用者

#### 1.1.1 普通使用者【已实现】

- 通过浏览器使用 AI 助手、图片识别、文档编辑与导出、PDF 工具和历史记录。
- AI 对话、助手文稿和 PDF 副本主要保存在浏览器；扫描图片和其导出文件保存在后端文件存储。
- 无账号、团队、角色权限和跨设备同步；清除浏览器站点数据可能导致本地对话和 PDF 副本丢失。

### 1.2 部署运维人员

#### 1.2.1 后端配置责任【已实现】

- 配置模型、OCR、搜索和文件存储服务。
- 设置上传大小、并发、速率、持久化目录和公网访问策略。
- API 密钥仅放在后端环境；不得写入前端构建变量、公开文档或 Git。

### 1.3 外部服务

#### 1.3.1 AI、OCR 与对象存储【已实现】

- AI：浏览器将当前会话必要消息发送到 DocuCraft 后端，后端再调用配置的模型服务；用户勾选联网搜索时才可调用搜索工具。
- OCR：浏览器上传图片到后端，后端将图片转发至配置的 OCR HTTP 服务。
- 对象存储：后端可使用本地目录或阿里云 OSS；签名 URL 的有效期由部署配置控制。
- PDF：导入、渲染、编辑和生成均在浏览器完成，PDF 原件不上传到后端。

### 1.4 主业务流程

#### 1.4.1 从输入到交付【已实现】

1. 选择 AI、图片识别或 PDF 工具。
2. 图片识别：校验/准备图片，调用 OCR，读取段落、表格及坐标，生成文档产物。
3. 用户校对文字和表格；保存编辑会生成新的扫描文档版本，不覆盖原件。
4. AI：选择工作流并提交当前会话；用户可编辑生成文稿，再导出 Markdown、DOCX 或 PDF。
5. PDF：浏览器内标注和另存副本；保留原 PDF。
6. 用户通过当前浏览器的历史列表重新打开、分享或删除记录。

---

## 二、前端三级功能模块与字段

### 2.1 工作台与服务连接

#### 2.1.1 工具导航【已实现】

- **active_tool**：字符串，页面状态；当前工具，如图片识别或 PDF。
- **view**：字符串，页面状态；当前主页面，如助手或历史。
- **mobile_pane**：枚举，页面状态；移动端当前显示助手面板或文档面板。
- **panels_swapped**：布尔，页面状态；桌面布局是否交换两侧面板。
- **操作**：切换 AI 助手、扫描文档、PDF、历史；移动端切换面板。
- **校验**：有未保存修改或任务执行中时，按界面提示完成、下载或确认关闭。

#### 2.1.2 后端地址设置【已实现】

- **api_base_url**：字符串，必填；后端 API 根地址，默认来自构建配置或当前主机的 5000 端口。
- **api_draft**：字符串；弹窗内临时编辑值，保存前不影响当前连接。
- **connection_status**：枚举，只读；checking、online、outdated、offline。
- **connection_message**：字符串，只读；连接或版本检查结果。
- **max_upload_bytes**：整数，只读；健康接口返回的上传字节上限。
- **max_source_image_bytes**：整数，只读；健康接口返回的原图字节上限。
- **校验**：完整 http/https URL；不允许用户名、密码、查询串和片段；保存时清理末尾斜杠。
- **保存**：写入当前浏览器 LocalStorage；不可用时只在当前会话有效。
- **切换语义**：新导入使用新服务；历史扫描文档继续使用记录中的原服务地址。

#### 2.1.3 服务健康检查【已实现】

- **status**：字符串，只读；健康状态，成功为 ok。
- **source_fingerprint**：字符串，只读；后端代码指纹。
- **api_compatibility_version**：整数，只读；API 兼容版本。
- **max_upload_bytes**：整数，只读；最大上传量。
- **max_source_image_bytes**：整数，只读；最大源图量。
- **调用时机**：启动、切换 API、图片上传前。
- **失败处理**：标记不可用或版本过旧；版本不兼容时阻止上传。

### 2.2 图片导入与 OCR 识别

#### 2.2.1 图片选择与批量导入【已实现】

- **file**：浏览器 File，必填；从本地文件选择器或拖放取得。
- **file.name**：字符串，只读；源文件名，用作标题候选。
- **file.type**：MIME 字符串，只读；必须是 image 类型。
- **file.size**：整数，只读；字节数，必须大于 0。
- **prepared_image**：二进制，只读；根据上传上限缩放或转换后的图片。
- **prepared_image.name**：字符串，只读；上传到后端的文件名。
- **import_index**：正整数，只读；当前批次序号，从 1 开始。
- **import_total**：正整数，只读；批次图片数。
- **upload_progress**：整数，只读；可计算时为 0–100，否则只展示处理中。
- **import_status**：字符串，只读；批次成功数量、失败原因及图像转换提示。
- **failed_files**：文件数组，只读；失败文件列表，可供用户重新提交。
- **校验**：拒绝非图片和空文件；尺寸必须符合健康接口上限。逐张处理，单张失败不能标成成功。
- **范围**：当前 OCR 上传接口只处理单张图片；PDF 多页识别不在此接口范围内。

#### 2.2.2 OCR 识别任务【已实现】

- **image**：文件二进制，必填；multipart/form-data 字段，后端检查 MIME 为 image/*。
- **document_id**：字符串，只读；后端成功建档后生成。
- **title**：字符串，只读；从安全化的源文件名生成。
- **processing_stage**：枚举，只读；准备、上传、OCR 分析、文档生成。
- **retry_count**：整数，只读；重试次数；仅对可恢复的服务错误有限重试。
- **warnings**：字符串数组，只读；低置信度、原图布局或导出能力提示。
- **error_detail**：字符串，只读；失败阶段及可理解的错误说明。
- **处理规则**：后端验证图像、处理方向、校验坐标并调用已配置 OCR；成功时生成 Word，尽可能生成 PDF 与结构化版本。
- **失败规则**：OCR 不可用或无可编辑文字时返回错误，不得留下伪成功记录；失败转换需要清理部分生成文件。

#### 2.2.3 OCR 文档概览【已实现】

- **id**：字符串，只读；文档 ID。
- **title**：字符串；文档标题，可在编辑区展示。
- **status**：字符串，只读；响应默认 completed。
- **has_tables**：布尔，只读；是否存在表格。
- **warnings**：字符串数组，只读；识别/导出警告。
- **image_width**、**image_height**：整数，只读；源图像像素尺寸。
- **source_image_url**：字符串或空，只读；源图预览地址。
- **editor_html**：字符串或空，只读；后端生成的网页编辑器内容。
- **展示规则**：需把警告与识别结果同时展示；OCR 输出不是人工校对后的定稿。

#### 2.2.4 段落、样式与坐标【已实现】

- **paragraphs**：字符串数组；按阅读顺序排列的段落，可编辑。
- **paragraph_styles**：字符串数组；与段落对应的样式，支持 title、subtitle、body；其他值按 body 处理。
- **paragraph_layouts**：对象数组；段落的源图几何布局，可缺省。
- **paragraph_layouts.fragments**：对象数组；每项含 text、left、top、width、height。
- **paragraph_layouts.source_width**：数字；布局坐标对应的源图宽度。
- **paragraph_layouts.left/top/right/bottom**：数字；段落边界坐标。
- **positioned_words**：对象数组；OCR 文字块或表单字段。
- **positioned_words.source_index**：整数或空；原始词块索引，范围 0–1,000,000。
- **positioned_words.text**：字符串；识别或编辑内容，最长 10,000 字符。
- **positioned_words.left/top/width/height**：有限数字，非负；源图左上角坐标及文字框宽高。
- **positioned_words.is_form_field**：布尔；是否为表单字段。
- **positioned_words.rendered_width**：有限数字或空，非负；编辑器实际绘制宽度。
- **low_confidence_examples**：字符串数组，只读；需人工重点核对的文本样例。
- **坐标规则**：源图左上角为原点；无效或越界坐标须忽略/提示，不能静默错位。

#### 2.2.5 表格识别与校对【已实现】

- **tables**：三维字符串数组；顺序为表格、行、单元格；空单元格必须保留。
- **table_layouts**：对象数组；每张表的几何布局。
- **column_widths**：数字数组；列宽。
- **row_heights**：数字数组；行高。
- **source_width**：数字；OCR 的源图宽度。
- **source_left/source_right**：数字；表格左右边界。
- **table_cells**：按表格分组的单元格对象。
- **row_start/row_end**：整数；占用行的起始与结束边界，结束值不包含在范围内。
- **col_start/col_end**：整数；占用列的起始与结束边界，结束值不包含在范围内。
- **text**：字符串；单元格识别/编辑文字。
- **left/top/right/bottom**：数字；单元格在源图中的边界。
- **校验**：合并单元格完整描述行列范围；表格网格与几何数组顺序一致。缺少源图时不能承诺原版式导出。

#### 2.2.6 编辑并保存扫描文档【已实现】

- **document_id**：字符串，编辑请求可选；关联原识别记录。
- **title**：字符串，可选，最多 200 字符；空值按扫描文档处理。
- **paragraphs**：字符串数组；编辑后的段落。
- **tables**：三维字符串数组；编辑后的网格。
- **positioned_words**：对象数组；编辑后的文字和坐标。
- **editor_dirty**：布尔，只读；当前内容与加载基线是否不同。
- **saving**：布尔，只读；读取编辑器/服务端生成文件中。
- **保存规则**：调用导出编辑接口并生成新的 document_id 和新产物；旧文件不原位覆盖。
- **失败规则**：若文件生成成功但历史写入失败，提醒用户立即下载；服务端保存错误不清除当前编辑稿。

### 2.3 AI 助手与写作

#### 2.3.1 工作流选择【已实现】

- **mode**：枚举，必填；auto、plan、write_from_plan、write、revise、polish、shorten、expand、rewrite、summarize、chat。
- **draft**：字符串；当前对话未发送的输入。
- **web_search**：布尔，必填；本轮是否允许联网搜索，默认关闭。
- **memory_profile_id**：字符串；前端生成的档案 ID，20–64 位字母、数字、下划线或连字符。
- **工作流规则**：plan 只讨论并完善规划；write_from_plan 在用户确认规划后生成正文；auto 判断写作或修改意图。
- **联网规则**：仅勾选且服务端已配置密钥时调用；搜索词会离开 DocuCraft 后端发送给配置的搜索服务。

#### 2.3.2 会话和消息管理【已实现】

- **conversation.id**：字符串，必填；浏览器生成的对话 ID。
- **conversation.title**：字符串；显示标题，新对话默认为“新对话”。
- **conversation.mode**：枚举；当前工作流。
- **conversation.draft**：字符串；未发送草稿。
- **conversation.createdAt/updatedAt**：整数毫秒时间戳；创建及最近活动时间。
- **conversation.messages**：消息数组。
- **conversation.document.title/content**：字符串；当前对话关联文档标题和正文。
- **activeId**：字符串或空；当前会话 ID。
- **message.id**：字符串，必填；消息 ID。
- **message.role**：枚举，必填；user 或 assistant。
- **message.content**：字符串，必填；每条最多 100,000 字符。
- **message.createdAt**：整数毫秒时间戳。
- **message.workflow**：枚举或空；助手消息对应工作流。
- **message.documentOutput**：布尔；是否是文档型回复。
- **message.status**：枚举或空；流式生成时为 streaming，结束后更新。
- **上下文校验**：一轮最多 80 条；至少一条 user 消息；所有消息总长度不超过 120,000 字符。
- **持久化**：优先 IndexedDB，按实现回退 LocalStorage；生成文档正文保存在会话 document 中。

#### 2.3.3 发送与流式响应【已实现】

- **mode**：枚举，必填；本轮工作流。
- **messages**：对象数组，必填；仅 role、content。
- **stream**：布尔；网页当前置 true。
- **web_search**：布尔；联网开关。
- **memory_profile_id**：字符串，可选；长期记忆隔离档案。
- **发送状态**：写入 user 消息、保存草稿、锁定当前会话，记录助手流式状态。
- **NDJSON delta**：事件含 type=delta 和 content 文本增量。
- **NDJSON done**：type=done，表示生成结束。
- **NDJSON error**：type=error 和 detail 错误信息。
- **失败处理**：保留已接收内容并提示中断；不能覆盖用户正在编辑的文稿。

#### 2.3.4 助手服务状态【已实现】

- **configured**：布尔，只读；模型配置是否完整。
- **provider**：字符串，只读；服务商展示名。
- **model**：字符串，只读；默认模型名。
- **web_search_configured**：布尔，只读；搜索密钥是否存在。
- **密钥**：不向前端返回。
- **未配置**：禁用生成并提示部署侧配置 AI_API_KEY 与 AI_MODEL。

#### 2.3.5 文档编辑与目录【已实现】

- **documentTitle**：字符串；关联文稿标题。
- **documentContent**：字符串；Markdown 正文。
- **documentEditorHtml**：字符串；富文本编辑临时状态。
- **documentEditorDirty**：布尔；编辑器是否有未保存变化。
- **editorMode**：枚举；阅读或编辑。
- **outline**：对象数组，只读；从 Markdown 标题提取的目录，含标题、层级、行号和锚点。
- **activeOutlineId**：字符串；当前目录位置。
- **安全规则**：Markdown 渲染不允许不可信原始 HTML 或远程图片；编辑内容需同步回纯文本/Markdown 存储。

#### 2.3.6 复制与导出【已实现】

- **title**：字符串，可选，最多 200 字符；空值使用“未命名文档”。
- **content**：字符串，必填，1–100,000 字符；导出正文。
- **file_format**：路径枚举；docx 或 pdf。
- **markdown_filename**：字符串；浏览器下载文件名。
- **Markdown 导出**：浏览器侧生成。
- **DOCX/PDF 导出**：后端生成二进制文件；PDF 需要服务器转换工具。
- **复制操作**：支持整篇或单条消息；剪贴板失败时提供手动复制对话框。

### 2.4 长期记忆管理

#### 2.4.1 查看、删除单条或清空【已实现】

- **X-DocuCraft-Memory-Profile**：请求头，必填；20–64 位允许字符。
- **memory.id**：字符串，只读；记忆 ID。
- **memory.content**：字符串，只读；记忆正文，最多 800 字符。
- **memory.category**：枚举，只读；semantic、episodic、procedural；未知值归一为 semantic。
- **memory.created_at**：UTC ISO 8601 字符串，只读。
- **操作**：查看列表、删除单条、清空当前档案。
- **限制**：profile ID 是隔离键，不是授权；当前 memory API 无账号认证。禁止存密码、密钥和不必要的敏感信息。

### 2.5 历史记录与文档管理

#### 2.5.1 历史记录对象【已实现】

- **id**：字符串，必填；扫描文档 ID 或 PDF 本地记录 ID。
- **type**：枚举，必填；image 或 pdf。
- **title**：字符串；历史显示标题。
- **createdAt**：整数毫秒时间戳；导入或生成时刻。
- **api**：字符串或空；扫描文档对应的 API 地址。
- **hasTables**：布尔；扫描文档是否含表格。
- **thumb**：字符串或空；扫描图缩略图地址。
- **docxUrl/structuredDocxUrl**：字符串或空；原图版和结构化 Word 地址。
- **pdfUrl/structuredPdfUrl**：字符串或空；原图版和结构化 PDF 地址。
- **sourceImageUrl**：字符串或空；源图地址。
- **name**：字符串；PDF 原文件名。
- **size**：整数；PDF 文件字节数。
- **storage**：枚举或空；当前持久保存时为 indexeddb。
- **sessionOnly**：布尔；本次会话可用但未能写入浏览器存储。
- **保存位置**：历史索引在 LocalStorage；PDF Blob 在 IndexedDB；扫描文件在后端。
- **列表交互**：按时间排序，标题/日期搜索，all/image/pdf 筛选，分页加载更多。

#### 2.5.2 打开、删除、分享和清空【已实现】

- **history_item_id**：字符串，必填；目标记录 ID。
- **api_base_url**：字符串；打开图像文档时使用记录绑定的后端地址。
- **file_kind**：枚举；docx、structured-docx、pdf、structured-pdf。
- **share_url**：字符串；图像文档分享优先 PDF/DOCX 地址。
- **confirm_clear**：布尔；清空前二次确认。
- **删除单项**：移除浏览器历史；PDF 同时删除 IndexedDB 文件；不删除服务器扫描文档。
- **清空历史**：清除本机历史和 PDF 副本；不删除设备源文件或后端扫描文件。
- **分享**：PDF 优先系统文件分享，不支持时下载；图像文档分享取决于后端 URL 和对象存储有效期。

### 2.6 PDF 本地查看与编辑

#### 2.6.1 导入及查看器【已实现】

- **file**：浏览器 File，必填；MIME 为 application/pdf 或文件名以 .pdf 结尾。
- **file.name**：字符串，只读；源文件名。
- **file.size**：整数，只读；必须大于 0。
- **file.lastModified**：整数时间戳，只读；源文件修改时间。
- **pdf_header**：字符串，只读；前 1024 字节需包含 %PDF-。
- **page_count**：整数，只读；PDF.js 解析的总页数。
- **page_index**：整数，只读；当前页，从 0 开始。
- **zoom**：数字；查看缩放，默认 1。
- **page_width/page_height**：数字，只读；当前页渲染尺寸。
- **loading/error_message**：布尔/字符串，只读；读取状态和解析错误。
- **隐私边界**：PDF 文件仅在浏览器读取，不调用 OCR API，不上传后端。

#### 2.6.2 擦除标记【已实现】

- **mode**：枚举；select、erase、signature。
- **brush_width**：数字；擦除笔宽，初始值 24 屏幕像素。
- **strokes**：数组；每项有 width 和 points。
- **points**：坐标数组；每项 x、y 为 0–1 页面归一化值。
- **pending_erase_strokes**：数组；待确认的擦除笔迹。
- **操作**：预览、确认、取消、撤销、重做、恢复原始页。
- **约束**：逐页维护；未导出时刷新或关闭会丢失修改，关闭前需确认。

#### 2.6.3 图片签名与手写签名【已实现】

- **dataUrl**：字符串；本地 PNG Data URL。
- **pixelWidth/pixelHeight**：整数；图像像素尺寸。
- **x/y**：数字；页面归一化左上位置，范围 0–1。
- **width/height**：数字；页面归一化宽高。
- **opacity**：数字；透明度 0–1。
- **rotation**：数字；旋转度数，界面范围 -180 到 180。
- **selected_signature**：整数；当前选择索引，无选择为 -1。
- **draw_brush/draw_strokes**：数字/数组；手写画笔及笔画。
- **图片校验**：只能读图片；最长边按实现缩至不超过 2400 像素；浅色背景可能被转透明。
- **说明**：视觉签名图层不是证书数字签名，不提供身份校验或防篡改证明。

#### 2.6.4 导出与另存副本【已实现】

- **exporting**：布尔，只读；正在生成 PDF。
- **edit_revision**：整数，只读；编辑变更版本，导出过程中发生变化则废弃旧结果。
- **output_name**：字符串，只读；源名加“_编辑后.pdf”。
- **export_file**：Blob/File；当前编辑版 PDF。
- **save_copy**：事件数据；编辑版文件另存为新 PDF 历史项。
- **download**：无修改时下载原件，有修改时下载编辑版。
- **另存规则**：生成新的本地 ID，保留原历史，不覆盖源文件。
- **失败处理**：本地空间不足时提示用户下载至设备；不得悄悄丢弃原件。

### 2.7 通用状态与错误提示

#### 2.7.1 长任务交互【已实现】

- **busy**：布尔；全局任务占用状态。
- **busy_title/busy_text**：字符串；当前阶段标题和详细进度。
- **toast**：字符串；短时操作结果。
- **saving**：布尔；文档保存中。
- **request_stage**：枚举；本地准备、上传、OCR、编辑器读取、导出、本地存储。
- **error_code/detail/retry_after**：字符串/整数或空；错误标识、错误说明和等待秒数。
- **要求**：显示真实失败阶段；有限重试只用于可判定的暂时故障；失败不得伪装成成功历史。

---

## 三、后端接口设计

> API 前缀为 /api/v1，健康接口为 /health。当前接口不要求登录令牌。公网部署需另配 HTTPS、访问控制和配额。

### 3.1 健康接口 GET /health

#### 3.1.1 返回字段【已实现】

- **status**：字符串；成功值 ok。
- **source_fingerprint**：字符串；后端源码指纹。
- **api_compatibility_version**：整数；兼容版本。
- **max_upload_bytes**：整数；最大上传请求量。
- **max_source_image_bytes**：整数；最大原图量。
- 请求无参数、无请求体。

### 3.2 助手状态 GET /api/v1/assistant/status

#### 3.2.1 返回字段【已实现】

- **configured**：布尔；AI 模型是否完成配置。
- **provider**：字符串；显示服务商名称。
- **model**：字符串；默认模型名。
- **web_search_configured**：布尔；联网搜索密钥是否存在。
- 不返回任何 API 密钥。

### 3.3 助手对话 POST /api/v1/assistant/chat

#### 3.3.1 请求字段【已实现】

- **mode**：枚举，可选，默认 auto；允许 auto、plan、write_from_plan、write、revise、polish、shorten、expand、rewrite、summarize、chat。
- **messages**：数组，必填，1–80 项；每项 role 为 user/assistant，content 为 1–100,000 字符字符串。
- **stream**：布尔，可选，默认 false。
- **web_search**：布尔，可选，默认 false。
- **memory_profile_id**：字符串，可选，最多 64 字符；空值或匹配 20–64 位允许字符。
- **跨字段规则**：至少一条 user 消息；内容总和不超过 120,000 字符。

#### 3.3.2 响应与错误【已实现】

- 非流式：content、provider、model。
- 流式：application/x-ndjson；每行事件为 delta/content、done，或 error/detail。
- 未配置 AI 返回 503；上游失败返回 HTTP 错误或流式 error 事件。

### 3.4 助手导出 POST /api/v1/assistant/export/{file_format}

#### 3.4.1 请求与响应【已实现】

- **file_format**：路径枚举，必填；docx 或 pdf。
- **title**：字符串，可选，默认“未命名文档”，最多 200 字符。
- **content**：字符串，必填，1–100,000 字符。
- 成功返回 DOCX 或 PDF 二进制文件。
- 不支持格式返回 404；PDF 转换不可用返回 503；其他生成失败返回 500。

### 3.5 长期记忆接口【已实现】

#### 3.5.1 列表 GET /api/v1/assistant/memories

- 请求头 **X-DocuCraft-Memory-Profile** 必填。
- 返回 **memories** 数组，每项包含 id、content、category、created_at。
- profile 不合法返回 422，SQLite 不可用返回 503。

#### 3.5.2 删除 DELETE /api/v1/assistant/memories/{memory_id}

- 请求头 **X-DocuCraft-Memory-Profile** 必填。
- 路径 **memory_id** 必填。
- 返回 **deleted** 布尔，表示是否找到并删除。

#### 3.5.3 清空 DELETE /api/v1/assistant/memories

- 请求头 **X-DocuCraft-Memory-Profile** 必填。
- 返回 **deleted** 整数，表示删除数量。

### 3.6 OCR 转换 POST /api/v1/documents/convert

#### 3.6.1 请求和响应【已实现】

- Content-Type 为 multipart/form-data。
- **image**：图片文件，必填。
- 成功返回 DocumentResponse。
- MIME 非图片返回 415；大小超限、OCR 上游错误、无文字或导出失败返回对应错误。
- 服务端副作用：保存源图、文档文件及 JSON 元数据。

### 3.7 文档刷新 GET /api/v1/documents/{document_id}/refresh

#### 3.7.1 请求与响应【已实现】

- **document_id**：路径字符串，必填。
- 无请求体；重新载入元数据、重建或刷新下载地址及旧布局兼容数据。
- 成功返回 DocumentResponse；不存在或元数据损坏返回 404。
- 这是按已持有 ID 读取，不是带用户权限的文档查询接口。

### 3.8 源图 GET /api/v1/documents/{document_id}/source-image

#### 3.8.1 请求与响应【已实现】

- **document_id**：路径字符串，必填。
- **original**：布尔查询参数，可选，默认 false。
- 成功返回 PNG/JPEG 图像；源图或元数据缺失返回 404。
- 当前无文档所有权鉴权；部署应限制网络访问。

### 3.9 文档下载 GET /api/v1/documents/{document_id}/download/{file_kind}

#### 3.9.1 下载字段【已实现】

- **document_id**：路径字符串，必填。
- **file_kind**：枚举，必填；docx、structured-docx、pdf、structured-pdf。
- 按类型返回 DOCX 或 PDF 二进制，带下载文件名并禁用缓存。
- 不支持类型或文件缺失返回 404；远程对象读取失败返回 502。
- 文件名必须从文档元数据读取并通过扩展名检查，不接受任意路径。

### 3.10 编辑文档 POST /api/v1/documents/export-edited

#### 3.10.1 请求字段【已实现】

- **document_id**：字符串，可选；关联源文档元数据。
- **title**：字符串，可选，最多 200 字符。
- **paragraphs**：字符串数组，可选，默认空。
- **tables**：三维字符串数组，可选，默认空。
- **positioned_words**：对象数组，可选，默认空；每项：
  - **source_index**：整数或空，范围 0–1,000,000。
  - **text**：字符串，默认空，最多 10,000 字符。
  - **left/top/width/height**：有限数字，非负，最大 100,000,000。
  - **is_form_field**：布尔，默认 false。
  - **rendered_width**：有限数字或空，非负，最大 100,000,000。
- 成功返回新 DocumentResponse 和新 ID；不覆盖旧版。
- 表格依赖的原图版式失效时返回 409；生成或发布失败返回 503。

### 3.11 通用 DocumentResponse 字段【已实现】

#### 3.11.1 OCR 和编辑接口的统一返回结构

- **id**：字符串；文档 ID。
- **title**：字符串或空；标题。
- **status**：字符串；默认 completed。
- **has_tables**：布尔；是否含表格。
- **docx_url/structured_docx_url**：字符串或空；原图版及结构化 Word 地址。
- **docx_preserves_source_layout**：布尔；原图版式说明。
- **pdf_url/structured_pdf_url**：字符串或空；原图版及结构化 PDF 地址。
- **pdf_preserves_source_layout**：布尔；PDF 版式说明。
- **warnings**：字符串数组；识别/导出警告。
- **paragraphs/paragraph_styles**：段落文字和样式数组。
- **tables**：三维字符串数组。
- **table_layouts/table_cells**：表格布局与单元格几何数组。
- **image_width/image_height**：整数；源图尺寸。
- **positioned_words**：坐标文字数组。
- **editor_html**：字符串或空；编辑器页面内容。
- **source_image_url**：字符串或空；源图地址。

---

## 四、数据实体与保存方案

> 当前项目没有集中式关系数据库。此节将现有存储和未来推荐表结构分开，避免把扩展方案误认为已经实现。

### 4.1 浏览器助手会话

#### 4.1.1 快照字段【已实现】

- **conversation.id**：字符串，主键；前端生成。
- **conversation.title**：字符串；列表标题。
- **conversation.mode**：字符串；工作流模式。
- **conversation.draft**：字符串；未发送草稿。
- **conversation.createdAt/updatedAt**：整数毫秒时间戳。
- **conversation.messages**：消息数组；结构见前文。
- **conversation.document**：对象，含 title、content。
- **activeId**：字符串或空；当前会话 ID。
- **savedAt**：整数毫秒时间戳；快照保存时间。
- **存储**：IndexedDB 数据库 iscan-assistant-storage、对象仓库 snapshots、快照主键 conversations；兼容回退到 LocalStorage。

### 4.2 浏览器历史与 PDF Blob

#### 4.2.1 PDF 本地对象字段【已实现】

- **id**：字符串，主键。
- **blob**：Blob；PDF 内容。
- **name**：字符串；原文件名。
- **lastModified**：整数时间戳。
- **存储**：IndexedDB 数据库 iscan-local-documents，对象仓库以 id 为键。
- **历史索引**：LocalStorage 保存类型、标题、时间、大小、后端 API 地址和文件 URL，不含 PDF Blob。
- **恢复限制**：用户清理浏览器数据、浏览器回收空间或换设备后无法保证恢复。

### 4.3 服务端扫描文档元数据

#### 4.3.1 JSON 文件字段【已实现】

- **title**：字符串；文档标题。
- **image_width/image_height**：整数；源图像尺寸。
- **paragraphs/paragraph_styles/paragraph_layouts**：段落内容、样式及坐标布局。
- **positioned_words/source_positioned_words**：当前及初始 OCR 文字几何。
- **tables/table_layouts/table_cells/source_table_cells**：表格文本、当前布局、单元格信息及 OCR 初始基线。
- **positioned_table_words**：布尔；是否存在定位表格文字。
- **source_image_url/source_image_file**：源图地址与存储文件名。
- **source_paper_colors**：对象；源纸张颜色采样信息。
- **docx_file/structured_docx_file/pdf_file/structured_pdf_file**：生成文件名。
- **warnings/low_confidence_examples**：警告和低置信度样例。
- **layout_version 字段**：字符串或空；导出布局版本。
- **位置**：STORAGE_DIR 内文件和元数据；OSS 模式下将文件发布为对象。
- **现有限制**：没有用户 ID、工作区 ID、服务端文档列表、权限 ACL、审计日志或软删除状态。

### 4.4 长期记忆 SQLite 数据

#### 4.4.1 assistant_memories 表【已实现】

- **id**：TEXT 主键；记忆 ID。
- **profile_id**：TEXT 必填；浏览器档案 ID。
- **content**：TEXT 必填；1–800 字符。
- **content_key**：TEXT 必填；规范化内容的 SHA-256 去重键。
- **category**：TEXT 必填；semantic、episodic、procedural。
- **created_at**：TEXT 必填；UTC ISO 8601 时间。
- **唯一约束**：profile_id 与 content_key 组合唯一。
- **索引**：profile_id、created_at 降序。
- **位置**：STORAGE_DIR/assistant_memory.sqlite3。

### 4.5 账号/团队扩展建议

#### 4.5.1 用户和工作区【设计建议，当前未实现】

- **users.id**：UUID 主键。
- **users.email**：字符串，唯一；登录邮箱。
- **users.display_name**：字符串，必填；显示名称。
- **users.password_hash**：字符串；只存安全哈希，不存明文密码。
- **users.status**：枚举；active、disabled、deleted。
- **workspaces.id/name**：UUID 和必填名称。
- **workspace_members.user_id/workspace_id**：UUID 外键。
- **workspace_members.role**：枚举；owner、editor、viewer。
- **workspace_members.created_at**：带时区时间。
- **上线前前置能力**：登录校验、会话过期、租户隔离、文档授权、审计、用户导出/删除及权限撤销。

#### 4.5.2 文档与文件版本【设计建议，当前未实现】

- **documents.id**：UUID 主键。
- **documents.workspace_id/owner_user_id**：UUID；归属工作区和创建者。
- **documents.title**：字符串，必填，最多 200 字符。
- **documents.source_type**：枚举；scan_image、assistant、pdf_edit。
- **documents.status**：枚举；processing、ready、failed、deleted。
- **documents.created_at/updated_at**：带时区时间。
- **document_files.id/document_id**：UUID 主键及文档外键。
- **document_files.kind**：枚举；source_image、docx_visual、docx_structured、pdf_visual、pdf_structured。
- **document_files.storage_key**：字符串；内部对象键，不接收用户任意路径。
- **document_files.mime_type/byte_size**：MIME 字符串和字节整数。
- **document_files.sha256**：字符串，可选；完整性校验。
- **document_files.created_at**：带时区时间。
- **迁移说明**：OCR 结构仍可保存为有版本号的 JSON；关系表主要补充身份、权限、搜索、保留期和审计。

---

## 五、后端部署配置字段

### 5.1 网络与存储目录【部署配置】

#### 5.1.1 运行变量

- **HOST**：字符串；API 监听地址。
- **PORT**：整数；API 端口，本地常用 5000。
- **PUBLIC_BASE_URL**：URL 或空；生成对外文件地址时使用。
- **STORAGE_DIR**：路径；服务端持久化目录。
- **MAX_UPLOAD_MB**：正整数；最大上传 MB。
- **MAX_SOURCE_IMAGE_MB**：正整数；最大源图 MB。
- **OCR_MAX_CONCURRENT**：正整数；OCR 最大并发数。
- **OCR_REQUESTS_PER_MINUTE**：正整数；OCR 每分钟请求上限。
- **规则**：生产环境将 STORAGE_DIR 挂载到持久磁盘，并通过 HTTPS 反向代理公开服务。

### 5.2 OCR 配置【部署配置】

#### 5.2.1 Provider 及 HTTP 契约

- **OCR_PROVIDER**：枚举；mock 仅本地演示，generic_http 调用自建服务。
- **OCR_ENDPOINT**：URL；生产环境 OCR HTTP 地址。
- **OCR_API_KEY**：字符串或空；可选 Bearer 凭据。
- **OCR_LAYOUT_LANGUAGES**：字符串；布局处理语言配置。
- **HTTP 请求**：POST multipart/form-data，图片字段 image；可选 Authorization: Bearer。
- **最小响应**：段落数组 paragraphs 或 text 字符串。
- **版式响应**：建议包含 positioned_words、tables、table_layouts、table_cells、paragraph_styles 和低置信度字段。
- **失败规则**：不自动切换到未配置的 OCR 服务；有限重试后向前端报告失败。

### 5.3 AI 和搜索配置【部署配置】

#### 5.3.1 模型变量

- **AI_PROVIDER**：字符串；模型服务商标识。
- **AI_API_BASE_URL**：URL；模型 API 根地址。
- **AI_API_KEY**：字符串；模型密钥，仅后端使用。
- **AI_MODEL**：字符串；默认模型名。
- **AI_PLANNER_MODEL**：字符串或空；规划阶段模型，为空时回退默认模型。
- **AI_WRITER_MODEL**：字符串或空；按规划写作阶段模型，为空时回退默认模型。
- **AI_MAX_OUTPUT_TOKENS**：整数；文档输出 token 上限，受后端硬上限限制。
- **TAVILY_API_KEY**：字符串或空；联网搜索密钥。
- **要求**：状态接口只返回是否配置，不返回密钥值。

### 5.4 文件存储与转换工具【部署配置】

#### 5.4.1 存储变量

- **STORAGE_BACKEND**：枚举；local 或 aliyun_oss。
- **OSS_ENDPOINT**：URL/主机名；OSS 服务端点。
- **OSS_BUCKET_NAME**：字符串；Bucket 名称。
- **OSS_ACCESS_KEY_ID/OSS_ACCESS_KEY_SECRET**：字符串；对象存储凭据。
- **OSS_OBJECT_PREFIX**：字符串；对象名前缀。
- **OSS_URL_EXPIRE_SECONDS**：正整数；签名 URL 有效期。
- **LIBREOFFICE_BIN**：字符串；DOCX 转 PDF 命令，默认 soffice。
- **OCR_RENDER_FONT**：字符串或空；文档绘制字体。
- **要求**：OSS 密钥不得返回浏览器；实际访问策略和过期规则由部署端设定。

---

## 六、全局校验、安全和存储规则

### 6.1 数据格式

#### 6.1.1 通用类型约定【已实现/建议】

- 文本按 UTF-8 处理；标题最多 200 字符，单条 AI 消息最多 100,000 字符。
- 文件大小使用整数字节；金额不是当前业务字段，当前不涉及订单/收费。
- OCR 坐标使用源图像像素坐标；PDF 覆盖层使用 0–1 归一化坐标。
- 服务端时间使用 UTC ISO 8601；浏览器活动时间以整数毫秒时间戳保存。
- 用户输入、文件名、MIME、坐标和下载路径都须服务端二次校验。

### 6.2 数据去向与删除语义

#### 6.2.1 当前持久化边界【已实现】

- 扫描图与导出文件：后端本地目录或 OSS。
- AI 对话和助手文稿：浏览器 IndexedDB，兼容退回 LocalStorage。
- PDF 原件和编辑副本：浏览器 IndexedDB，不上传服务端。
- 历史列表：浏览器 LocalStorage。
- 长期记忆：服务端 SQLite，按 memory_profile_id 隔离。
- 删除浏览器历史不删除服务端扫描文件；清理浏览器数据可能清除会话和 PDF 副本。
- 多人服务上线前须定义服务端保留期、自动清理、账号删除与备份策略。

### 6.3 安全和公网部署边界

#### 6.3.1 当前限制与必要控制

- 当前 API 无登录和文档所有权校验；profile header 不构成授权。
- 公网开放可能让未授权请求消耗 OCR/AI 配额，或访问已知/泄露的文档 ID。
- 面向公众或多人使用前应实现认证、租户隔离、文档授权、配额、限速、审计、HTTPS、CORS/CSRF 策略和安全文件清理。
- 外部 URL 只从后端受控配置读取；部署时限制网络出口，避免 SSRF。
- 日志不得写密钥或不必要的完整用户正文。

---

## 七、三级扩展路线建议

### 7.1 阶段一：稳定个人版【设计建议】

#### 7.1.1 可交付功能

- 提供 AI/OCR/搜索服务配置状态页，但不显示凭据。
- 明确扫描文件服务端保留天数及删除方式。
- 对低置信度、版式丢失、转换失败显示恢复建议。
- 本地存储异常时提供会话导出和 PDF 下载提示。
- 固化 OCR Provider 契约版本，提供部署自检。
- 补充浏览器数据迁移和备份说明。

### 7.2 阶段二：账号与团队版【设计建议】

#### 7.2.1 身份与权限

- 注册、邮箱验证、登录、注销和会话过期。
- 个人空间/团队空间；成员角色 owner、editor、viewer。
- AI 会话、文档、记忆按用户和工作区隔离。
- 分享链接过期、撤销与下载权限。
- 用户数据导出、删除、审计和保留期设置。
- AI/OCR 用量统计、速率限制和团队配额。

### 7.3 阶段三：协作文档项目【设计建议】

#### 7.3.1 项目和版本字段

- **project_id**：UUID，主键。
- **workspace_id**：UUID；所属工作区。
- **name**：字符串，必填；项目名称。
- **description**：字符串，可选；项目说明。
- **owner_id**：UUID；负责人。
- **status**：枚举；active、archived。
- **created_at/updated_at**：带时区时间。
- **document_id/project_id**：UUID；文档及所属项目。
- **title**：字符串，必填；文档标题。
- **source_type**：枚举；scan_image、assistant、pdf_edit。
- **current_version_id**：UUID；当前版本。
- **version_id/parent_version_id**：UUID；版本及父版本。
- **content_ref**：字符串；版本内容/文件引用。
- **created_by/change_summary/created_at**：创建者、变更说明和时间。
- **评审点**：并发编辑冲突、版本恢复、评论、共享授权、对象存储成本和 AI/OCR 费用归属。

---

## 八、开发前待确认与验收范围

### 8.1 待确认事项

#### 8.1.1 产品决策

- 继续作为无账号个人工具，还是转为公网多租户/团队产品。
- 扫描图、OCR 元数据和导出文件默认保留时间及清理入口。
- AI 会话是否需要加密云端同步。
- 长期记忆是否默认启用，是否应在保存记忆前让用户确认。
- AI、OCR、联网搜索服务商和隐私告知范围。
- OCR 返回字段、坐标单位及契约版本策略。
- 原图版和结构化版导出的优先级及失败展示方式。
- PDF 编辑是否只需视觉图层；若要法律效力签名需单独设计证书签名。
- 目标并发量、最大图像/PDF 尺寸、磁盘配额和服务可用性要求。

### 8.2 验收建议【设计建议】

#### 8.2.1 核心业务链路

- OCR：类型/大小限制、方向处理、无文字、低置信度、段落顺序、空单元格、合并单元格、坐标越界、原图/结构化导出。
- 编辑：段落与定位词同步、表格编辑、另存新版本、旧版可访问、历史失败恢复。
- AI：全部模式请求契约、规划后写作、流式中断、搜索开关、配置缺失、文稿编辑及 Markdown/DOCX/PDF 导出。
- 记忆：列表、删除单条、清空、重复去重、非法 profile、SQLite 不可用。
- PDF：无效文件、页码、擦除预览、撤销重做、签名变换、导出、另存新文件、原件保留。
- 浏览器存储：IndexedDB 不可用、空间不足、刷新恢复、删除/清空语义和本地数据丢失提示。
- 部署：健康检查、版本兼容、OCR 上游错误、OSS URL 过期、服务端下载错误及限速行为。

---

## 九、仓库依据

- README.md、backend/README.md：项目能力、部署配置和主要 API。
- docs/architecture.md：系统架构及数据存储边界。
- docs/self-hosted-ocr.md：OCR HTTP 请求、响应字段与坐标契约。
- docs/ai-writing-workflow.md：AI 工作流与会话交互。
- backend/app/models.py、backend/app/main.py：API 模型、路由和文档流程。
- backend/app/services/assistant_memory.py、backend/app/services/storage.py：记忆和文件存储。
- frontend/src/App.vue、frontend/src/AssistantWorkspace.vue、frontend/src/PdfEditor.vue：前端功能实现。
- frontend/src/assistant-state.js、frontend/src/assistant-storage.js、frontend/src/local-documents.js：浏览器数据持久化。
