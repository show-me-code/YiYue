"""给锁定版本 BabelDOC 0.6.4 应用上游未合并的排版修复补丁。

来源（均未合入 0.6.4，实测可修复本应用的核心排版缺陷）：
- PR #617  合并垂直包含的段落，避免译文堆叠重叠
- PR #618  行延伸墨迹裁剪 + 布局框特异性排序，避免视觉行被拆碎、字号不一致
  （618 包含了 #614 对同一函数的修复意图，故 #614 无需单独应用）

幂等：重复运行自动跳过。由 launch.py / start.bat 在依赖安装后自动调用。
手工运行：cd backend && uv run python ../scripts/apply_babeldoc_patches.py
"""

import sys
from pathlib import Path


def _locate_babeldoc() -> Path:
    """定位已安装的 babeldoc 包目录。

    不再硬编码 backend/.venv/Lib/site-packages（只对 Windows + 该 venv 布局成立；
    POSIX 是 lib/pythonX.Y/site-packages）。优先问当前解释器，因为启动器就是用
    项目 venv 的 python 调本脚本的。
    """
    import importlib.util

    try:
        spec = importlib.util.find_spec("babeldoc")
    except (ImportError, ValueError):
        spec = None
    if spec and spec.submodule_search_locations:
        return Path(next(iter(spec.submodule_search_locations)))

    root = Path(__file__).resolve().parents[1]
    candidates = [root / "backend" / ".venv" / "Lib" / "site-packages" / "babeldoc"]
    candidates += sorted(
        (root / "backend" / ".venv" / "lib").glob("python*/site-packages/babeldoc")
    )
    for c in candidates:
        if c.exists():
            return c
    raise SystemExit(
        "找不到 babeldoc 包：请先在 backend/.venv 中安装依赖"
        "（cd backend && uv sync），再重跑本脚本。"
    )


# 注意：babeldoc 目录在 main() 里惰性定位，这样本模块可以被测试直接导入
# （不需要环境里已装 babeldoc）来校验下面的补丁表本身。
# (文件, [(旧串必须唯一且存在 → 新串, 幂等标记)])；旧串取自 0.6.4 实际源码。
# 幂等标记是**新串里独有**的一段文字。不能靠"新串在、旧串不在"判断：插入型补丁
# （把新方法插在旧锚点前）应用后旧串依然存在，重跑会再插一遍，文件每次启动都变长。
PATCHES = [
    (
        "format/pdf/document_il/midend/paragraph_finder.py",
        [
            # --- PR #618: _get_effective_y_bounds 交集裁剪 ---
            (
                '''        visual_box = char.visual_bbox.box
        return visual_box.y, visual_box.y2
        pdf_box = char.box
        if calculate_iou_for_boxes(visual_box, pdf_box) >= 0.5:
            return visual_box.y, visual_box.y2
        return pdf_box.y, pdf_box.y2''',
                '''        visual_box = char.visual_bbox.box
        pdf_box = char.box
        if pdf_box is not None and calculate_iou_for_boxes(visual_box, pdf_box) >= 0.5:
            y1 = max(visual_box.y, pdf_box.y)
            y2 = min(visual_box.y2, pdf_box.y2)
            if y2 > y1:
                return y1, y2
        return visual_box.y, visual_box.y2''',
                "if pdf_box is not None and calculate_iou_for_boxes",
            ),
            # --- PR #617: 合并垂直包含的段落（新方法，插在 fix_overlapping_paragraphs 前）---
            (
                '''    def fix_overlapping_paragraphs(self, page: Page):
        """
        Adjusts the bounding boxes of paragraphs on a page to resolve vertical overlaps.''',
                '''    def _try_merge_contained_paragraphs(
        self, page: Page, para1, para2
    ) -> bool:
        """Merge two vertically-contained, horizontally-overlapping paragraphs.

        Backport of upstream PR #617: such a pair usually comes from one
        visual text block split mid-line during character grouping. Both
        boxes share the same vertical territory, so typesetting would render
        each paragraph's first line at the same y position, producing
        overlapping text. Merge conservatively: same layout label, plain
        line/char compositions only, reading order preserved.
        """
        if not para1.layout_label or para1.layout_label != para2.layout_label:
            return False

        compositions1 = para1.pdf_paragraph_composition or []
        compositions2 = para2.pdf_paragraph_composition or []
        if not compositions1 and not compositions2:
            return False
        for comp in compositions1 + compositions2:
            if not comp.pdf_line and not comp.pdf_character:
                return False

        def _first_char_info(paragraph):
            for comp in paragraph.pdf_paragraph_composition or []:
                if comp.pdf_line:
                    chars = comp.pdf_line.pdf_character
                else:
                    chars = [comp.pdf_character]
                for char in chars:
                    if char.char_unicode and char.char_unicode.strip():
                        box = char.visual_bbox.box
                        return ((box.y + box.y2) / 2, box.x, box.y2 - box.y)
            return None

        info1 = _first_char_info(para1)
        info2 = _first_char_info(para2)
        if info1 is None or info2 is None:
            return False
        center1, x1, height1 = info1
        center2, x2, height2 = info2
        same_row = abs(center1 - center2) <= 0.5 * min(height1, height2)
        if same_row:
            para1_first = x1 <= x2
        else:
            para1_first = center1 > center2
        ordered = (
            compositions1 + compositions2
            if para1_first
            else compositions2 + compositions1
        )

        para1_contains_para2 = (
            para1.box.y <= para2.box.y and para1.box.y2 >= para2.box.y2
        )
        para2_contains_para1 = (
            para2.box.y <= para1.box.y and para2.box.y2 >= para1.box.y2
        )
        if para2_contains_para1 and not para1_contains_para2:
            survivor, absorbed = para2, para1
        else:
            survivor, absorbed = para1, para2

        survivor.pdf_paragraph_composition = ordered
        self.update_paragraph_data(survivor, update_unicode=True)
        page.pdf_paragraph.remove(absorbed)
        logger.debug(
            f"Merged vertically contained paragraph {absorbed.debug_id} into"
            f" {survivor.debug_id} on page {page.page_number}."
        )
        return True

    def fix_overlapping_paragraphs(self, page: Page):
        """
        Adjusts the bounding boxes of paragraphs on a page to resolve vertical overlaps.''',
                "_try_merge_contained_paragraphs",
            ),
            # --- PR #617: 循环内调用合并 ---
            (
                '''            for i in range(len(paragraphs)):
                for j in range(i + 1, len(paragraphs)):
                    para1 = paragraphs[i]
                    para2 = paragraphs[j]

                    if para1.box is None or para2.box is None:
                        continue

                    if para1.xobj_id != para2.xobj_id:
                        continue

                    # Check for overlap using the existing method
                    if self.bbox_overlap(para1.box, para2.box):
                        if self.is_bbox_contain_in_vertical(para1.box, para2.box):
                            continue''',
                '''            for i in range(len(paragraphs)):
                if paragraphs_mutated:
                    break
                for j in range(i + 1, len(paragraphs)):
                    para1 = paragraphs[i]
                    para2 = paragraphs[j]

                    if para1.box is None or para2.box is None:
                        continue

                    if para1.xobj_id != para2.xobj_id:
                        continue

                    # Check for overlap using the existing method
                    if self.bbox_overlap(para1.box, para2.box):
                        if self.is_bbox_contain_in_vertical(para1.box, para2.box):
                            if self._try_merge_contained_paragraphs(
                                page, para1, para2
                            ):
                                overlap_found_in_pass = True
                                paragraphs_mutated = True
                                break
                            continue''',
                "paragraphs_mutated = True",
            ),
            # --- PR #617: 循环初始化 ---
            (
                '''        while iterations < max_iterations:
            iterations += 1
            overlap_found_in_pass = False

            for i in range(len(paragraphs)):''',
                '''        while iterations < max_iterations:
            iterations += 1
            overlap_found_in_pass = False
            paragraphs_mutated = False

            for i in range(len(paragraphs)):''',
                "paragraphs_mutated = False",
            ),
            # --- 本地修复：孤儿片段重分配（新方法）---
            (
                '''    def fix_overlapping_paragraphs(self, page: Page):''',
                '''    def _redistribute_orphan_fragments(self, page: Page):
        """把布局框边缘切出的孤儿片段归还给同排的兄弟段落。

        版面模型返回的文本区域框偶尔比实际文字行窄，行首/行尾的个别字符
        会落到框外，被编成独立的微段落（甚至把两行的片段混在一起）。
        这些碎片翻译后会渲染成悬空断裂的单词。此处将碎片段落的所有组件
        按行归属与横向间隙归还给正确的段落，按阅读顺序重建行，并依赖
        get_char_unicode_string 的间距启发式自动补回空格。
        """
        paragraphs = page.pdf_paragraph
        if not paragraphs or len(paragraphs) < 2:
            return

        def comp_box(comp):
            if comp.pdf_line is not None:
                return comp.pdf_line.box
            if comp.pdf_character is not None:
                return comp.pdf_character.visual_bbox.box
            return None

        def comp_chars(comp):
            if comp.pdf_line is not None:
                return comp.pdf_line.pdf_character or []
            if comp.pdf_character is not None:
                return [comp.pdf_character]
            return None

        for a in list(paragraphs):
            if a not in paragraphs or a.layout_label is None:
                continue
            a_comps = a.pdf_paragraph_composition or []
            if not a_comps or len(a_comps) > 4:
                continue
            total = 0
            plausible = True
            for comp in a_comps:
                chs = comp_chars(comp)
                if chs is None:
                    plausible = False
                    break
                total += len(chs)
            if not plausible or total == 0 or total > 40:
                continue

            # 为 A 的每个组件寻找 (目标段落, 目标行组件)
            matches = []
            all_matched = True
            for comp in a_comps:
                cb = comp_box(comp)
                cy = (cb.y + cb.y2) / 2
                h = max(cb.y2 - cb.y, 1)
                best = None
                for t in paragraphs:
                    if t is a or t.layout_label != a.layout_label or t.box is None:
                        continue
                    if not (t.box.y - h * 0.5 <= cy <= t.box.y2 + h * 0.5):
                        continue
                    for tcomp in t.pdf_paragraph_composition or []:
                        tb = comp_box(tcomp)
                        if tb is None:
                            continue
                        tcy = (tb.y + tb.y2) / 2
                        if abs(tcy - cy) > 0.6 * min(tb.y2 - tb.y, h):
                            continue
                        x_overlap = min(cb.x2, tb.x2) - max(cb.x, tb.x)
                        gap = max(cb.x - tb.x2, tb.x - cb.x2)
                        if x_overlap <= 2 and gap <= 30:
                            if best is None or gap < best[0]:
                                best = (gap, t, tcomp)
                if best is None:
                    all_matched = False
                    break
                matches.append((comp, best[1], best[2]))
            if not all_matched or not matches:
                continue

            # 全部组件都能归位才执行重分配：按（行，x）重建目标段落的组件顺序
            touched = set()
            for comp, t, tcomp in matches:
                trow = comp_box(tcomp)
                comps = t.pdf_paragraph_composition
                comps.append(comp)
                # 以目标组件的行中心为插入行锚点，行内按 x 排序
                rows = []
                for c in comps:
                    b = comp_box(c)
                    cy = (b.y + b.y2) / 2
                    for row in rows:
                        if abs(row[0] - cy) <= 0.6 * max(b.y2 - b.y, 1):
                            row[1].append(c)
                            break
                    else:
                        rows.append([cy, [c]])
                rows.sort(key=lambda r: -r[0])
                t.pdf_paragraph_composition = [
                    c for row in rows for c in sorted(row[1], key=lambda c: comp_box(c).x)
                ]
                touched.add(id(t))
            for t in paragraphs:
                if id(t) in touched:
                    self.update_paragraph_data(t, update_unicode=True)
            if a in paragraphs:
                paragraphs.remove(a)
            logger.debug(
                f"Redistributed orphan fragment paragraph {a.debug_id} on page"
                f" {page.page_number} into {len(touched)} sibling(s)."
            )

    def fix_overlapping_paragraphs(self, page: Page):''',
                "def _redistribute_orphan_fragments",
            ),
            # --- 本地修复：在 process_page 末尾调用 ---
            (
                '''            self.merge_alternating_line_number_paragraphs(paragraphs)

        for paragraph in paragraphs:
            self.update_paragraph_data(paragraph, update_unicode=True)''',
                '''            self.merge_alternating_line_number_paragraphs(paragraphs)

        # 本地修复：归还布局框边缘切出的孤儿片段
        self._redistribute_orphan_fragments(page)

        for paragraph in paragraphs:
            self.update_paragraph_data(paragraph, update_unicode=True)''',
                "self._redistribute_orphan_fragments(page)",
            ),
        ],
    ),
    (
        "format/pdf/document_il/utils/layout_helper.py",
        [
            # --- PR #618: 同优先级时偏好最小（最具体）布局框，防止大框抢字 ---
            (
                '''    matching_layouts.sort(key=lambda x: (x["priority"], -x["iou"]))''',
                '''    matching_layouts.sort(
        key=lambda x: (
            x["priority"],
            0 if x["iou"] >= 0.5 else 1,
            x["area"] if x["iou"] >= 0.5 else 0.0,
            -x["iou"],
        )
    )''',
                '0 if x["iou"] >= 0.5 else 1',
            ),
        ],
    ),
]


def main() -> None:
    site = _locate_babeldoc()
    # "area" 字段是 618 排序键需要的，先确认字典构造处已包含；没有则一并补上
    layout_file = site / "format/pdf/document_il/utils/layout_helper.py"
    src = layout_file.read_text("utf-8")
    if '"area"' not in src:
        old = '''                        "iou": iou,
                    }'''
        new = '''                        "iou": iou,
                        "area": (layout.box.x2 - layout.box.x)
                        * (layout.box.y2 - layout.box.y),
                    }'''
        assert src.count(old) == 1, "layout_helper: iou 字典构造点不唯一，请人工检查"
        src = src.replace(old, new)
        layout_file.write_text(src, "utf-8")
        print("patched: layout_helper.py (area 字段)")

    for rel, replacements in PATCHES:
        f = site / rel
        src = f.read_text("utf-8")
        changed = False
        for old, new, marker in replacements:
            if marker in src:
                continue  # 已应用（按"新串独有标记"判定，插入型补丁才不会重复插入）
            n = src.count(old)
            assert n == 1, f"{rel}: 目标片段出现 {n} 次（应为 1），请人工检查。\n片段开头: {old[:80]!r}"
            src = src.replace(old, new)
            changed = True
        if changed:
            f.write_text(src, "utf-8")
            print(f"patched: {rel}")
        else:
            print(f"skip (已应用): {rel}")

    print("BabelDOC 排版补丁就绪。")


if __name__ == "__main__":
    main()
