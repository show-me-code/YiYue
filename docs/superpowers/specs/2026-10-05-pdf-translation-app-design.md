# 设计文档：「译阅」(YiYue) — 本地 PDF 翻译工作台

- 日期：2026-10-05
- 状态：已确认核心方向（用户选定：本地 Web 应用 / 基于 BabelDOC / OpenAI 兼容接口 / PDF 核心功能）

## 1. 背景与目标

目标体验：上传学术/技术 PDF，调用**用户自己的 LLM API**（OpenAI 兼容协议）进行翻译，**保持原有版式**（公式、图表、字体样式、引用跳转），并在浏览器中**双栏对照阅读**，支持"翻完即读"。

### 目标（第一版）

1. 接入用户自己的 OpenAI 兼容 API（多配置档：Base URL / API Key / 模型名 / QPS）。
2. PDF 格式保持翻译：保留公式（位图原样）、图表、颜色、字体样式；参考文献与作者默认不译；无水印输出。
3. 双栏对照阅读器：左原文右译文，同步滚动/缩放，可切换 仅译文 / 双栏 对照模式。
4. 翻完即读：大文件分段顺序翻译，每段完成即增量提供阅读。
5. 术语表：手动维护 + CSV 导入导出；自动术语提取结果可保存复用。
6. 翻译缓存：相同文件+选项的重复翻译秒出；保留 BabelDOC 内置译文缓存。
7. 任务队列与历史：批量上传排队、进度实时可见、可取消/重试/删除。
8. 页码范围翻译、扫描件（实验性）透传开关。

### 非目标（二期候选）

DOCX / EPUB / 图片 / 漫画翻译；完整 OCR 流水线；可编辑 Word 导出；多用户/鉴权；桌面壳（Tauri）。

## 2. 技术选型与已核实事实

| 项 | 选择 | 依据 |
|---|---|---|
| 形态 | 本地 Web 应用（浏览器访问 localhost） | 用户选定；开发快、跨平台、双栏阅读体验好 |
| 翻译引擎 | BabelDOC（进程内嵌入，锁定版本） | 用户选定；格式保持业界最好水平 |
| 后端 | Python 3.12 + FastAPI + Uvicorn + SQLModel(SQLite) | BabelDOC 为 Python 库，单进程嵌入最顺 |
| 前端 | Vue 3 + Vite + pdfjs-dist + Element Plus | 快速构建表单/列表/阅读器 |
| 通信 | REST + SSE（任务进度流） | 进度事件为单向流，SSE 足够且可断线重连 |
| 包管理 | uv（Python）/ npm（前端） | 本机已具备：Python 3.12.4、uv 0.12.3、Node 26 |

**BabelDOC 集成要点（已对照源码核实）**：

- 入口：`babeldoc.format.pdf.high_level.async_translate(config: TranslationConfig)` —— 异步生成器，依次产出 dict 事件：`progress_start` / `progress_update`（含 `stage`, `stage_progress`, `stage_current/total`, `overall_progress`）/ `progress_end` / `finish` / `error`。
- 结果：`TranslateResult` 含 `original_pdf_path`, `mono_pdf_path`, `dual_pdf_path`, `no_watermark_mono_pdf_path`, `no_watermark_dual_pdf_path`, `auto_extracted_glossary_path`, `total_seconds`, token 统计。
- 配置：`TranslationConfig(translator, input_file, lang_in, lang_out, pages, no_dual/no_mono, qps, pool_max_workers, glossaries=[Glossary], auto_extract_glossary, watermark_output_mode=NoWatermark, ocr_workaround, auto_enable_ocr_workaround, skip_scanned_detection, custom_system_prompt, ...)`。
- 术语表：`babeldoc.glossary.Glossary / GlossaryEntry` 对象直传，无需落 CSV。
- 翻译器：实现 `babeldoc.translator.translator.BaseTranslator` 的 OpenAI 系翻译器（随 BabelDOC 版本引用其内部实现，经 Runner 隔离层封装）。
- 资产：首次运行需下载字体/版面模型；`babeldoc.assets.assets.warmup` + `high_level.init()`；支持离线资产包。
- **许可证注意**：BabelDOC 为 AGPL-3.0。自用/自托管合规；若将本应用对外提供网络服务，需按 AGPL 开源相应代码。

## 3. 总体架构

```
浏览器 (Vue 3 + pdf.js)
  配置页 │ 任务列表/上传 │ 双栏对照阅读器
        │ REST + SSE
FastAPI 后端 (uvicorn, 单进程)
  ├─ profiles API     配置档 CRUD + 连接测试
  ├─ tasks API        上传/启动/取消/重试/删除 + SSE 进度
  ├─ glossary API     术语 CRUD + CSV 导入导出
  ├─ files API        PDF 流式读取（支持 Range，供 pdf.js）
  └─ services
      ├─ orchestrator  任务队列（串行）、状态机、分段调度、增量合并
      ├─ babeldoc_runner  TranslationConfig 映射 + async_translate 事件转换（唯一接触 BabelDOC 的模块）
      ├─ cache         任务级缓存键（sha256(文件)+sha256(选项)）
      └─ assets        启动预热 / 资产状态
SQLite (data/app.db) │ data/tasks/<task_id>/ 存原文件与输出
```

## 4. 关键设计

### 4.1 任务状态机与分段"翻完即读"

状态：`queued → translating(part_i/total, 每段含阶段进度) → merging → done | failed | cancelled`。

- 页数 ≤ part_size（默认 20，0 表示整本）时单次调用 BabelDOC。
- 更大的文件按 part_size 切段（pypdf 切分），**顺序**翻译各段；每段完成即：
  1. 将该段 mono/dual PDF 追加进任务的合并输出文件（pypdf `insert_pdf`）；
  2. 推送 SSE 事件 `pages_available`；
  3. 阅读器收到后增量重新加载（pdf.js 重新打开合并文件并保留当前阅读位置）。
- 段边界可能损失跨页段落衔接，这是"翻完即读"的已知代价；默认 20 页平衡延迟与质量，可调。
- 任务级互斥：同一时刻仅一个任务在翻译（保护 API 限流），其余排队。

### 4.2 阅读器

- 自研双栏：左右两个 pdf.js canvas 视图，分别加载原文与合并后的译文（mono，无水印）PDF。
- 同步规则：以页面为锚（左页 n ⇄ 右页 n），缩放联动；可锁定/解除同步。
- 模式切换：双栏对照 / 仅译文；下载按钮提供 单语译文 PDF、拼版双语 PDF、原文 PDF。
- 渐进加载：完成页数随 SSE 更新，未完成页显示"翻译中"占位。

### 4.3 缓存

- 键 = `sha256(pdf bytes) + sha256(规范化的选项 JSON)`（选项含语言对、页码范围、part_size、术语表内容哈希、扫描件开关等）。
- 命中 → 任务直接置为 done 并引用历史输出（复制链接，不复制文件）。
- BabelDOC 内置译文缓存保持开启（同一文件重试/分段重跑时省 token）。

### 4.4 术语表

- 表字段：`source, target, tgt_lang, enabled`；作用于任务时按 `tgt_lang` 过滤生成 `Glossary` 快照（随任务保存，保证可复现）。
- 自动提取：任务完成后若产生 `auto_extracted_glossary_path`，提供下载与"导入到我的术语表"。

### 4.5 API 概览（REST）

```
GET/POST/PUT/DELETE /api/profiles        POST /api/profiles/{id}/test
POST   /api/tasks  (multipart: file + options)   GET /api/tasks?status=
GET    /api/tasks/{id}   POST /api/tasks/{id}/cancel|retry   DELETE /api/tasks/{id}
GET    /api/tasks/{id}/events        (SSE: progress / pages_available / done / error)
GET    /api/tasks/{id}/files/{kind}  (kind: original|mono|dual, 支持 Range)
GET/POST/PUT/DELETE /api/glossary    GET /api/glossary/export  POST /api/glossary/import
GET    /api/assets/status            POST /api/assets/warmup
```

## 5. 数据模型（SQLite / SQLModel）

- `Profile(id, name, base_url, api_key, model, qps=4, pool_max_workers, custom_system_prompt?, is_default, created_at)`
- `GlossaryEntry(id, source, target, tgt_lang, enabled, created_at)`
- `Task(id, filename, file_hash, size_bytes, page_count, status, error, profile_id, lang_in, lang_out, pages, part_size, scan_mode, glossary_snapshot_json, cache_key, progress_json, created_at, started_at, finished_at)`
- `TaskOutput(id, task_id, kind: original|mono|dual, path)`；缓存命中时 Task 指向已有输出目录。

## 6. 错误处理

- 段级重试：每段失败重试 2 次（指数退避）；仍失败 → 任务 failed，保留已完成段的输出并在详情页展示 BabelDOC/HTTP 错误。
- API 401/429/超时：错误信息透传到任务详情与 SSE `error` 事件。
- SSE 断线：前端自动重连（EventSource 原生），进度从 `GET /api/tasks/{id}` 兜底恢复。
- 取消：cancel 调用会触发 BabelDOC 取消（`async_translate` 响应 CancelledError），清理临时段文件。
- 上传校验：仅接受 `.pdf`，大小上限默认 200MB。

## 7. 测试策略

- **单元**（pytest）：选项→TranslationConfig 映射；分段与合并（pypdf）；术语表→Glossary；缓存键稳定性。
- **集成**：pytest 起一个 mock OpenAI 兼容服务（返回确定译文），对 2 页样例 PDF 跑通 上传→翻译→mono/dual 输出→文件可读。
- **手工验收**：用户真实 API + 一篇论文 PDF，检查公式/图表/参考文献/双栏同步。

## 8. 里程碑

- M1 后端骨架：FastAPI + SQLite + profiles/tasks/glossary/files API + babeldoc_runner（整本翻译）+ SSE 进度
- M2 前端骨架与阅读器：配置页、任务列表、上传、pdf.js 双栏对照
- M3 术语表 + 任务级缓存
- M4 分段"翻完即读" + 批量队列 + 取消/重试
- M5 打磨：资产预热页、扫描件开关、页码范围、错误展示、README/启动脚本

## 9. 目录结构

```
translate/
  backend/
    app/
      main.py  config.py  db.py  models.py
      api/{profiles,tasks,glossary,files,assets}.py
      services/{orchestrator,babeldoc_runner,cache,glossary}.py
    tests/
    pyproject.toml
  frontend/
    src/{views,components,api,stores}/
    package.json  vite.config.ts
  docs/superpowers/specs/
  scripts/start.{bat,sh}
  README.md
```
