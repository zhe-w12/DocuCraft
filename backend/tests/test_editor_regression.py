import random
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.editor import (
    _build_source_word_spatial_index,
    _cell_font_size,
    _coordinate_cell_alignment,
    _coordinate_cell_vertical_alignment,
    _join_cell_words,
    _match_source_word_index,
    recover_source_table_cells,
    render_editor_html,
    sample_source_paper_colors,
    sample_source_text_color,
)
from app.services.ocr import (
    OcrResult,
    ParagraphLayout,
    PositionedWord,
    TableCell,
    TextFragment,
    _join_ocr_lines,
    clean_ocr_text,
)


class EditorRegressionTests(unittest.TestCase):
    def test_unchanged_source_words_reuse_table_cells_without_recovery_scan(self):
        cells = [[TableCell(0, 1, 0, 1, "保持原文", 10, 20, 100, 50)]]
        source_words = [PositionedWord("单元格文字", 20, 30, 40, 10)]
        current_words = [PositionedWord("单元格文字", 20, 30, 40, 10)]

        recovered = recover_source_table_cells(cells, source_words, current_words)

        self.assertIs(recovered, cells)

    def test_spatial_source_word_matching_keeps_legacy_best_match(self):
        randomizer = random.Random(527)
        source_words = [
            PositionedWord(
                f"source-{index}",
                randomizer.uniform(-2000, 12000),
                randomizer.uniform(-2000, 12000),
                randomizer.uniform(3, 900),
                randomizer.uniform(4, 120),
            )
            for index in range(500)
        ]
        source_words.extend(
            [
                PositionedWord("wide-left-anchor", 20, 20, 600, 18),
                PositionedWord("wide-center-anchor", 200, 80, 600, 18),
                PositionedWord("tie-first", 500, 500, 20, 12),
                PositionedWord("tie-second", 500, 500, 20, 12),
                PositionedWord("invalid-source", float("nan"), 30, 10, 10),
            ]
        )
        targets = [
            PositionedWord(
                f"target-{index}",
                randomizer.uniform(-2200, 12200),
                randomizer.uniform(-2200, 12200),
                randomizer.uniform(3, 900),
                randomizer.uniform(4, 120),
            )
            for index in range(500)
        ]
        targets.extend(
            [
                PositionedWord("left-match", 21, 20, 600, 18),
                PositionedWord("center-match", 201, 80, 600, 18),
                PositionedWord("tie", 500, 500, 20, 12),
                PositionedWord("invalid", float("nan"), 30, 10, 10),
            ]
        )
        spatial_index = _build_source_word_spatial_index(source_words)

        for target in targets:
            self.assertEqual(
                _match_source_word_index(target, source_words, spatial_index),
                _match_source_word_index(target, source_words),
            )

    def test_source_word_match_survives_saved_box_width_expansion(self):
        source = [PositionedWord("左对齐项", 3, 10, 20, 12)]
        widened = PositionedWord("编辑后的长内容", 3, 10, 97, 12)
        spatial_index = _build_source_word_spatial_index(source)

        self.assertEqual(_match_source_word_index(widened, source, spatial_index), 0)
        self.assertEqual(_match_source_word_index(widened, source), 0)

    def test_source_word_match_survives_saved_center_shift(self):
        source = [PositionedWord("居中项", 40, 10, 20, 12)]
        widened = PositionedWord("编辑后的居中长内容", 10, 10, 80, 12)
        spatial_index = _build_source_word_spatial_index(source)

        self.assertEqual(_match_source_word_index(widened, source, spatial_index), 0)
        self.assertEqual(_match_source_word_index(widened, source), 0)

    def test_samples_source_ink_color_without_changing_the_result(self):
        with Image.new("RGB", (20, 20), "white") as image:
            ImageDraw.Draw(image).rectangle((5, 5, 14, 14), fill="#203040")

            self.assertEqual(sample_source_text_color(image, (0, 0, 20, 20)), "#203040")

    def test_joins_positioned_cell_words_by_source_line_and_column(self):
        words = [
            (0, PositionedWord("年级", 80, 30, 15, 10)),
            (1, PositionedWord("学校", 10, 30, 20, 10)),
            (2, PositionedWord("班级", 80, 10, 20, 10)),
            (3, PositionedWord("姓名", 10, 10, 20, 10)),
        ]

        self.assertEqual(_join_cell_words(words), "姓名班级\n学校年级")

    def test_keeps_spaces_between_adjacent_english_cell_words(self):
        words = [
            (0, PositionedWord("UPDATE", 42, 10, 38, 10)),
            (1, PositionedWord("FOR", 5, 10, 25, 10)),
            (2, PositionedWord("锁", 5, 30, 10, 10)),
        ]

        self.assertEqual(_join_cell_words(words), "FOR UPDATE\n锁")

    def test_table_opens_on_clear_structure_with_original_coordinate_switch(self):
        html = render_editor_html(
            OcrResult(
                paragraphs=["测试表格标题"],
                tables=[[["编号", "名称"], ["1", "示例"]]],
            ),
            "http://example.test/api/v1/documents/demo/source-image",
        )
        self.assertIn("id='coordinatePage'", html)
        self.assertIn("id='coordinateToggle'", html)
        self.assertIn("<div class='page table-document' id='page'>", html)
        self.assertIn("data-editor-version='38'", html)
        self.assertIn("setView('edit')", html)
        self.assertIn("setView('coordinate')", html)
        self.assertIn("function resetZoom", html)
        self.assertIn(".page{width:794px;min-width:794px;max-width:794px", html)
        self.assertIn(".original-page{display:none;width:794px;min-width:794px;max-width:794px", html)
        self.assertIn("if(coordinate)coordinate.style.zoom=zoom", html)
        self.assertIn("function fitEditPage", html)
        self.assertIn("fitZoom=Math.min(1", html)
        self.assertIn("const actualZoom=fitZoom*zoom", html)
        self.assertIn("onclick='resetZoom()'", html)
        self.assertIn("function fitPositionedWord", html)
        self.assertIn("document.querySelectorAll('[data-positioned-word]').forEach(other=>{if(other===e)return;", html)
        self.assertIn("document.querySelectorAll('[data-coordinate-cell]').forEach(other=>{", html)
        self.assertIn("unitScale=794/sourceWidth,paddingFor=height=>", html)
        self.assertIn("Math.round(height/unitScale*.08)", html)
        self.assertIn("other.classList.contains('edited')?other.offsetWidth", html)
        self.assertIn("rightLimit=Math.min(rightLimit,otherLeft-otherPadding-padding)", html)
        self.assertIn("centerAvailable=2*Math.min(center-leftLimit,rightLimit-center)", html)
        self.assertIn("Math.max(leftLimit,Math.min(rightLimit-next,centeredLeft))", html)
        self.assertIn("Math.max(base,rightLimit-baseLeft)", html)
        self.assertIn("e.style.whiteSpace='pre-wrap';e.style.height='auto'", html)
        self.assertIn("sameLine=verticalOverlap/Math.max(Math.min(baseHeight,otherHeight),1)>=.35", html)
        self.assertIn("e.style.height=Math.min(desiredHeight,maxHeight)+'px'", html)
        self.assertIn("height:e.classList.contains('edited')?", html)
        self.assertIn("source_index:e.dataset.wordIndex===undefined?null:Number(e.dataset.wordIndex)", html)
        self.assertIn("width:Number(e.dataset.width||0),rendered_width:e.classList.contains('edited')?", html)
        self.assertIn("Number.parseFloat(e.style.width||'')/coordinateScale:null", html)
        self.assertIn(
            "Number.parseFloat(e.style.height||'')/coordinateScale:Number(e.dataset.height||0)",
            html,
        )
        self.assertIn("const coordinateScale=Math.max(794/Number(document.getElementById('coordinatePage')?.dataset.sourceWidth||794),.0001)", html)
        self.assertIn("e.dataset.baseTop=e.offsetTop;e.dataset.baseHeight=e.offsetHeight", html)
        self.assertIn("document.body.dataset.dirty=dirty?'1':'0'", html)
        self.assertIn("e.classList.contains('edited')||e.innerText!==original", html)
        self.assertIn("data-source-text=", html)
        self.assertIn("source=c.dataset.sourceText??c.innerText", html)
        self.assertIn("grid[rr][cc]=isStart?(c.dataset.edited==='1'?c.innerText:source):''", html)
        self.assertIn("iscan:collect-document", html)
        self.assertIn("iscan:editor-document", html)
        self.assertIn("editorInitialSnapshot=collectDocument()", html)
        self.assertIn("type:'iscan:editor-dirty',dirty", html)
        self.assertIn(".coordinate-word:focus,.coordinate-word.edited", html)
        self.assertIn(".coordinate-word{position:absolute;z-index:2;display:block;overflow:hidden;white-space:pre-wrap;word-break:break-all;background:transparent;color:transparent", html)
        self.assertIn(".coordinate-cell{position:absolute;z-index:1;display:flex;box-sizing:border-box;overflow:hidden;white-space:pre-wrap;word-break:break-all;align-items:center;justify-content:center;text-align:center;cursor:text;user-select:text;-webkit-user-select:text;caret-color:#4f46e5;background:transparent;color:transparent", html)
        self.assertIn(".coordinate-cell[data-align='right']{justify-content:flex-end;text-align:right}", html)
        self.assertIn(".coordinate-cell[data-edited='1']{z-index:4;background:var(--paper-color,#fff);color:var(--ink-color,#111);overflow:auto}", html)
        self.assertIn("function fitCoordinateCell(e)", html)
        self.assertIn(".coordinate-cell:focus,.coordinate-cell.edited", html)
        self.assertIn("background:var(--paper-color,#fff);color:var(--ink-color,#111);overflow:auto}", html)
        self.assertIn(
            "background:var(--paper-color,#fff);color:var(--ink-color,#111)",
            html,
        )
        self.assertIn("测试表格标题", html)
        self.assertIn("原图版式校对", html)
        toolbar_end = html.index("</div><div class='tip'>")
        self.assertLess(html.index("id='originalToggle'"), toolbar_end)

    def test_text_document_does_not_create_table_editor(self):
        html = render_editor_html(OcrResult(paragraphs=["只有文字的内容"], tables=[]))
        self.assertNotIn("id='coordinatePage'", html)
        self.assertNotIn("id='coordinateToggle'", html)
        self.assertNotIn("style='display:none'", html)
        self.assertIn("只有文字的内容", html)

    def test_text_scan_with_source_coordinates_opens_on_original_layout(self):
        html = render_editor_html(
            OcrResult(
                image_width=800,
                image_height=1100,
                paragraphs=["扫描里的原始文字"],
                positioned_words=[PositionedWord("扫描里的原始文字", 80, 120, 260, 28)],
            ),
            "http://example.test/source-image",
        )

        self.assertIn("id='coordinatePage'", html)
        self.assertIn("data-positioned-word='1'", html)
        self.assertIn("文字编辑</button>", html)
        self.assertIn("style='display:none'", html)
        self.assertIn("原图版式", html)

    def test_edit_overlays_sample_the_imported_paper_color(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "gray-scan.png"
            source = Image.new("RGB", (180, 120), (178, 181, 184))
            draw = ImageDraw.Draw(source)
            draw.rectangle((54, 42, 87, 53), fill=(25, 25, 25))
            source.save(source_path)
            source.close()

            result = OcrResult(
                image_width=180,
                image_height=120,
                paragraphs=["扫描文字"],
                positioned_words=[PositionedWord("扫描文字", 50, 40, 42, 16)],
            )
            paper_colors = sample_source_paper_colors(source_path, result)
            html = render_editor_html(
                result,
                "http://example.test/source-image",
                paper_colors=paper_colors,
            )

        self.assertEqual(paper_colors["word:0"], "#b2b5b8")
        self.assertIn("--paper-color:#b2b5b8", html)
        self.assertIn("background:var(--paper-color,#fff)", html)

    def test_table_cell_overlays_sample_the_imported_paper_color(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "gray-form.png"
            source = Image.new("RGB", (180, 120), (178, 181, 184))
            draw = ImageDraw.Draw(source)
            draw.rectangle((56, 42, 84, 54), fill=(25, 25, 25))
            source.save(source_path)
            source.close()

            result = OcrResult(
                image_width=180,
                image_height=120,
                paragraphs=["表单"],
                tables=[[['姓名']]],
                table_cells=[[TableCell(0, 1, 0, 1, "姓名", 30, 25, 150, 90)]],
            )
            paper_colors = sample_source_paper_colors(source_path, result)
            html = render_editor_html(
                result,
                "http://example.test/source-image",
                paper_colors=paper_colors,
            )

        self.assertEqual(paper_colors["cell:0:0:0"], "#b2b5b8")
        self.assertIn("--paper-color:#b2b5b8", html)

    def test_reordered_words_keep_their_original_paper_colors(self):
        original_words = [
            PositionedWord("上方文字", 20, 10, 40, 14),
            PositionedWord("下方文字", 20, 80, 40, 14),
        ]
        html = render_editor_html(
            OcrResult(
                image_width=180,
                image_height=120,
                positioned_words=list(reversed(original_words)),
            ),
            "http://example.test/source-image",
            source_positioned_words=original_words,
            paper_colors={"word:0": "#ff0000", "word:1": "#00ff00"},
        )

        first_index = html.index("data-word-index='0'")
        second_index = html.index("data-word-index='1'")
        first_span = html[html.rfind("<span", 0, first_index):html.index(">", first_index)]
        second_span = html[html.rfind("<span", 0, second_index):html.index(">", second_index)]
        self.assertIn("--paper-color:#00ff00", first_span)
        self.assertIn("--paper-color:#ff0000", second_span)
        self.assertNotIn("data-edited='1'", first_span)
        self.assertNotIn("data-edited='1'", second_span)

    def test_editor_escapes_ocr_html_before_running_its_script(self):
        html = render_editor_html(
            OcrResult(paragraphs=["<script>parent.document.body.innerHTML='owned'</script>"])
        )

        self.assertIn("&lt;script&gt;parent.document.body.innerHTML=&#x27;owned&#x27;&lt;/script&gt;", html)
        self.assertNotIn("<script>parent.document.body.innerHTML", html)

    def test_text_document_keeps_prose_flow_when_coordinates_exist(self):
        html = render_editor_html(
            OcrResult(
                paragraphs=["普通文字段落不应该出现表单下划线"],
                paragraph_styles=["body"],
                paragraph_layouts=[
                    ParagraphLayout(
                        fragments=[TextFragment("普通文字段落不应该出现表单下划线", 10, 10, 220, 20)],
                        source_width=800,
                        left=10,
                        top=10,
                        right=230,
                        bottom=30,
                    )
                ],
            )
        )
        self.assertNotIn("<span class='line-field'", html)
        self.assertIn("普通文字段落不应该出现表单下划线", html)

    def test_checkbox_ocr_normalization_is_preserved(self):
        self.assertEqual(clean_ocr_text("口脱贫家庭学生"), "□脱贫家庭学生")
        self.assertEqual(clean_ocr_text("□低保边缘家庭学生"), "□低保边缘家庭学生")
        self.assertEqual(clean_ocr_text("□特困职工及其子女□最低生活保障家庭学生"), "□特困职工及其子女 □最低生活保障家庭学生")
        self.assertEqual(
            clean_ocr_text("口享受国家定期抚恤补助的优抚对象及其子女"),
            "□享受国家定期抚恤补助的优抚对象及其子女",
        )
        self.assertEqual(
            clean_ocr_text("□特困救助学生口享受国家定期抚恤补助的优抚对象"),
            "□特困救助学生 □享受国家定期抚恤补助的优抚对象",
        )
        self.assertEqual(
            clean_ocr_text("□在学人数2人口除父母外赡养老人三位及以上"),
            "□在学人数2人 □除父母外赡养老人三位及以上",
        )

    def test_checkbox_ocr_repairs_misread_markers_between_joined_options(self):
        self.assertEqual(
            clean_ocr_text(
                "□脱贫家庭学生　□防止返贫致贫对象家庭学生口原广东省户籍建档立卡家庭学生"
            ),
            "□脱贫家庭学生　□防止返贫致贫对象家庭学生 □原广东省户籍建档立卡家庭学生",
        )
        self.assertEqual(
            clean_ocr_text(
                "□最低生活保障家庭学生口低保边缘家庭学生口特困救助学生家庭学生"
            ),
            "□最低生活保障家庭学生 □低保边缘家庭学生 □特困救助学生家庭学生",
        )
        self.assertEqual(
            clean_ocr_text("□父母均没有工作口农村个体小型种植户口父母一方为初中及以下"),
            "□父母均没有工作 □农村个体小型种植户 □父母一方为初中及以下",
        )

    def test_checkbox_ocr_does_not_rewrite_prose_near_checkbox_lists(self):
        self.assertEqual(clean_ocr_text("□户口低保管理机制说明"), "□户口低保管理机制说明")
        self.assertEqual(clean_ocr_text("人口低保联动机制"), "人口低保联动机制")

    def test_chinese_form_parentheses_match_source_typography(self):
        self.assertEqual(clean_ocr_text("(2026年版)"), "（2026年版）")
        self.assertEqual(clean_ocr_text("Top-K (example)"), "Top-K (example)")
        self.assertEqual(clean_ocr_text("算法说明(示例)"), "算法说明（示例）")

    def test_known_english_terms_keep_chinese_spacing(self):
        self.assertEqual(
            clean_ocr_text("前K名，比Top-K更自然，Top-p进一步筛选"),
            "前 K 名，比 Top-K 更自然，Top-p 进一步筛选",
        )
        self.assertEqual(clean_ocr_text("第1项"), "第1项")
        self.assertEqual(
            _join_ocr_lines(["达到", "p", "的精英圈", "比", "Top-K", "更自然"]),
            "达到 p 的精英圈比 Top-K 更自然",
        )

    def test_table_coordinate_editor_uses_one_editable_overlay_per_cell(self):
        html = render_editor_html(
            OcrResult(
                paragraphs=["标题"],
                positioned_words=[
                    PositionedWord("单元格文字", 10, 10, 90, 18),
                    PositionedWord("页眉", 240, 10, 42, 18),
                ],
                tables=[[["单元格文字"]]],
                table_cells=[[TableCell(0, 1, 0, 1, "单元格文字", 0, 0, 180, 60)]],
            ),
            "http://example.test/source-image",
        )
        self.assertIn("data-coordinate-cell='1'", html)
        self.assertIn("data-base-font-size='", html)
        self.assertIn("height:", html)
        self.assertIn(">单元格文字</span>", html)
        self.assertNotIn("data-word-index='0'", html)
        self.assertIn("data-word-index='1'", html)

    def test_table_words_keep_their_original_positions_when_editing_is_enabled(self):
        cell = TableCell(0, 1, 0, 1, "□资助家庭学生   □低保家庭学生", 0, 0, 200, 40)
        words = [
            PositionedWord("□资助家庭学生", 8, 10, 58, 12),
            PositionedWord("□低保家庭学生", 112, 10, 62, 12),
        ]
        html = render_editor_html(
            OcrResult(
                image_width=200,
                image_height=100,
                positioned_words=words,
                tables=[[ [cell.text] ]],
                table_cells=[[cell]],
            ),
            "http://example.test/source-image",
            positioned_table_words=True,
        )

        self.assertEqual(html.count("data-positioned-word='1'"), 2)
        self.assertNotIn("data-coordinate-cell='1'", html)
        self.assertIn("data-word-index='0'", html)
        self.assertIn("data-word-index='1'", html)

    def test_reopened_expanded_left_cell_word_keeps_source_alignment(self):
        source_cell = TableCell(0, 1, 0, 1, "原选项", 0, 0, 100, 40)
        source_word = PositionedWord("原选项", 3, 10, 20, 12)
        edited_word = PositionedWord("编辑后的长选项内容", 3, 10, 97, 12)
        html = render_editor_html(
            OcrResult(
                image_width=100,
                image_height=60,
                positioned_words=[edited_word],
                tables=[[['编辑后的长选项内容']]],
                table_cells=[[TableCell(0, 1, 0, 1, "编辑后的长选项内容", 0, 0, 100, 40)]],
            ),
            "http://example.test/source-image",
            source_positioned_words=[source_word],
            source_table_cells=[[source_cell]],
            positioned_table_words=True,
        )

        word_markup = html.split("data-word-index='0'", 1)[1].split("</span>", 1)[0]
        self.assertIn("data-edited='1'", word_markup)
        self.assertNotIn("data-center='1'", word_markup)
        self.assertNotIn("text-align:center;", word_markup)

    def test_centered_table_word_uses_cell_center_without_heading_bold(self):
        cell = TableCell(0, 1, 0, 1, "原编号", 0, 0, 200, 40)
        html = render_editor_html(
            OcrResult(
                image_width=200,
                image_height=100,
                positioned_words=[PositionedWord("原编号", 70, 10, 60, 12)],
                tables=[[['原编号']]],
                table_cells=[[cell]],
            ),
            "http://example.test/source-image",
            positioned_table_words=True,
        )

        self.assertIn("data-center='1' data-center-x='397.0'", html)
        word_markup = html.split("data-word-index='0'", 1)[1].split("</span>", 1)[0]
        self.assertIn("text-align:center;", word_markup)
        self.assertNotIn("font-weight:700;", word_markup)

    def test_positioned_table_words_leave_blank_cells_editable(self):
        html = render_editor_html(
            OcrResult(
                image_width=200,
                image_height=100,
                tables=[[ [""] ]],
                table_cells=[[TableCell(0, 1, 0, 1, "", 0, 0, 100, 40)]],
            ),
            "http://example.test/source-image",
            positioned_table_words=True,
        )

        self.assertIn("data-coordinate-cell='1'", html)

    def test_saved_long_cell_text_stays_inside_a_scrollable_source_box(self):
        long_text = "这是一段较长的表单填写内容，用于确认保存后仍能查看单元格中的全部文字。"
        source_cell = TableCell(0, 1, 0, 1, "", 0, 0, 100, 40)
        edited_cell = TableCell(0, 1, 0, 1, long_text, 0, 0, 100, 40)

        html = render_editor_html(
            OcrResult(
                image_width=200,
                image_height=100,
                tables=[[[long_text]]],
                table_cells=[[edited_cell]],
            ),
            "http://example.test/source-image",
            source_table_cells=[[source_cell]],
            positioned_table_words=True,
        )

        self.assertIn("data-edited='1'", html)
        self.assertIn(long_text, html)
        self.assertIn("width:395.0px;", html)
        self.assertIn("height:156.8px;", html)
        self.assertIn("overflow:auto}", html)

    def test_cell_font_size_uses_source_ocr_height(self):
        self.assertAlmostEqual(_cell_font_size("标签", 200, 60, [10.0, 10.0, 12.0]), 10.8)

    def test_cell_alignment_uses_original_word_positions(self):
        cell = TableCell(0, 1, 0, 1, "", 0, 0, 100, 50)
        self.assertEqual(
            _coordinate_cell_alignment(cell, [PositionedWord("左侧", 8, 2, 20, 10)]),
            "left",
        )
        self.assertEqual(
            _coordinate_cell_alignment(cell, [PositionedWord("居中", 38, 18, 24, 12)]),
            "center",
        )
        self.assertEqual(
            _coordinate_cell_alignment(cell, [PositionedWord("右侧", 72, 2, 20, 10)]),
            "right",
        )
        self.assertEqual(
            _coordinate_cell_vertical_alignment(cell, [PositionedWord("靠上", 8, 2, 20, 10)]),
            "top",
        )

    def test_coordinate_scale_uses_original_image_width(self):
        html = render_editor_html(
            OcrResult(
                image_width=1000,
                image_height=1400,
                positioned_words=[PositionedWord("页眉", 500, 20, 100, 20)],
                tables=[[[""]]],
                table_cells=[],
            ),
            "http://example.test/source-image",
        )
        # 500 source pixels must map to 397px on the 794px editor canvas;
        # deriving the scale from the word extent (600px) would put it at
        # roughly 662px and reproduce the old left/right blank-space bug.
        self.assertIn("left:397.0px", html)

    def test_heading_overlay_stays_centered_when_edited(self):
        html = render_editor_html(
            OcrResult(
                paragraphs=["原始标题"],
                paragraph_styles=["title"],
                positioned_words=[PositionedWord("原始标题", 300, 20, 120, 24)],
                tables=[[['编号', '内容'], ['1', '示例']]],
                table_cells=[[
                    TableCell(0, 1, 0, 1, "编号", 20, 80, 100, 120),
                    TableCell(0, 1, 1, 2, "内容", 100, 80, 220, 120),
                ]],
            ),
            "http://example.test/source-image",
        )
        self.assertIn("data-center='1'", html)
        self.assertIn("data-center-x='", html)
        self.assertIn("text-align:center;font-weight:700", html)
        self.assertIn("e.dataset.centerX", html)

    def test_saved_coordinate_edits_stay_visible_over_the_original_scan(self):
        source_words = [PositionedWord("原标题", 60, 10, 80, 20)]
        source_cells = [[
            TableCell(0, 1, 0, 1, "原单元格", 0, 60, 90, 110),
            TableCell(0, 1, 1, 2, "待清空", 90, 60, 180, 110),
        ], [
            TableCell(0, 1, 0, 1, "第二张表的基线", 180, 60, 270, 110),
        ]]
        html = render_editor_html(
            OcrResult(
                image_width=300,
                image_height=300,
                positioned_words=[PositionedWord("新标题", 60, 10, 80, 20)],
                tables=[[["第二张表的基线", ""]], [["第二张表的基线"]]],
                table_cells=[[
                    TableCell(0, 1, 0, 1, "第二张表的基线", 0, 60, 90, 110),
                    TableCell(0, 1, 1, 2, "", 90, 60, 180, 110),
                ], [
                    TableCell(0, 1, 0, 1, "第二张表的基线", 180, 60, 270, 110),
                ]],
            ),
            "http://example.test/source-image",
            source_positioned_words=source_words,
            source_table_cells=source_cells,
        )

        self.assertIn(".coordinate-word[data-edited='1']", html)
        self.assertIn(".coordinate-cell[data-edited='1']", html)
        self.assertEqual(html.count("contenteditable='true' data-edited='1'"), 3)
        self.assertIn("data-empty='1'", html)

    def test_coordinate_editor_uses_device_width_and_fit_scaling(self):
        html = render_editor_html(
            OcrResult(
                image_width=1000,
                image_height=1400,
                tables=[[['标题']]],
                table_cells=[[TableCell(0, 1, 0, 1, '标题', 0, 0, 1000, 1400)]],
            ),
            "http://example.test/source-image",
        )
        self.assertIn("width=device-width", html)
        self.assertIn("data-source-width='1000.0'", html)
        self.assertIn("data-source-height='1400.0'", html)
        self.assertIn("function fitCoordinatePage", html)
        self.assertIn("const coordinateHeight=sourceHeight*794/sourceWidth", html)
        self.assertIn("page.style.height=(coordinateHeight*fit+2)+'px'", html)
        self.assertIn("canvas.style.transform='scale('+fit+')'", html)
        self.assertIn("overflow:auto;-webkit-overflow-scrolling:touch;touch-action:pan-x pan-y", html)


if __name__ == "__main__":
    unittest.main()
