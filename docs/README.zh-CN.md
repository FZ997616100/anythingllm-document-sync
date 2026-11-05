# 本地文件夹同步修复版

基于 [dastra/anythingllm-document-sync](https://github.com/dastra/anythingllm-document-sync) 修改，保留 MIT 许可证。
本工具把文件同步进 AnythingLLM，日常搜索、问答和查看出处仍使用 AnythingLLM Desktop。

## 已补齐的同步处理

- 递归扫描根目录和子目录，支持 Word DOCX、Excel XLSX、PPTX、PDF，后缀大小写不敏感。
- 一次上传返回的全部文档分别记录，Excel 多工作表一起更新、一起清理。
- 按内容检查变化，恢复旧修改时间也能发现内容更新。
- 新版全部进入工作区后才清理旧版；失败保留旧版，下次重试。
- 仅清理本工具导入的文档，手动导入的内容不参与清理。
- 根目录不可用、遍历失败时暂停删除。从配置移除目录或增加排除规则也不会清除其旧记录。
- 一次执行或持续轮询；退出后不运行，下次启动补扫描。更名按新增路径和清理旧路径处理。

清理会从 AnythingLLM 全局文档库及所有复用该对象的工作区删除本工具上传的对象。
建议单独建立同步工作区，不把需要永久保留的对象复用到其它工作区。
上传与清理期间可能短暂并存新旧版本。

## Windows 使用

需要 Python 3.12/3.13、已启动的 AnythingLLM。源码修复不会自动安装主软件、创建开机任务或读取正式资料。
按主 README 的命令创建隔离环境，将 config.example.yaml 复制为 config.local.yaml，
填写本机 API key、工作区标识和资料路径。

配置使用 UTF-8；Windows 路径建议 `C:/Knowledge`，或用 YAML 单引号。
相对资料路径以配置文件所在目录为基准。排除规则沿用上游的名称或路径子串匹配。
状态数据库放在资料目录外。每轮结束后等待设定秒数，长文档识别期间无法保证固定分钟内完成。

已有旧数据库请先备份。原记录会保留并补齐字段。旧版没有记录服务器和工作区，
首次升级应保持原目标，后续版本才会按目标分别记录。不要通过删除状态数据库重置同步。

## PDF 修复

官方核心 v1.19.0 只在整本没有可提取文字时进入 OCR，混合文字页和扫描页可能漏页；
页内图片提取也不能等同于完整页面渲染。本修复在同步端逐页处理，再通过官方 raw-text 接口入库，原 PDF 只读。

使用免费本地组件：pypdf 提取文字、PDFium 渲染整页、Tesseract 识别文字。
安装可选 requirements-pdf.txt 后，OCR 还需要本地 Tesseract 及 chi_sim、eng 语言包；
本工具不会自动下载或安装它们。配置中的 tesseract_cmd 指向引擎，
tessdata_dir 可指定本地语言包目录。
中文资料建议使用官方 [tessdata_best](https://github.com/tesseract-ocr/tessdata_best) 中的 chi_sim、eng
高精度模型，下载到本地后用 tessdata_dir 指定目录。系统自带的快速模型在干净测试页中也曾识别错字。
CI 使用固定版本的官方高精度模型，实际资料仍需抽样核验文字准确性。

| 模式 | 行为 |
| --- | --- |
| `mode: 'off'` | 默认，交给 AnythingLLM 原生 PDF 解析。 |
| `mode: auto` | 每页文字不足 30 个非空白字符时整页 OCR；适合一本中混合文字页与扫描页。 |
| `mode: force` | 每页完整渲染再 OCR；适合同一页同时有长文字层和含字图片，或文字层有误。 |

auto 无法发现已有长文字层中额外图片里的字，这类资料使用 force。
改变识别模式、语言或清晰度等 PDF 提取配置时，即使原文件没有修改也会重新处理。
force 更慢，原生文字精度也可能降低。输出保留文件名、页码和提取方式。
只有无文字且整页渲染严格全白时才标记空白页。
缺语言、超时、OCR失败或非空白页识别为空时整份文档报错，保留旧索引，不用部分结果覆盖。
加密 PDF 需要先提供有权解密的副本。OCR不能保证复杂表格结构、公式或电路图语义正确。

旧 .doc/.xls/.ppt/.wps 格式转换尚未实现。
Office 正文解析仍由 AnythingLLM 完成；图片文字、SmartArt 和公式重算不能当作已支持。

## 全本地设置

服务器地址使用 `http://127.0.0.1:3001`。
AnythingLLM 的问答模型、嵌入模型都应配置本机 Ollama，向量库存本地，
关闭不需要的联网工具和使用统计。同步器不会替你修改主软件配置。
源码、软件、模型、语言包首次下载需要联网，日常处理可以在本机完成。

## 验证边界

测试使用真实目录、SQLite、PDF解析渲染以及模拟接口响应。
GitHub CI 的 OCR 测试只使用代码生成的中文、英文、多图和空白页，无任何个人或单位资料。
真实 AnythingLLM 测试默认关闭，需要显式指定一次性测试工作区。
主软件安装后仍需用测试副本检验 Office 提取、检索命中、新旧版本切换、删除和重启补同步。

上传已在服务端完成、但响应丢失导致对象 ID 未收到时，可能留下不能自动追踪的对象。
本工具不会用清空工作区的方式处理，应对照错误记录检查。

## 官方依据与许可证

- [原生同步限制](https://docs.anythingllm.com/beta-preview/active-features/live-document-sync)
- [PDF入口](https://github.com/Mintplex-Labs/anything-llm/blob/v1.19.0/collector/processSingleFile/convert/asPDF/index.js)
- [上传与raw-text接口](https://github.com/Mintplex-Labs/anything-llm/blob/v1.19.0/server/endpoints/api/document/index.js)
- [pypdf文字提取](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)，BSD-3-Clause
- [pypdfium2](https://github.com/pypdfium2-team/pypdfium2)，Apache-2.0/BSD-3-Clause，含另列的PDFium及第三方许可证
- [Tesseract](https://github.com/tesseract-ocr/tesseract)，Apache-2.0
