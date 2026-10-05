# 「译阅」实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 复刻幻觉翻译核心体验的本地 Web 应用：上传 PDF → 用户自己的 OpenAI 兼容 API 翻译（BabelDOC 引擎，保持版式）→ 浏览器双栏对照阅读，支持术语表、缓存、批量队列、"翻完即读"。

**Architecture:** FastAPI 单进程后端嵌入 BabelDOC（`async_translate` 异步事件流），SQLite 存配置/任务/术语；Vue 3 + pdf.js 前端通过 REST + SSE 交互；任务编排器串行执行队列、按段切分大文件实现增量输出。

**Tech Stack:** Python 3.12 / uv / FastAPI / SQLModel / pypdf / babeldoc==0.5.28（锁定）；Vue 3 / Vite / Element Plus / pdfjs-dist。

**执行方式说明：** 用户未在线选择执行方式，按自主模式采用 **Inline Execution（executing-plans 思路）**：本会话逐任务执行、每任务提交一次 git。

---

## 文件结构总览

```
backend/
  app/
    __init__.py
    main.py               # FastAPI 应用工厂、静态文件挂载、启动初始化
    config.py             # 路径/常量（DATA_DIR, TASKS_DIR, DB_URL…）
    db.py                 # engine、session、建表
    models.py             # SQLModel: Profile, GlossaryEntry, Task, TaskOutput
    schemas.py            # Pydantic 请求/响应模型（不含 ORM）
    api/
      __init__.py
      profiles.py         # 配置档 CRUD + 连接测试
      glossary.py         # 术语 CRUD + CSV 导入导出
      tasks.py            # 上传/列表/详情/取消/重试/删除 + SSE
      files.py            # PDF 文件流（Range 支持）
      assets.py           # BabelDOC 资产状态 + 预热
    services/
      __init__.py
      babeldoc_runner.py  # 唯一接触 BabelDOC 的模块：config 映射、事件规范化
      orchestrator.py     # 队列、状态机、分段调度、增量合并
      cache.py            # 任务级缓存键与命中
      pdfops.py           # pypdf 切分/合并/页数
  tests/
    conftest.py           # TestClient fixture、mock OpenAI 服务 fixture
    test_profiles.py  test_glossary.py  test_cache.py  test_pdfops.py
    test_tasks_flow.py    # 全链路集成（mock LLM + 2 页样例 PDF）
frontend/
  src/
    api/client.ts         # fetch 封装 + SSE
    stores/{config,tasks}.ts
    views/{SettingsView,TaskListView,ReaderView}.vue
    components/{ProfileForm,GlossaryEditor,TaskCard,PdfPane,DualReader}.vue
    App.vue  router.ts  main.ts
  package.json  vite.config.ts  index.html
scripts/start.bat  scripts/start.sh
README.md
```

**关键接口约定（后续任务必须一致）：**

- SSE 事件 JSON：`{"type":"progress","task_id":...,"part_index":i,"part_total":n,"stage":str,"stage_progress":float,"overall_progress":float}` / `{"type":"pages_available","pages_done":k}` / `{"type":"done","outputs":{...}}` / `{"type":"error","message":str}`。
- Task.status ∈ `queued | translating | merging | done | failed | cancelled`。
- 缓存键：`sha256(file_bytes)` + `sha256(canonical_options_json)` 拼接再 sha256，十六进制 64 位。
- BabelDOC 事件规范化（runner 内）：`progress_start/update/end → {stage, stage_progress(0-100), stage_current, stage_total}`，`finish → TranslateResult`，`error → BabelDOCError(message)`。

---

## Task 1: 后端骨架与依赖安装

**Files:** Create `backend/pyproject.toml`（已完成）、`backend/app/{__init__,config,db,models,main}.py`

- [x] pyproject.toml 已写入（babeldoc==0.5.28 锁定；fastapi/sqlmodel/pypdf/uvicorn/python-multipart；dev: pytest/httpx/pytest-asyncio）
- [ ] `uv sync` 安装并确认 `python -c "import babeldoc; print(babeldoc.__version__)"`
- [ ] 本地检查 BabelDOC 真实 API 形态：`OpenAI` 翻译器类位置与构造参数、`TranslationConfig` 字段、`Glossary` 构造 —— 以**已安装源码为准**修正 runner 设计（网络检索不可靠时以此为准）
- [ ] 写最小 FastAPI 应用（`/api/health` 返回 `{"status":"ok"}`），`uv run uvicorn app.main:app --port 8000` 可启动
- [ ] Commit: `feat(backend): 项目骨架与健康检查`

## Task 2: 数据模型与建表

**Files:** `backend/app/models.py`, `backend/app/db.py`, `backend/app/config.py`

- [ ] models：`Profile(name, base_url, api_key, model, qps:int=4, pool_max_workers:int|None, custom_system_prompt:str|None, is_default:bool)`；`GlossaryEntry(source, target, tgt_lang="zh", enabled=True)`；`Task(filename, file_hash, size_bytes, page_count, status="queued", error:str|None, profile_id FK, lang_in="en", lang_out="zh", pages:str|None, part_size:int=20, scan_mode:str="off", glossary_snapshot_json:str|None, cache_key:str|None, progress_json:str|None, created_at, started_at|None, finished_at|None)`；`TaskOutput(task_id FK, kind∈{original,mono,dual}, path)`
- [ ] `init_db()` 建表；pytest 验证内存库可建表可增查
- [ ] Commit

## Task 3: 配置档 API（含连接测试）

**Files:** `backend/app/api/profiles.py`, `backend/app/schemas.py`, `backend/tests/test_profiles.py`

- [ ] 测试先行：CRUD 四接口 + `POST /api/profiles/{id}/test`（用 httpx MockTransport 验证：向 `{base_url}/chat/completions` 发 1 token 请求，401/超时/成功三态）
- [ ] 实现；api_key 写接口返回时脱敏（`sk-***last4`）
- [ ] Commit

## Task 4: 术语表 API

**Files:** `backend/app/api/glossary.py`, `backend/tests/test_glossary.py`, `backend/app/services/glossary.py`

- [ ] services：`entries_to_babeldoc_glossary(entries, tgt_lang) -> babeldoc.glossary.Glossary`（过滤 enabled+tgt_lang；空列表返回 None）
- [ ] API：CRUD + `GET /export`（CSV 下载）+ `POST /import`（CSV 上传，utf-8-sig 兼容 Excel）
- [ ] 测试：CSV round-trip；空表；乱码表头报 400
- [ ] Commit

## Task 5: babeldoc_runner（核心隔离层）

**Files:** `backend/app/services/babeldoc_runner.py`, `backend/tests/test_runner_mapping.py`

- [ ] `build_translation_config(profile, task, input_path, output_dir) -> TranslationConfig`：字段映射（lang/pages/no_mono=False/no_dual=False/watermark=NoWatermark/qps/pool_max_workers/ocr_workaround+skip_scanned_detection+auto_enable_ocr_workaround 按 scan_mode/glossaries）
- [ ] `translate_stream(config) -> AsyncIterator[dict]`：包装 `async_translate`，事件转规范 dict；`finish` 事件转 `{"type":"finish","result":TranslateResult}`；异常转 `{"type":"error","message":...}`
- [ ] `check_assets() -> {"ready": bool, "missing": [...]}` 与 `warmup_assets(cb)`（调 `babeldoc.assets.assets.warmup`）
- [ ] 单测：mock async_translate（monkeypatch）验证事件规范化、error 透传；config 字段映射快照断言
- [ ] Commit

## Task 6: pdfops 与 cache

**Files:** `backend/app/services/pdfops.py`, `backend/app/services/cache.py`, `backend/tests/test_pdfops.py`, `backend/tests/test_cache.py`

- [ ] pdfops：`page_count(path)`、`split_pdf(path, part_size) -> list[Path]`（顺序命名 part_0001.pdf…）、`merge_pdfs(paths, out)`（pypdf insert_pdf）
- [ ] cache：`compute_cache_key(file_bytes, options_dict) -> str`（json.dumps(sort_keys=True,ensure_ascii=False) 规范化）；`find_cached(cache_key) -> TaskOutput 路径组 | None`（扫 tasks 目录下 cache-index.json）
- [ ] 测试：用 pypdf 现场生成 3 页 PDF 测切分/合并页数与顺序；cache 键稳定性（同输入同键、选项顺序无关）
- [ ] Commit

## Task 7: orchestrator 任务编排

**Files:** `backend/app/services/orchestrator.py`, `backend/app/api/tasks.py`, `backend/tests/test_tasks_flow.py`

- [ ] `TaskManager`：`asyncio.Queue` 串行 worker；`submit(task_id)`；`cancel(task_id)`；状态机推进；progress 写回 DB + 广播到 `asyncio.Queue[str]` 订阅者（SSE）
- [ ] 整本路径：upload 存文件→建 Task→入队→worker 调 runner→SSE progress→done（记录 outputs）
- [ ] 分段路径：`part_size>0 且 page_count>part_size` → split → 逐段 translate_stream → 每段完成 merge 增量写入 mono/dual → `pages_available` 事件 → 全部完成 done；段失败重试 2 次（退避 2s/8s）→ failed
- [ ] API：`POST /api/tasks`(multipart: file, lang_in, lang_out, pages, part_size, scan_mode, profile_id, glossary_ids) / `GET /api/tasks` / `GET /api/tasks/{id}` / `POST .../cancel|retry` / `DELETE` / `GET .../events`(SSE)
- [ ] 集成测试：conftest 起 mock OpenAI 服务（返回 `choice.delta.message` 风格补全 JSON），上传 2 页样例 PDF（fixture 现场生成），断言任务 done 且 mono/dual 文件可被 pypdf 打开
- [ ] Commit

## Task 8: files API（Range）

**Files:** `backend/app/api/files.py`

- [ ] `GET /api/tasks/{id}/files/{kind}`：StreamingResponse，解析 `Range: bytes=a-b` 返回 206（pdf.js 必需），`Accept-Ranges: bytes`，正确 Content-Length/Type
- [ ] 测试：全量 200、Range 206 首段字节、404
- [ ] Commit

## Task 9: assets API

**Files:** `backend/app/api/assets.py`

- [ ] `GET /api/assets/status`（首启后台线程触发检查）、`POST /api/assets/warmup`（SSE 或轮询进度；实现为后台任务 + status 轮询即可）
- [ ] Commit

## Task 10: 前端骨架

**Files:** `frontend/`（Vite+Vue3+TS 脚手架）、Element Plus、pdfjs-dist、router、`api/client.ts`、stores

- [ ] `npm create vite@latest frontend -- --template vue-ts`；装 `element-plus pdfjs-dist`
- [ ] vite 代理 `/api`→`http://127.0.0.1:8000`；pdf.js worker 用 `?url` 导入
- [ ] 布局：左侧导航（任务/配置/术语表），顶栏资产状态点
- [ ] Commit

## Task 11: 前端页面

**Files:** `views/SettingsView.vue`（ProfileForm 增删改查+测试连接）、`views/TaskListView.vue`（上传拖拽+选项表单+列表卡片+进度条+EventSource）、`views/ReaderView.vue` + `components/DualReader.vue`（双 pdf.js 视图、页码锚点同步、缩放联动、模式切换、渐进加载新页）、`components/GlossaryEditor.vue`

- [ ] 逐组件实现并 `npm run build` 通过
- [ ] Commit（分 2-3 次）

## Task 12: 收尾

**Files:** `scripts/start.bat|sh`（uv sync + 前端 build + 起服务）、`README.md`（安装/配置 API/使用/AGPL 说明）、根 `.gitignore`

- [ ] Windows bat + POSIX sh；README 含"连接测试→上传→双栏阅读"三步上手
- [ ] 全量测试 `uv run pytest` 通过；手工冒烟记录
- [ ] Commit

---

## 风险与回退

- babeldoc==0.5.28 安装失败/版本不存在 → 查 PyPI 最新 0.x 版本并锁定，重跑 runner 断言。
- BabelDOC 资产下载需外网，若用户环境受限 → README 提供离线资产包说明（`--generate-offline-assets`）。
- mock LLM 集成测试若 BabelDOC 对非流式/流式响应格式敏感 → 以其 OpenAI 翻译器实际请求为准调整 mock（以本地安装源码为准）。
