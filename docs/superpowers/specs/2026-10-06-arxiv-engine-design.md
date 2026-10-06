# 设计增补：混合路由之第一期 — arXiv 源码直译重编译引擎

日期：2026-10-06。决策：用户选定混合路由（C）：第一期 arXiv 源码直译（原模板编译），
第二期 MinerU 识别重建（模板风格届时再定）；现有 BabelDOC 原位翻译永久保留为兜底。

## 原理

幻觉翻译同类做法（已调研验证）：不解析 PDF，而是下载 arXiv 的 LaTeX 源码，LLM 只翻译
正文文本（公式命令/图表/引用原样保留），再用 LaTeX 编译器重新编译 —— 排版与原版一致，
公式无损。本工程用 **Tectonic**（21MB 自包含 XeTeX，按需拉宏包）替代完整 TeX Live。

## 流程与组件

```
Task(engine = auto | arxiv | babeldoc)
  auto: 检测 arXiv ID（文件名 / PDF 首页文本 / 用户输入）→ 有 ID 走 arxiv，失败或无 ID 走 babeldoc
arxiv 引擎（services/arxiv_engine.py）:
  1. 下载 https://arxiv.org/e-print/<id> （tar.gz / gz 单文件）→ task_dir/arxiv_src/
  2. 定位 main.tex（含 \documentclass 且 \begin{document}；递归解析 \input/\include 得文件清单）
  3. 用作者自带的 .bbl：\bibliography{x} 与包内 *.bbl 对齐后改写为 \input{x.bbl}
     （改名还不够：Tectonic 见到 \bibliography 会自己跑 BibTeX，用不存在的 .bib
      覆盖 .bbl，导致全篇 \cite 变成 (?)）
  4. tex 翻译（services/tex_translate.py）：
     - 保护：注释行、verbatim/lstlisting、display/inline 数学、figure*/table*/equation/align/
       gather/thebibliography 环境、\cite/\ref/\eqref/\label/\url/\href/\includegraphics/
       \input/\include/\bibliographystyle 等命令 → 占位符 [[M#]]
     - 正文按空行分段、聚成 ≤2500 字符块，逐块 LLM 翻译
     - \caption / \captionof / \title 用花括号配平单独抽出翻译（正则版会漏 \captionof{figure}{...}
       与深嵌套图注，表现为图注静默保留英文）
     - 单块译文校验：占位符数量、占位符邻位花括号、花括号/$ 总数；不合格或"原样返回"
       （历史坏缓存）时绕过缓存重试一次，仍不合格才回退原文并计入未翻译计数
     - **恢复占位符只替换占位符本身，不吞两侧花括号**：源码里 \hbox{[[M2]]} 的花括号
       是真实语法，连同占位符一起替换会产出 \hbox$x$，编译报 "Missing { inserted."，
       进而触发整节退回英文——恢复后再比对一次花括号/$ 计数，任何结构破坏都回退原文
  5. 中文预处理（prepare_for_xetex）：
     - 注入 ctex[fontset=fandol]；**记录并还原 ctex 改动的行距**（实测不改会 12 页→14 页）
     - 删除 inputenc/fontenc 行（XeTeX 冲突）
     - 汉化块插在 \begin{document} 之前（导言区最末），避免被作者自己的
       \renewcommand{\figurename}{Figure~} 覆盖
     - 样式文件（*.sty/*.cls）里硬编码的摘要标题补丁成"摘要"：
       {\large\bf Abstract} / \textbf{Abstract} / {Abstract} 三种写法
  6. Tectonic 编译（services/tectonic.py）：bin 从 GitHub release 按需下载至 data/bin/；
     编译超时 600s；失败解析日志重试 1 次 → 仍失败则整任务回退 babeldoc 引擎
  7. 闭环验证（services/verify.py，用户规格第 5 步）：
     - 基线 = 同一份源码"未翻译"编译出的 PDF（试编译阶段的产物，已含 ctex 与 .bbl 修复）
     - 逐页渲染 PNG 目检 + PyMuPDF 逐页墨迹边界框（文本块/矢量/位图）对照**版心**
       （版心由基线各页外接框的横向中位数 + 纵向并集估计），超阈值页自动裁剪高倍
       存疑区域（译文/基线成对）
     - 内容完整性：图表标题数、章节标题数、参考文献标题
     - **语言检查**：参考文献之前若残留成句英文（同一行 ≥8 个英文单词且 ≥3 行），
       判定为"整节没翻译"——编译失败时的局部降级（把某个 .tex 退回英文重编）只有
       这一项能发现，条数比对是看不出来的
     - 编译日志的 Overfull/Underfull 与基线对比：与基线一致即作者原版瑕疵，按忠实原则保留
     - 产出 data/tasks/<id>/verify/{verify_report.json, verify_summary.txt, render/, zoom/}；
       结论非"通过"时写入任务备注；GET /api/tasks/<id>/verify 可取报告
  8. 输出：mono = 编译后的译文 PDF；original = 上传/下载的原始 PDF；翻译后的 tex 源码留存
     task_dir/arxiv_out/ 供高级用户取用
```

## 缓存与幂等

- 任务级缓存键加入 `cache.PIPELINE_VERSION`：改动预处理/提示词/宏包注入等影响产出的逻辑时
  递增，旧产出不再被命中——否则会出现"代码修好了、跑出来还是旧结果"。
- 翻译器层缓存键由 BabelDOC 依 model + system prompt 等构成，提示词变更即自然失效。


## 数据与接口变更

- Task 新增列：`engine`（auto|arxiv|babeldoc，默认 auto）、`arxiv_id`（旧库自动迁移）
- create_task 表单新增：engine 选择、可选 arXiv ID/链接输入；缓存键加入 engine 与 arxiv_id
- 进度事件沿用现有 SSE；arxiv 引擎的阶段：下载源码 → 解析 → 翻译（按块报进度）→ 编译
- /api/assets 扩展 tectonic 状态（ready/warming + 下载）

## 回退矩阵

| 失败点 | 处理 |
|---|---|
| 无 arXiv ID / 源码不存在（如纯 PDF 投稿） | 直接走 babeldoc |
| 源码下载失败（网络） | 重试 2 次后回退 babeldoc |
| 编译失败（宏包/语法） | 日志分析重试 1 次 → 回退 babeldoc，任务备注"已回退" |

## 测试

- 单元：ID 检测（新式/老式/URL/文件名）、保护-恢复 roundtrip（数学/命令逐字保留）、
  分块边界、xeCJK 注入与 inputenc 清理
- 集成：真机下载 1610.01644v4 源码 + 跳过 LLM（原文编译）验证 Tectonic/字体注入链路；
  全链路用百炼账号对 task2 真跑人工验收
- 回退：断网/坏 ID 时 auto 引擎应平滑落到 babeldoc
