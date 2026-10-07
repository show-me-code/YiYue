# 闭环验证记录：审查缺陷修复轮 + arXiv 1610.01644 复跑

- 日期：2026-10-07
- 起因：按此前代码审查**已复核确认**的缺陷清单逐条修复（"按照之前核对的内容，进行修复"）
- 真跑任务：
  - 任务 #8：`engine=arxiv`，`arxiv_id=1610.01644`，en→zh，行距 1.3× → **通过**
  - 任务 #4：`engine=auto`，`arxiv_id=2607.03728`，en→zh（详见第四节与第六节）
- 产物：
  - 译文 PDF：`backend/data/tasks/8/mono.pdf`（14 页）
  - 编译基线（同源码未翻译编译）：`backend/data/tasks/8/arxiv_probe/main.pdf`（16 页）
  - 验证报告：`backend/data/tasks/8/verify/verify_report.json`、`verify_summary.txt`
  - 逐页渲染 + 存疑页放大对照：`backend/data/tasks/8/verify/render/`、`verify/zoom/`
  - 可编辑源码：`backend/data/tasks/8/arxiv_out/`

## 一、任务 #8 验证结论：通过

| 检查项 | 结果 |
|---|---|
| 页数 | 译文 14 / 基线 16（中文更紧凑，属正常） |
| 逐页墨迹超版心 | 译文 0 页 / 基线 0 页；新增溢出 0 页 |
| 图表标题 | 21 / 21（图 2/3/9 等图注、子图注均已汉化） |
| 章节标题 | 60 / 60 |
| 成句英文残留 | 0 行；英文图表标题 0 条 |
| 参考文献 | 标题存在；`\cite` 未解析 **0** 处，`\ref` 未解析 0 处（作者 `.bbl` 生效） |
| 占位符泄漏 | 成品 `.tex` 里 `[[M#]]`/`[[C#]]` **0 处**（本轮新修） |
| Overfull hbox | 译文 1 / 基线 1 —— 与源码原样编译一致，**作者原版瑕疵，按忠实原则保留** |
| 行距 | 段内 14.2pt，一致性 93.9%，段间基线 19.7pt |
| 逐页目检 | 已目检 p1（标题/摘要）、p6（图 2/3 + 子图注）、p11（参考文献）、p14（图 9，偏差最大页）；无重叠、断字、越界 |

标题抽查：`arxiv_out/main.tex:64` 为 `\title{理解中间层 \\ 使用线性分类器探针}` —— 中文标题正确落位。

## 二、本轮修复的缺陷（均先复现、后修复、带回归测试）

共 29 项：下面 27 项来自审查清单，另 2 项（第 28、29 条）是这场真跑新暴露的。

### Critical

1. **`\title` 被第一个图注顶掉**：正文图注与导言区标题各自从 `[[C0]]` 起编号，
   `put_caps` 按 `all_caps[0]` 查表，于是 `\title{My Real Title}` 被替换成第一个
   `\caption` 的内容（实测输出 `\title{译|First caption}`）。
   修复：标题接在正文图注之后编号（`_extract_translatable_blocks(start_index=)`）。
2. **繁体（`zt`）目标语言完全不注入 ctex**：`prepare_for_xetex` 只认 `zh` 前缀，
   zt 任务没有中文断行/标点禁则，标签也仍是简体。修复 + 简/繁两套标签用字。

### 数据损坏类

3. **`errors="replace"` 读 + UTF-8 写回把源码里的重音字母永久改成 `�`**（4 处写回点）。
   新增 `app/services/textio.py`：UTF-8 优先，失败退 cp1252/latin-1（单字节编码无损），
   统一以 UTF-8 写回（XeTeX 本来也只接受 UTF-8）。
4. **换行被 `read_bytes()` 丢掉通用换行转换** → CRLF 源码写回成 `\r\r\n`。
   TeX 把 CR 与 LF 都当行尾，`\r\r\n` 就是**空行 = 段落结束**：实测整篇源码每行都
   断成一段，报 `! Paragraph ended before \author was complete`，**arXiv 引擎全线回退**
   （见"三、本轮自己踩的坑"）。修复：统一换行后再写回。
5. **缓存索引读-改-写无锁 + 登记失败会把成功任务判成失败**：加进程内互斥锁、唯一临时
   文件名 + 原子替换；`register_cache` 失败只告警（调用点在成品产出**之后**）。

### 逻辑错误类

6. **`_translate_english_titles` 用 `reversed(edits)` 而不是位置倒序**：edits 是"章节标题
   先收集、子图注后收集"，倒序收集顺序 ≠ 倒序位置，前面的改动会把后面记录的偏移顶偏
   （实测把 `\section{Related Work}` 改成 `\nted Work}`）。改为按起始位置倒序。
7. **`\citep*` 的 `{key}` 没被保护**：命令名后用 `\b` 时，`*` 之后不成立边界，正则退回
   只匹配 `\citep`，星号被 `[^{}\s%]*` 吃掉、`{key}` 留在正文里当散文翻译。改用 `(?![A-Za-z])`。
8. **`_restore` 不展开嵌套占位符**：注释行整行受保护时，行内的 `\citep{key}` 已是
   `[[M0]]`，整行再包成 `[[M1]]`，单遍替换把内层 token 留在成品里
   （实测 appendix.tex/experiments.tex 有几十处）。改为迭代展开 —— 且**只展开 M 命名空间**
   （见"三"）。
9. **图注/标题占位符 `[[C#]]` 会被 `_restore` 按 M 列表查表**：迭代展开后必然命中
   **完全无关**的受保护片段，实测 `\caption{[[C7]]}` 被换成一整段
   `\begin{equation*}…\end{figure}`，编译报 `File ended while scanning use of
   \caption@ydblarg`。`_restore` 现在只认 `[[M#]]`，`[[C#]]` 留给 `put_caps`。
10. **`use_shipped_bbl` 只看"第一处 `\bibliography`"**：注释里的
    `% \bibliography{old}` 若先出现，注释被改掉、真命令原样留下，引用照样全变 `(?)`；
    多个文献库名只 `\input` 第一个、子目录名丢失。改为只在非注释代码段替换、逐个名字
    对齐 `.bbl`、保留子目录、重写全部出现处。
11. **`verify.py` 用"长度比较"判断新增溢出**：译文 2 页超版心、基线也是 2 页但页码
    完全不同时判为"没有新增"。改为按页集合取差（`new_overflow_pages`）。
12. **排队中的任务被取消 → 状态 `failed` 且错误信息为空**：`TaskCancelled` 走到通用
    异常分支。worker 现在把 `TaskCancelled` 归入 `cancelled`。
13. **`cancel()` 对已完成任务留下永久取消标记**：之后点"重试"，任务一出队就被判取消。
    改为只对"在跑或在排"的任务记标记，并给 `submit` 加去重。
14. **SSE 订阅队列无上限、订阅者集合只增不减**：队列加上限并丢最旧帧（state/progress
    都是全量快照），`unsubscribe` 清掉空集合。
15. **页码范围选不到任何页时静默产出 0 页 PDF**：`_extract_pages` 现在直接失败并报原始页数。
16. **Windows 上合并 PDF 遇到 `PermissionError` 一次就判任务失败**：阅读器正持有
    `mono.pdf`（"翻完即读"）时替换会失败；改为退避重试。

### 安全 / 健壮性

17. **tar 解包只判 `..`**：`C:/evil`、`\\server\share`、符号链接都能把写入引到目标目录之外。
    改为拒绝绝对路径/盘符/UNC/`..`/链接，并加 `filter="data"` 与解析后目录校验。
18. **CORS `allow_origins=["*"]` 且无鉴权**：任意网页都能读本机任务输出、改配置档。
    改为只放行本机来源（`localhost` / `127.0.0.1` / `[::1]`）。
19. **配置档连接测试会把用户的 API key 发给另一家厂商**：主协议探测失败后无条件用
    "另一种协议"再试，`base_url` 为空时就会打到对方官方端点。改为仅在填了自定义端点时跨协议。
20. **`free_stale_port` 会杀掉占用 8000 端口的无关进程**：改为先读命令行，只结束确认是
    本应用 uvicorn 的进程，否则明确提示不处理。
21. **`apply_babeldoc_patches.py` 每次启动都重复插入补丁**：插入型补丁用"新串在、旧串不在"
    判幂等不成立，文件每次启动都变长（实测 `_try_merge_contained_paragraphs` /
    `_redistribute_orphan_fragments` 会被插第二遍）。改为每个补丁带"新串独有标记"；
    同时不再硬编码 Windows `.venv/Lib/site-packages` 路径。
22. **`files.py` 的 Range 分支是死代码**：FastAPI 里 `range: str | None = None` 是**查询
    参数**，不是 HTTP `Range` 头，那段 206 逻辑永远不执行（真正生效的是 Starlette
    `FileResponse` 的原生 Range 支持）。删掉重复实现，保留 `Cache-Control: no-store`，
    并补测单段/后缀段/416。

### 前端

23. **`PdfPane` 载入竞态**：增量合并频繁 reload 时，旧请求晚到会把 `Loading aborted`
    写在已经加载好的文档上。加载入代次（`loadSeq`）丢弃过期结果。
24. **已渲染画布无上限**：长文档连续滚动内存持续增长。加保留半径（±8 页）回收画布，
    并保留占位高度以免滚动跳动。
25. **`DualReader` 用固定 `setTimeout(600)` 恢复阅读页**：大文件重载可能几秒，固定延时
    既可能太早也可能太晚。改为等 `loaded` 事件后再跳页。
26. **`ReaderView` 终态不关 SSE**：服务端生成器结束后 `EventSource` 不停自动重连，
    每次白占一条连接 + 一个订阅队列；`cancelled` 事件也无人处理。终态统一关闭并处理。
27. **`el-upload` 缺 `on-exceed`**：`limit=1` 时再拖一个文件"点了没反应"。改为替换旧文件。
28. **`repair_from_log` 支持 xcolor 选项冲突**（第四节）：论文 2607.03728 的源码本身
    在本工具链下编译不过，整条 arXiv 路线回退原位排版。
29. **单块翻译无墙钟上限 + arXiv 路径忽略配置档 qps**（第六节）：真跑时整篇可以卡死
    25 分钟以上不报错；qps 被静默忽略。

## 三、本轮自己踩的坑（都是闭环真跑抓出来的，如实记录）

修 8/9 两条时改出了两个新问题，靠"真跑 1610.01644"当场暴露，均已修复并补回归测试：

1. **CRLF 双写**（缺陷 4）：第一次真跑（任务 #5）试编译直接失败
   `! Paragraph ended before \author was complete`，静默回退 BabelDOC。
   定位方式：把修复前后两条路径的"预处理 + `.bbl` 改写 + 编译"分别重放，
   发现新路径的每个换行都变成 `\r\r\n`。
2. **M/C 命名空间混淆**（缺陷 9）：修完 4 之后第二次真跑（任务 #7）在**译文编译**阶段
   失败 `! File ended while scanning use of \caption@ydblarg`，`appendix.tex` 的
   `\caption{[[C7]]}` 被换成了无关的 LaTeX 片段。
   定位方式：先做"无 LLM 的确定性全管线编译"（回声翻译器跑真实源码 + Tectonic 编译）
   —— 它复现了同一处损坏，代价为 0 token。

结论：**"改完跑单测"不足以证明管线正确**，必须真跑一遍论文并逐页目检；
成本最低的中间步骤是"回声翻译器 + 真源码 + 真编译"。

## 四、另一篇论文：任务 #4（2607.03728）

该任务的源码**本身无法编译**：作者 `00README.json` 指定 TeX Live 2025 + pdflatex，
而 Tectonic 内置的是 LaTeX 2021 —— 那里的 eso-pic 会先**无选项**加载 xcolor，
之后 `iclr_palette.tex` 又写 `\usepackage[table,dvipsnames]{xcolor}`，直接
`! LaTeX Error: Option clash for package xcolor.`（实测**不加任何处理**的原始源码
同样是这个错）。

已在 `repair_from_log` 里加日志驱动修复：把作者自己写的那组选项用
`\PassOptionsToPackage{table,dvipsnames}{xcolor}` 提前到 `\documentclass` 之前，
冲突消失且作者要的 `table`/`dvipsnames` 功能照旧（属于"让源码能在本工具链下编译"，
与"保留作者原版瑕疵"的原则不冲突——原版瑕疵指版式，不指编译不过）。

## 六、任务 #4 的卡死诊断（本轮最后发现，已加防护）

修好 xcolor 之后重跑任务 #4，试编译通过、进入正文翻译，然后**整篇卡住不动**：

- 进度停在 main.tex 的 14% 超过 25 分钟；进程 CPU 累计只有 8.5s（完全空闲）；
- 日志里既没有异常，也没有 tenacity 的重试告警——不是"在重试"，是真的僵在某一层。

查 BabelDOC 0.6.4 的 `do_translate`（`translator/translator.py`）：对 `RateLimitError`
`stop_after_attempt(100)` + `wait_exponential(min=1, max=15)`，HTTP 客户端超时 600s/次
——最坏情况**单块**可以耗掉约 25 分钟；而我们的管线会一直等下去，用户只看到"翻译中"。

已加两道防护：

1. **单块墙钟上限**（默认 300s，`YIYUE_CHUNK_TIMEOUT_S` 可覆盖）：超时不重试、
   该块保留原文继续，并计入 `chunks_timeout/captions_timeout` → 写进验证报告 reasons
   （不静默）；超时时用 `faulthandler` 打印全部线程栈，下次再卡可直接定位层次。
2. **arXiv 路径显式应用配置档 qps**：BabelDOC 的 `TranslationConfig(qps=...)` 只在它
   自己的流水线里生效；arXiv 路径直接调 `translator.translate()`，以前一直在用
   BabelDOC 默认的 5 qps，用户在配置档里填的速率被静默忽略。

精确卡在哪一层尚未定论（现场没有栈可查），所以这次先让"卡死"变成"有上限、有记录、
有栈可查"，并把任务 #4 取消（它是被中断后重跑的，不是成品产出）。

## 七、复现方式

```bash
cd backend && uv run pytest -q          # 118 项（含 58 项本轮回归测试）

# 真跑一篇（走真实 LLM）：
#   POST /api/tasks  form: engine=arxiv, arxiv_id=1610.01644, profile_id=<真实配置档>
# 闭环验证报告：
#   GET  /api/tasks/8/verify
#   backend/data/tasks/8/verify/verify_summary.txt（人可读）

# 无 LLM 的确定性全管线检查（改动了 tex 管线时**先跑这个**，几秒到一分钟）：
cd backend && uv run python ../scripts/check_tex_pipeline.py \
    --src data/tasks/8/arxiv_src
# 输出：主文件/文件清单/.bbl 结果/占位符泄漏数/Tectonic 编译是否通过，退出码即结论
```

这次真跑抓到两个"改出新问题"，都属于该脚本能提前拦住的类型（见第三节）。
