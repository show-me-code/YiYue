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
  3. tex 翻译（services/tex_translate.py）：
     - 保护：注释行、verbatim/lstlisting、display/inline 数学、figure*/table*/equation/align/
       gather/thebibliography 环境、\cite/\ref/\eqref/\label/\url/\href/\includegraphics/
       \input/\include/\bibliographystyle 等命令 → 占位符 [[M#]]
     - 正文按空行分段、聚成 ≤2500 字符块，逐块 LLM 翻译（失败该块保留原文）
     - 恢复占位符；\caption{...} 单独翻译
  4. 预处理：无 xeCJK 则在 \documentclass 后注入 xeCJK + SimSun/微软雅黑；
     删除 inputenc/fontenc 行（XeTeX 冲突）
  5. Tectonic 编译（services/tectonic.py）：bin 从 GitHub release 按需下载至 data/bin/；
     编译超时 600s；失败解析日志重试 1 次 → 仍失败则整任务回退 babeldoc 引擎
  6. 输出：mono = 编译后的译文 PDF；original = 上传的原始 PDF；翻译后的 tex 源码留存
     task_dir/arxiv_out/ 供高级用户取用
```

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
