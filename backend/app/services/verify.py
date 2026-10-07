"""闭环版式验证（用户规格第 5 步）：把"逐页目检 + 墨迹版心测量"做成流水线阶段。

对一篇 arXiv 源码直译的产出做四件事：

1. **逐页渲染** PNG（默认 110dpi）供人工逐页目检；
2. **逐页测量墨迹边界框**（文本块 + 矢量图 + 位图），与**版心**对照：版心由基线
   （同一份源码"未翻译"编译出的 PDF）逐页外接框的中位数/并集估计——横向取中位数
   （页边距稳定），纵向取并集（内容多少随文字长度变化）。超出版心的量即 overflow；
3. **与基线逐页对照**给出版心偏差，并对存疑页裁剪**高倍对照图**（译文/基线成对），
   用来区分"真问题"（溢出、重叠）与"提取假阳性"（中文断行、内容位移）；
4. **内容完整性**：图表标题数、章节标题数、参考文献标题、引用/交叉引用是否解析。

判定口径（这是本模块最重要的部分）：
- **硬性不通过**：新增 overflow（基线没有、译文有）、内容条目数不一致、存在未翻译块、
  引用或交叉引用未解析——这些才是真问题；
- **仅提示**：页数与基线不同（中文更紧凑属正常，且页数一变逐页对照的意义就下降）、
  与基线相同位置的 overflow（作者原版瑕疵，按忠实原则保留）、版心偏差页（需目检，
  放大图已生成）。
"""
import json
import logging
import re
import statistics
from datetime import datetime
from pathlib import Path

import pymupdf

logger = logging.getLogger(__name__)

DEFAULT_TOL_PT = 25.0
RENDER_DPI = 110
ZOOM_DPI = 220
ZOOM_MAX_PAGES = 6

_CAPTION_RE = re.compile(r"(?:Figure|图)\s*~?\s*(\d+)")
_REF_HEADING_RE = re.compile(r"(参考文献|References)")
_PURE_NUMBER_RE = re.compile(r"\d+(?:\.\d+)*")
_NUMERIC_LINE_RE = re.compile(r"^[\d.,%()\-–—\s]+$")


def section_count(text: str) -> int:
    """章节标题计数，容忍中文断行把编号与标题拆成两行。

    PDF 文本抽取里 "3.1 相关工作" 可能变成 "3.1" + "相关工作" 两行，直接用
    "编号+文字同行" 的正则会少数几条，造成"内容缺失"的假阳性。这里把
    "编号单独成行、且下一非空行是文字" 也算一条；纯数字行（坐标轴刻度）不算。
    """
    lines = text.splitlines()
    count = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if _PURE_NUMBER_RE.fullmatch(s):
            nxt = next((x.strip() for x in lines[i + 1:] if x.strip()), "")
            if nxt and len(nxt) > 1 and not _NUMERIC_LINE_RE.match(nxt):
                count += 1
        elif _PURE_NUMBER_RE.match(s) and len(s) > 1 and s[0].isdigit() \
                and re.match(r"^\d+(?:\.\d+)*\s+\S", s):
            count += 1
    return count
# 同一行内连续 ≥8 个英文单词 ≈ 成句英文散文（图内标签、人名、邮箱都达不到）
_ENGLISH_RUN_RE = re.compile(r"(?:[A-Za-z][A-Za-z''\-]*[ \t]+){7,}[A-Za-z][A-Za-z''\-]*")


def english_residue(text: str, ref_heading_re: re.Pattern = _REF_HEADING_RE,
                    limit: int = 5) -> list[str]:
    """参考文献之前的长英文行：译文里出现即说明有内容没被翻译。

    编译失败时编排器会把某个 .tex 退回英文原文重编（局部降级），这种"整节没翻译"
    只靠条数比对发现不了——必须看语言本身。
    """
    m = ref_heading_re.search(text)
    body = text[:m.start()] if m else text
    return _ENGLISH_RUN_RE.findall(body)[:limit]


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 行首的图表标题（"图 4: ..."）。行内正文引用（"如图 6 所示"）不会命中，
# 否则会把 "图 6 中 Raghu et al. 的模型" 误判成英文图注。
_CAPTION_LINE_RE = re.compile(r"(?m)^\s*(?:图|Figure)\s*~?\s*\d+\s*[:：]?\s*([^\n]{0,200})")


def english_captions(text: str, limit: int = 5) -> list[str]:
    """渲染后仍是英文的图表标题（只数实际出现在版面上的）。"""
    found = []
    for body in _CAPTION_LINE_RE.findall(text):
        body = body.strip()
        if not body or _CJK_RE.search(body):
            continue
        if len(re.findall(r"[A-Za-z]{2,}", body)) >= 3:
            found.append(body[:120])
    return found[:limit]


def ink_bbox(page: pymupdf.Page) -> pymupdf.Rect | None:
    """整页墨迹外接框：文本块 + 矢量绘制 + 位图。"""
    box: pymupdf.Rect | None = None

    def merge(r):
        nonlocal box
        r = pymupdf.Rect(r)
        box = r if box is None else box | r

    for b in page.get_text("dict")["blocks"]:
        merge(b["bbox"])
    for d in page.get_drawings():
        merge(d["rect"])
    for img in page.get_image_info():
        merge(img["bbox"])
    return box


def _boxes(doc: pymupdf.Document) -> list[pymupdf.Rect | None]:
    return [ink_bbox(doc[i]) for i in range(len(doc))]


def estimate_type_area(boxes: list[pymupdf.Rect | None]) -> pymupdf.Rect | None:
    """从基线各页墨迹外接框估计版心（横向中位数、纵向并集）。"""
    valid = [b for b in boxes if b is not None]
    if not valid:
        return None
    return pymupdf.Rect(
        statistics.median(b.x0 for b in valid),
        min(b.y0 for b in valid),
        statistics.median(b.x1 for b in valid),
        max(b.y1 for b in valid),
    )


def overflow_pt(ink: pymupdf.Rect, area: pymupdf.Rect) -> float:
    """墨迹超出版心的最大量（pt）；不超出为 0。"""
    return max(0.0, area.x0 - ink.x0, ink.x1 - area.x1,
               area.y0 - ink.y0, ink.y1 - area.y1)


def new_overflow_pages(overflow_t: list[int], overflow_b: list[int]) -> list[int]:
    """译文里超版心的页，去掉基线本来就超版心的页。

    按**页集合**取差，而不是比长度：译文 2 页超版心、基线也是 2 页但页码
    完全不同时，比长度会得出"没有新增"的结论，真正的溢出被静默放过。
    """
    base = set(overflow_b)
    return [p for p in overflow_t if p not in base]


def spacing_profile(doc: pymupdf.Document) -> dict:
    """行距画像：段内行距众数与一致性、段间基线距离。

    用基线（baseline）而不是墨迹外接框，才能区分"排版行距"与"字形上下伸展"。
    """
    from collections import Counter

    gaps: list[float] = []
    across: Counter = Counter()
    for pi in range(len(doc)):
        prev_last = None
        for block in doc[pi].get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            bases = [round(line["spans"][0]["origin"][1], 1)
                     for line in block["lines"] if line.get("spans")]
            if not bases:
                continue
            for a, b in zip(bases, bases[1:]):
                g = round(b - a, 1)
                if 4 < g < 40:          # 同一文本块内的行距
                    gaps.append(g)
            if prev_last is not None:
                g = round(bases[0] - prev_last, 1)
                if 12 < g < 26:         # 段间/标题后的基线距离
                    across[g] += 1
            prev_last = bases[-1]
    if not gaps:
        return {}
    mode, hit = Counter(gaps).most_common(1)[0]
    same = sum(1 for g in gaps if abs(g - mode) <= 0.3)
    return {
        "body_leading_pt": mode,
        "leading_uniformity": round(100 * same / len(gaps), 1),
        "paragraph_pitch_pt": across.most_common(1)[0][0] if across else None,
        "samples": len(gaps),
    }


def log_stats(log_path: Path | None) -> dict:
    """编译日志里的排版告警计数（Overfull/Underfull/未定义引用与文献）。"""
    if not log_path or not Path(log_path).exists():
        return {}
    text = Path(log_path).read_text("utf-8", errors="replace")
    return {
        "overfull_hbox": len(re.findall(r"Overfull \\hbox", text)),
        "underfull_hbox": len(re.findall(r"Underfull \\hbox", text)),
        "overfull_vbox": len(re.findall(r"Overfull \\vbox", text)),
        "undefined_ref": len(re.findall(r"Reference .*? undefined", text, re.S)),
        "undefined_cite": len(re.findall(r"Citation .*? undefined", text, re.S)),
    }


def _doc_text(doc: pymupdf.Document) -> str:
    return "".join(doc[i].get_text() for i in range(len(doc)))


def _zoom_crop(page: pymupdf.Page, box: pymupdf.Rect, pad: float = 12.0) -> pymupdf.Rect:
    rect = pymupdf.Rect(box)
    rect.x0 -= pad
    rect.y0 -= pad
    rect.x1 += pad
    rect.y1 += pad
    return rect & page.rect


def verify_translation(
    translated_pdf: Path,
    baseline_pdf: Path,
    out_dir: Path,
    tol: float = DEFAULT_TOL_PT,
    render_dpi: int = RENDER_DPI,
    zoom_dpi: int = ZOOM_DPI,
    issues: dict | None = None,
    translated_log: Path | None = None,
    baseline_log: Path | None = None,
) -> dict:
    """执行闭环验证并落盘报告（verify_report.json / verify_summary.txt）。"""
    translated_pdf, baseline_pdf, out_dir = (
        Path(translated_pdf), Path(baseline_pdf), Path(out_dir))
    render_dir, zoom_dir = out_dir / "render", out_dir / "zoom"
    render_dir.mkdir(parents=True, exist_ok=True)

    doc_t, doc_b = pymupdf.open(translated_pdf), pymupdf.open(baseline_pdf)
    boxes_t, boxes_b = _boxes(doc_t), _boxes(doc_b)
    n_t, n_b = len(doc_t), len(doc_b)
    area = estimate_type_area(boxes_b) or estimate_type_area(boxes_t)

    page_detail: list[dict] = []
    overflow_t: list[int] = []
    delta_pages: list[tuple[float, int]] = []
    for i in range(n_t):
        doc_t[i].get_pixmap(dpi=render_dpi).save(render_dir / f"p{i + 1:02d}.png")
        box = boxes_t[i]
        item: dict = {"page": i + 1}
        if box is None:
            item["note"] = "空白页或无法测量"
            page_detail.append(item)
            continue
        item["ink"] = [round(v, 1) for v in box]
        if area is not None:
            of = overflow_pt(box, area)
            item["overflow_pt"] = round(of, 1)
            if of > tol:
                overflow_t.append(i + 1)
        if i < n_b and boxes_b[i] is not None:
            bb = boxes_b[i]
            deltas = [abs(box.x0 - bb.x0), abs(box.y0 - bb.y0),
                      abs(box.x1 - bb.x1), abs(box.y1 - bb.y1)]
            item["delta_vs_baseline_pt"] = round(max(deltas), 1)
            item["deltas"] = {"dx0": round(deltas[0], 1), "dy0": round(deltas[1], 1),
                              "dx1": round(deltas[2], 1), "dy1": round(deltas[3], 1)}
            if max(deltas) > tol:
                delta_pages.append((max(deltas), i))
        page_detail.append(item)

    # 基线里本来就超版心的页（作者原版瑕疵）
    overflow_b: list[int] = []
    if area is not None:
        overflow_b = [i + 1 for i, b in enumerate(boxes_b)
                      if b is not None and overflow_pt(b, area) > tol]
    new_overflow = new_overflow_pages(overflow_t, overflow_b)

    # 存疑页放大对照（新增溢出页优先，其次版心偏差最大的页）
    zoom_pages = sorted(set(overflow_t) | {i + 1 for _, i in
                                          sorted(delta_pages, reverse=True)[:ZOOM_MAX_PAGES]})
    if zoom_pages:
        zoom_dir.mkdir(parents=True, exist_ok=True)
        for p in zoom_pages[:ZOOM_MAX_PAGES]:
            t_page, i = doc_t[p - 1], p - 1
            clip_t = _zoom_crop(t_page, pymupdf.Rect(boxes_t[i])) if boxes_t[i] else t_page.rect
            t_page.get_pixmap(dpi=zoom_dpi, clip=clip_t).save(zoom_dir / f"p{p:02d}_zh.png")
            if i < n_b and boxes_b[i] is not None:
                doc_b[i].get_pixmap(dpi=zoom_dpi,
                                    clip=_zoom_crop(doc_b[i], boxes_b[i])) \
                    .save(zoom_dir / f"p{p:02d}_src.png")

    text_t, text_b = _doc_text(doc_t), _doc_text(doc_b)
    residue = english_residue(text_t)
    zh_captions = english_captions(text_t)
    content = {
        "captions_translated": len(_CAPTION_RE.findall(text_t)),
        "captions_baseline": len(_CAPTION_RE.findall(text_b)),
        "sections_translated": section_count(text_t),
        "sections_baseline": section_count(text_b),
        "references_heading": bool(_REF_HEADING_RE.search(text_t)),
        "english_lines": len(residue),
        "english_sample": residue[0][:160] if residue else None,
        "english_captions": len(zh_captions),
        "english_caption_sample": zh_captions[0] if zh_captions else None,
    }
    spacing_t = spacing_profile(doc_t)
    spacing_b = spacing_profile(doc_b)
    content["spacing"] = spacing_t
    if content.get("spacing"):
        uni_t = spacing_t["leading_uniformity"]
        uni_b = spacing_b.get("leading_uniformity") if spacing_b else None
        # 行距"不统一"的判定与具体行距值无关：只看一致性是否明显差于基线（或绝对偏低）
        if (uni_b is not None and uni_t + 5 < uni_b) or uni_t < 70:
            content["spacing_warning"] = (
                f"段内行距一致性仅 {uni_t}%"
                + (f"（基线 {uni_b}%）" if uni_b is not None else "")
                + "，行距不统一")
    stats_t, stats_b = log_stats(translated_log), log_stats(baseline_log)
    overfull_new = max(0, stats_t.get("overfull_hbox", 0) - stats_b.get("overfull_hbox", 0))
    issues = dict(issues or {})

    reasons: list[str] = []
    notes: list[str] = []
    if new_overflow:
        reasons.append(f"{len(new_overflow)} 页墨迹超出基线版心 {tol}pt 以上：{new_overflow}")
    if content["captions_translated"] < content["captions_baseline"]:
        reasons.append(f"图表标题数少于基线（{content['captions_translated']} < "
                       f"{content['captions_baseline']}），可能有图注漏译或丢失")
    # 章节标题计数受断行影响会有 ±1~2 的抖动，只差一两条时作为提示而非结论
    section_gap = content["sections_baseline"] - content["sections_translated"]
    if section_gap >= 3:
        reasons.append(f"章节标题数明显少于基线（{content['sections_translated']} < "
                       f"{content['sections_baseline']}），可能有整节内容丢失")
    elif section_gap > 0:
        notes.append(f"章节标题计数少 {section_gap} 条（{content['sections_translated']} < "
                     f"{content['sections_baseline']}）：中文断行会让编号与标题分行，"
                     "属计数抖动，抽查对应页确认即可")
    if not content["references_heading"]:
        reasons.append("译文里找不到「参考文献 / References」标题")
    if content["english_lines"] >= 3:
        reasons.append(
            f"译文正文里仍有 {content['english_lines']} 行成句英文（该节可能没翻译）："
            f"「{(content['english_sample'] or '')[:60]}…」")
    if issues.get("files_reverted"):
        reasons.append(
            f"以下文件因编译失败被退回英文原文重编（该节未翻译）：{issues['files_reverted']}")
    if issues.get("chunks_structure_reverted"):
        reasons.append(
            f"{issues['chunks_structure_reverted']} 个正文块恢复占位符时结构被破坏，"
            "已回退为原文（译文可能夹带英文）")
    if content["english_captions"]:
        reasons.append(
            f"{content['english_captions']} 条图表标题仍是英文："
            f"「{(content['english_caption_sample'] or '')[:60]}…」")
    if content.get("spacing_warning"):
        reasons.append(content["spacing_warning"])
    if issues.get("chunks_untranslated"):
        reasons.append(f"{issues['chunks_untranslated']} 个正文块未翻译（已保留英文）")
    if issues.get("chunks_timeout") or issues.get("captions_timeout"):
        n_to = (issues.get("chunks_timeout") or 0) + (issues.get("captions_timeout") or 0)
        reasons.append(
            f"{n_to} 块翻译超时未返回（已放弃该块并保留原文）："
            "多为翻译服务被限流或单块过大，可调低配置档 qps 后重试")
    if issues.get("captions_untranslated"):
        # 阶段计数包含未渲染的条件分支，故先作提示；真被渲染出来的英文图注
        # 由上方的"图表标题仍是英文"检查兜住。
        notes.append(f"翻译阶段有 {issues['captions_untranslated']} 条图注翻译失败并回退原文"
                     "（若位于未渲染的条件分支则不影响成品）")
    if stats_t.get("undefined_cite"):
        reasons.append(f"{stats_t['undefined_cite']} 处引用未解析（作者 .bbl 是否生效？）")
    if stats_t.get("undefined_ref"):
        reasons.append(f"{stats_t['undefined_ref']} 处交叉引用未解析")
    if overfull_new:
        reasons.append(f"比基线新增 {overfull_new} 处 Overfull hbox")

    if n_t != n_b:
        notes.append(f"页数与基线不同（译文 {n_t} / 基线 {n_b}）：中文更紧凑属正常，"
                     "逐页对照仅在内容对齐的页面上有效")
    if overflow_b and not new_overflow:
        notes.append(f"基线第 {overflow_b} 页同样超出理论版心：属作者原版瑕疵，"
                     "按忠实原则保留，未去「修好」")
    elif overflow_b and new_overflow:
        notes.append(f"基线第 {overflow_b} 页本身超出理论版心（作者原版瑕疵）；"
                     f"译文另有第 {new_overflow} 页为新增溢出，需目检")
    if delta_pages:
        top = ", ".join(f"p{i + 1}({d:.0f}pt)" for d, i in
                        sorted(delta_pages, reverse=True)[:ZOOM_MAX_PAGES])
        notes.append(f"与基线版心偏差超过 {tol}pt 的页：{top} —— 已生成放大对照图，"
                     "用于区分真问题与提取假阳性")

    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "translated_pdf": str(translated_pdf),
        "baseline_pdf": str(baseline_pdf),
        "pages": {"translated": n_t, "baseline": n_b},
        "type_area_pt": [round(v, 1) for v in area] if area else None,
        "tolerance_pt": tol,
        "page_detail": page_detail,
        "overflow": {"translated_pages": overflow_t, "baseline_pages": overflow_b,
                     "new_pages": new_overflow},
        "content": content,
        "layout_warnings": {
            "translated": stats_t, "baseline": stats_b,
            "new_overfull_vs_baseline": overfull_new,
            "note": "与基线相同的 Overfull 属作者原版瑕疵，按忠实原则保留",
        },
        "translation_issues": issues,
        "baseline_content": {"spacing": spacing_b},
        "verdict": "需人工复核" if reasons else "通过",
        "reasons": reasons,
        "notes": notes,
        "artifacts": {"render_dir": str(render_dir),
                      "zoom_dir": str(zoom_dir) if zoom_pages else None},
    }
    doc_t.close()
    doc_b.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "verify_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    (out_dir / "verify_summary.txt").write_text(summary_text(report), "utf-8")
    logger.info("verify: verdict=%s reasons=%s notes=%s",
                report["verdict"], reasons, notes)
    return report


def summary_text(report: dict) -> str:
    """人可读的验证小结（中文）。"""
    pages = report["pages"]
    lines = [
        "闭环版式验证报告（用户规格第 5 步）",
        f"时间：{report['created_at']}",
        f"译文：{report['translated_pdf']}",
        f"基线：{report['baseline_pdf']}（同一份源码未翻译编译，版式基准）",
        f"页数：译文 {pages['translated']} / 基线 {pages['baseline']}",
        f"版心（基线估计）：{report['type_area_pt']}  阈值 {report['tolerance_pt']}pt",
        "",
        "逐页码：墨迹外接框 / 超版心 / 与基线偏差",
    ]
    for p in report["page_detail"]:
        if "ink" in p:
            lines.append(
                f"  p{p['page']:>3}: 超版心 {p.get('overflow_pt', 0):>6.1f}pt"
                f"  与基线偏差 {p.get('delta_vs_baseline_pt', 0):>6.1f}pt")
        else:
            lines.append(f"  p{p['page']:>3}: {p.get('note', '')}")
    c = report["content"]
    baseline_spacing = report.get("baseline_content", {}).get("spacing", {})
    lines += [
        "",
        f"内容完整性：图表标题 {c['captions_translated']}/{c['captions_baseline']}，"
        f"章节标题 {c['sections_translated']}/{c['sections_baseline']}，"
        f"参考文献标题 {'有' if c['references_heading'] else '无'}，"
        f"成句英文残留 {c.get('english_lines', 0)} 行，"
        f"英文图表标题 {c.get('english_captions', 0)} 条",
        f"行距：段内 {c.get('spacing', {}).get('body_leading_pt', '?')}pt"
        f"（一致性 {c.get('spacing', {}).get('leading_uniformity', '?')}%），"
        f"段间基线 {c.get('spacing', {}).get('paragraph_pitch_pt', '?')}pt"
        f"（基线文档：段内 {baseline_spacing.get('body_leading_pt', '?')}pt"
        f" 一致性 {baseline_spacing.get('leading_uniformity', '?')}%）",
        f"超版心页：译文 {report['overflow']['translated_pages']} / "
        f"基线 {report['overflow']['baseline_pages']}（新增 {report['overflow']['new_pages']}）",
        f"排版告警：译文 {report['layout_warnings']['translated']}",
        f"          基线 {report['layout_warnings']['baseline']}",
        f"翻译阶段问题：{report['translation_issues'] or '无'}",
        "",
        f"结论：{report['verdict']}",
    ]
    lines += [f"  原因：{r}" for r in report["reasons"]]
    lines += [f"  提示：{n}" for n in report["notes"]]
    lines.append(f"\n逐页渲染图：{report['artifacts']['render_dir']}")
    if report["artifacts"].get("zoom_dir"):
        lines.append(f"存疑区域放大对照：{report['artifacts']['zoom_dir']}")
    return "\n".join(lines) + "\n"
