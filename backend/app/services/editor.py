from __future__ import annotations

import re
from dataclasses import replace
from html import escape
from math import floor, isfinite
from pathlib import Path
from statistics import median

from PIL import Image

from app.services.ocr import (
    OcrResult,
    PositionedWord,
    TableCell,
    clean_ocr_text,
    normalize_table_cell_value,
    _join_positioned_ocr_line,
)


def render_editor_html(
    result: OcrResult,
    source_image_url: str | None = None,
    original_image_url: str | None = None,
    source_positioned_words: list[PositionedWord] | None = None,
    source_table_cells: list[list[TableCell]] | None = None,
    paper_colors: dict[str, str] | None = None,
    positioned_table_words: bool = False,
) -> str:
    """Render OCR structure as a Word-like, contenteditable document."""
    editable_count = _editable_character_count(result)
    original_image_url = original_image_url or source_image_url
    # Plain-prose OCR results have their source coordinates removed during
    # reflow, so they open as editable text. Keep coordinate mode for tables
    # and legacy geometry-only documents that still need visual correction.
    coordinate_mode = bool(source_image_url and (result.tables or result.positioned_words))
    paper_colors = paper_colors if coordinate_mode and isinstance(paper_colors, dict) else {}
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1,maximum-scale=3,user-scalable=yes'>",
        "<style>",
        "*{box-sizing:border-box}html{background:#f4f6fb}body{margin:0;overflow-x:auto;"
        "background:#f4f6fb;color:#111;font-family:Arial,'Microsoft YaHei',sans-serif;"
        "font-size:16px;padding:12px 10px 36px}",
        ".toolbar{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;overflow:visible;"
        "scrollbar-width:none;gap:6px;width:min(794px,100%);"
        "margin:0 auto 10px;padding:8px;background:#fff;border:1px solid #e3e7ef;"
        "border-radius:10px}",
        ".toolbar::-webkit-scrollbar{display:none}",
        ".edit-tools{display:flex;flex-wrap:wrap;align-items:center;gap:6px;min-width:0}",
        ".toolbar button{border:1px solid #d9deea;background:#fff;border-radius:7px;"
        "padding:7px 11px;color:#35415a;font-size:14px;white-space:nowrap}",
        ".toolbar .icon-button{width:38px;min-width:38px;padding:6px 0;font-size:21px;"
        "line-height:1;font-family:Arial,'Microsoft YaHei',sans-serif}",
        "@media(max-width:520px){.toolbar{justify-content:flex-start;}"
        ".edit-tools{width:100%;overflow:visible}"
        ".toolbar button{padding:6px 8px;font-size:13px}.toolbar .view-button{flex:1 1 110px;min-width:110px}"
        ".zoom-label{min-width:42px;padding:6px 0}}",
        ".zoom-label{display:inline-block;min-width:48px;padding:7px 0;text-align:center;"
        "color:#788399;font-size:13px;cursor:pointer}",
        ".page{width:794px;min-width:794px;max-width:794px;min-height:1123px;margin:0 auto;padding:40px 38px;"
        "background:#fff;border:1px solid #e1e5ed;border-radius:3px;"
        "box-shadow:0 1px 4px rgba(30,45,70,.08)}",
        ".original-page{display:none;width:794px;min-width:794px;max-width:794px;margin:0 auto;background:#fff;"
        "border:1px solid #e1e5ed;border-radius:3px;box-shadow:0 1px 4px rgba(30,45,70,.08);"
        "line-height:0;overflow:hidden}",
        ".original-page img{display:block;width:794px;max-width:none;height:auto;margin:0}",
        ".coordinate-page{display:none;width:min(794px,100%);margin:0 auto;background:#fff;border:1px solid #e1e5ed;"
        "border-radius:3px;box-shadow:0 1px 4px rgba(30,45,70,.08);overflow:auto;"
        "-webkit-overflow-scrolling:touch;touch-action:pan-x pan-y}",
        ".coordinate-canvas{position:relative;width:794px;line-height:0;transform-origin:top left}",
        ".coordinate-image{display:block;width:794px;height:auto;margin:0}",
        ".coordinate-layer{position:absolute;left:0;top:0;width:794px;height:100%}",
        ".coordinate-cell{position:absolute;z-index:1;display:flex;box-sizing:border-box;overflow:hidden;white-space:pre-wrap;word-break:break-all;"
        "align-items:center;justify-content:center;text-align:center;cursor:text;user-select:text;"
        "-webkit-user-select:text;caret-color:#4f46e5;"
        "background:transparent;color:transparent;line-height:1.1;padding:1px 2px;outline:none;"
        "box-shadow:none;"
        "font-family:Arial,'Microsoft YaHei',sans-serif}"
        ".coordinate-cell[data-empty='1']{background:var(--paper-color,#fff);color:transparent;box-shadow:none}"
        ".coordinate-cell[data-edited='1']{z-index:4;background:var(--paper-color,#fff);color:var(--ink-color,#111);overflow:auto}"
        ".coordinate-cell[data-align='left']{justify-content:flex-start;text-align:left}"
        ".coordinate-cell[data-align='right']{justify-content:flex-end;text-align:right}"
        ".coordinate-cell[data-valign='top']{align-items:flex-start}"
        ".coordinate-cell[data-valign='bottom']{align-items:flex-end}"
        ".coordinate-cell:focus,.coordinate-cell.edited{z-index:4;box-shadow:inset 0 0 0 2px #6875ee;"
        "background:var(--paper-color,#fff);color:var(--ink-color,#111);overflow:auto}"
        ".coordinate-word{position:absolute;z-index:2;display:block;overflow:hidden;white-space:pre-wrap;word-break:break-all;"
        "background:transparent;color:transparent;line-height:1.05;padding:0 2px;outline:none;"
        "box-shadow:none;"
        "cursor:text;user-select:text;-webkit-user-select:text;caret-color:#4f46e5;"
        "font-family:Arial,'Microsoft YaHei',sans-serif;text-align:left}",
        ".coordinate-word:focus,.coordinate-word.edited{z-index:3;box-shadow:inset 0 0 0 2px #6875ee;"
        "background:var(--paper-color,#fff);color:var(--ink-color,#111);overflow:auto}",
        ".coordinate-word[data-edited='1']{z-index:3;background:var(--paper-color,#fff);"
        "color:var(--ink-color,#111);overflow:auto}",
        ".coordinate-word.form-field{border-bottom:1px solid transparent;white-space:pre;overflow:auto;"
        "padding:0 2px 1px;z-index:4}",
        ".coordinate-word.form-field:focus,.coordinate-word.form-field.edited{"
        "border-bottom-color:#555;background:var(--paper-color,#fff);color:var(--ink-color,#111);"
        "box-shadow:inset 0 0 0 2px #6875ee}",
        "p[data-paragraph]{margin:4px 0 10px;min-height:25px;line-height:1.55;"
        "outline:none;font-size:16px;text-align:left}",
        "p.title{text-align:center;font-weight:700;font-size:22px;margin-top:0}",
        "p.subtitle{text-align:center;font-weight:700;font-size:18px;margin-bottom:8px}",
        ".table-document p.title,.table-document p.subtitle{font-weight:400}",
        ".positioned-paragraph{position:relative;width:794px;min-height:28px;margin:4px 0 10px -38px;"
        "outline:none;font-size:16px;line-height:1.35}",
        ".positioned-fragment{position:absolute;top:0;white-space:nowrap;min-height:24px;"
        "outline:none;line-height:1.35}",
        ".ocr-low-confidence{background:#fff0bd;text-decoration:underline dotted #b7791f;"
        "text-underline-offset:2px;border-radius:2px;color:inherit}",
        ".line-field{position:absolute;display:inline-block;box-sizing:border-box;height:20px;min-width:12px;"
        "padding:0 2px 1px;border:0;border-bottom:1px solid #555;outline:none;white-space:pre;"
        "line-height:18px;vertical-align:bottom;color:#111;caret-color:#4f46e5;cursor:text;"
        "overflow-x:auto;overflow-y:hidden;z-index:2}",
        ".line-field:focus,.positioned-fragment:focus{box-shadow:inset 0 0 0 2px #6875ee;"
        "background:#f7f8ff}",
        ".coordinate-field{position:absolute;display:block;box-sizing:border-box;z-index:4;overflow:auto;"
        "padding:0 2px 1px;border:0;border-bottom:1px solid transparent;outline:none;white-space:pre;"
        "line-height:1.25;color:transparent;background:transparent;caret-color:#4f46e5;cursor:text}",
        ".coordinate-field:focus,.coordinate-field.edited{border-bottom-color:#555;"
        "background:var(--paper-color,#fff);color:var(--ink-color,#111);"
        "box-shadow:inset 0 0 0 2px #6875ee}",
        "table{display:table;border-collapse:collapse;table-layout:fixed;margin:18px 0;"
        "font-size:11px}",
        "td{border:1px solid #555;padding:5px 4px;min-height:26px;vertical-align:middle;"
        "white-space:pre-wrap;word-break:break-word;line-height:1.35;outline:none;color:#111}",
        "td:focus,p:focus{box-shadow:inset 0 0 0 2px #6875ee;background:#f7f8ff}",
        ".tip{width:min(794px,100%);margin:0 auto 9px;color:#788399;font-size:12px}",
        "</style></head><body data-editor-version='38'>",
        "<div class='toolbar'><div class='edit-tools'><button class='icon-button' aria-label='撤销' title='撤销' onclick=cmd('undo')>↶</button>"
        "<button class='icon-button' aria-label='重做' title='重做' onclick=cmd('redo')>↷</button><button onclick=cmd('bold')><b>粗体</b></button>"
        "<button onclick=cmd('justifyCenter')>居中</button><button onclick=changeZoom(-0.1)>−</button>"
        "<span class='zoom-label' id='zoomLabel' onclick='resetZoom()' title='恢复100%'>100%</span>"
        "<button onclick=changeZoom(0.1)>＋</button></div>",
    f"<div class='tip'>点击文字或表格单元格可进行编辑 · {editable_count} 字符</div>",
    ]
    parts.insert(-1, "<button class='view-button' id='originalToggle' onclick=toggleOriginal()>原图预览</button>")
    if coordinate_mode:
        coordinate_label = "原图版式校对" if result.tables else "文字编辑"
        parts.insert(-1, f"<button class='view-button' id='coordinateToggle' onclick=toggleCoordinate()>{coordinate_label}</button>")
    parts.insert(-1, "</div>")
    if source_image_url:
        parts.append(
            f"<div class='original-page' id='originalPage'><img src='{escape(original_image_url or source_image_url, quote=True)}' "
            "alt='原始扫描页'></div>"
        )
        # Keep the original-coordinate page available for visual checking even
        # when the structured editable document is the default view.
        if coordinate_mode:
            parts.append(
                _coordinate_page_html(
                    result,
                    source_image_url,
                    source_positioned_words,
                    source_table_cells,
                    paper_colors,
                    positioned_table_words,
                )
            )
    # Table scans open as a real, editable document first. The coordinate
    # canvas remains available for checking each OCR box against the scan, but
    # it should not make the default page look like a low-resolution photo.
    page_style = " style='display:none'" if coordinate_mode and not result.tables else ""
    initial_view = "edit" if result.tables else "coordinate"
    page_class = "page table-document" if result.tables else "page"
    parts.append(f"<div class='{page_class}' id='page'{page_style}>")
    page_blocks: list[tuple[float, int, list[str]]] = []
    page_block_order = 0

    styles = list(result.paragraph_styles)
    if len(styles) < len(result.paragraphs):
        styles.extend(["body"] * (len(result.paragraphs) - len(styles)))
    layouts = list(result.paragraph_layouts)
    if len(layouts) < len(result.paragraphs):
        layouts.extend([None] * (len(result.paragraphs) - len(layouts)))
    for index, paragraph in enumerate(result.paragraphs):
        paragraph_parts: list[str] = []
        layout = layouts[index] if index < len(layouts) else None
        paragraph_style = styles[index] if index < len(styles) else "body"
        if not result.tables and paragraph_style in {"title", "subtitle"}:
            paragraph_style = "body"
        if (
            paragraph_style == "body"
            and result.tables
            and (
                layout is None
                or result.image_height <= 0
                or layout.top <= result.image_height * 0.3
            )
            and ("申请表" in paragraph or "比赛结果" in paragraph)
        ):
            paragraph_style = "title"
        title_class = " title" if paragraph_style == "title" else ""
        subtitle_class = " subtitle" if paragraph_style == "subtitle" else ""
        # Coordinate fragments are intended for form fields. In a prose-only
        # scan the same gaps are ordinary word spacing; rendering them as
        # positioned fields makes the editor look like a fill-in form.
        if result.tables and layout and layout.fragments and paragraph_style == "body":
            paragraph_parts.append(
                _positioned_paragraph_html(
                    layout,
                    paragraph,
                    index,
                    result.positioned_words,
                    result.low_confidence_examples,
                )
            )
        else:
            paragraph_parts.append(
                f"<p class='editable{title_class}{subtitle_class}' data-paragraph='1' contenteditable='true'>"
                f"{_html_text_with_low_confidence(paragraph, result.low_confidence_examples)}</p>"
            )
        paragraph_order = (
            float(layout.top)
            if result.tables and layout is not None
            else float("-inf")
        )
        page_blocks.append((paragraph_order, page_block_order, paragraph_parts))
        page_block_order += 1

    for table_index, table in enumerate(result.tables):
        table_parts: list[str] = []
        cells = result.table_cells[table_index] if table_index < len(result.table_cells) else []
        col_count = max((len(row) for row in table), default=0)
        layout = result.table_layouts[table_index] if table_index < len(result.table_layouts) else None
        widths = layout.column_widths if layout else []
        if len(widths) != col_count or not any(widths):
            widths = [1.0] * col_count
        else:
            positive_widths = [width for width in widths if width > 0]
            fallback_width = sum(positive_widths) / len(positive_widths) if positive_widths else 1.0
            widths = [width if width > 0 else fallback_width for width in widths]
        total_width = sum(widths) or float(max(col_count, 1))
        table_width, table_left = _source_table_box(layout)
        pixel_widths = [max(int(round(table_width * width / total_width)), 1) for width in widths]
        pixel_widths[-1] += table_width - sum(pixel_widths)
        table_parts.append(
            f"<table data-table='{table_index}' style='display:table;width:{table_width}px;min-width:{table_width}px;"
            f"max-width:{table_width}px;margin-left:{table_left}px;' "
            f"data-column-widths='{','.join(str(width) for width in pixel_widths)}'><colgroup>"
        )
        table_parts.extend(f"<col style='width:{width}px'>" for width in pixel_widths)
        table_parts.append("</colgroup><tbody>")
        if cells:
            starts = {(cell.row_start, cell.col_start): cell for cell in cells}
            covered = {
                (row, col)
                for cell in cells
                for row in range(cell.row_start, cell.row_end)
                for col in range(cell.col_start, cell.col_end)
                if (row, col) != (cell.row_start, cell.col_start)
            }
            row_count = len(table)
            for row in range(row_count):
                row_style = ""
                if layout and row < len(layout.row_heights):
                    source_width = max(sum(widths), 1.0)
                    row_height = max(int(round(table_width * layout.row_heights[row] / source_width)), 20)
                    row_style = f" style='height:{row_height}px'"
                table_parts.append(f"<tr{row_style}>")
                for col in range(col_count):
                    if (row, col) in covered:
                        continue
                    cell = starts.get((row, col))
                    rowspan = cell.row_end - cell.row_start if cell else 1
                    colspan = cell.col_end - cell.col_start if cell else 1
                    fallback_text = cell.text if cell else table[row][col]
                    cell_words = (
                        [word for _, word in _positioned_words_in_cell(cell, result.positioned_words)]
                        if cell
                        else []
                    )
                    value = _editor_cell_text(fallback_text, cell_words)
                    alignment = (
                        _coordinate_cell_alignment(cell, cell_words)
                        if cell and cell_words
                        else None
                    )
                    if "□" in value:
                        alignment = "left"
                    elif alignment is None:
                        alignment = "left" if len(value.replace("\n", "")) > 24 else "center"
                    vertical_alignment = (
                        _coordinate_cell_vertical_alignment(cell, cell_words)
                        if cell and cell_words
                        else None
                    )
                    cell_style = f"text-align:{alignment};"
                    if vertical_alignment in {"top", "bottom"}:
                        cell_style += f"vertical-align:{vertical_alignment};"
                    table_parts.append(
                        f"<td contenteditable='true' rowspan='{rowspan}' colspan='{colspan}' "
                        f"data-row='{row}' data-col='{col}' "
                        f"data-rowspan='{rowspan}' data-colspan='{colspan}' "
                        f"style='{cell_style}' "
                        f"data-source-text='{_html_attribute_text(value)}'>"
                        f"{_html_text_with_low_confidence(value, result.low_confidence_examples)}</td>"
                    )
                table_parts.append("</tr>")
        else:
            for row_index, row in enumerate(table):
                row_style = ""
                if layout and row_index < len(layout.row_heights):
                    source_width = max(sum(widths), 1.0)
                    row_height = max(int(round(table_width * layout.row_heights[row_index] / source_width)), 20)
                    row_style = f" style='height:{row_height}px'"
                table_parts.append(f"<tr{row_style}>")
                for column_index, value in enumerate(row):
                    value = _editor_cell_text(value, [])
                    table_parts.append(
                        f"<td contenteditable='true' rowspan='1' colspan='1' "
                        f"data-row='{row_index}' data-col='{column_index}' "
                        f"data-rowspan='1' data-colspan='1' "
                        "style='text-align:left;' "
                        f"data-source-text='{_html_attribute_text(value)}'>"
                        f"{_html_text_with_low_confidence(value, result.low_confidence_examples)}</td>"
                    )
                table_parts.append("</tr>")
        table_parts.append("</tbody></table>")
        valid_cells = [
            cell for cell in cells
            if cell.right > cell.left and cell.bottom > cell.top
        ]
        table_order = min((cell.top for cell in valid_cells), default=float("inf"))
        page_blocks.append((table_order, page_block_order, table_parts))
        page_block_order += 1

    for _order, _sequence, block_parts in sorted(page_blocks, key=lambda item: (item[0], item[1])):
        parts.extend(block_parts)

    parts.append(
        "</div><script>"
        "let editorInitialSnapshot='';"
        "document.querySelectorAll('[contenteditable=true]').forEach(e=>e.addEventListener('input',()=>{"
        "if(e.tagName==='TD')e.dataset.edited='1';"
        "if(e.hasAttribute('data-coordinate-field'))e.classList.add('edited');"
        "const dirty=collectDocument()!==editorInitialSnapshot;"
        "document.body.dataset.dirty=dirty?'1':'0';"
        "parent.postMessage({type:'iscan:editor-dirty',dirty},'*')}));"
        "document.querySelectorAll('[data-form-field-value]').forEach(e=>{"
        "e.addEventListener('beforeinput',event=>{if(event.inputType==='insertParagraph'||"
        "event.inputType==='insertLineBreak')event.preventDefault()});"
        "e.addEventListener('paste',event=>{event.preventDefault();"
        "const text=(event.clipboardData?.getData('text/plain')||'').replace(/[\\r\\n\\t]+/g,' ');"
        "if(text)document.execCommand('insertText',false,text)});});"
        "function fitPositionedWord(e){if(!e)return;const base=Number(e.dataset.baseWidth||e.offsetWidth||4);"
        "const baseLeft=Number(e.dataset.baseLeft||e.offsetLeft||0),baseTop=Number(e.dataset.baseTop||e.offsetTop||0),"
        "baseHeight=Number(e.dataset.baseHeight||e.offsetHeight||4),baseRight=baseLeft+base;"
        "const page=document.getElementById('coordinatePage'),sourceWidth=Math.max(Number(page?.dataset.sourceWidth||794),1),"
        "sourceHeight=Math.max(Number(page?.dataset.sourceHeight||1123),1),"
        "unitScale=794/sourceWidth,paddingFor=height=>Math.max(1,Math.min(3,Math.round(height/unitScale*.08)))*unitScale,"
        "padding=paddingFor(baseHeight);let leftLimit=padding,rightLimit=794-padding;"
        "const overlapsLine=(top,height,otherTop,otherHeight)=>{"
        "const overlap=Math.min(top+height,otherTop+otherHeight)-Math.max(top,otherTop);"
        "return overlap/Math.max(Math.min(height,otherHeight),1)>=.35};"
        "document.querySelectorAll('[data-positioned-word]').forEach(other=>{if(other===e)return;"
        "const otherBaseLeft=Number(other.dataset.baseLeft||other.offsetLeft||0),"
        "otherLeft=other.classList.contains('edited')?other.offsetLeft:otherBaseLeft,"
        "otherTop=Number(other.dataset.baseTop||other.offsetTop||0),"
        "otherHeight=Number(other.dataset.baseHeight||other.offsetHeight||0),"
        "otherWidth=other.classList.contains('edited')?other.offsetWidth:Number(other.dataset.baseWidth||other.offsetWidth||0),"
        "otherPadding=paddingFor(otherHeight),"
        "otherRight=otherLeft+otherWidth;"
        "if(!overlapsLine(baseTop,baseHeight,otherTop,otherHeight))return;"
        "if(otherLeft>=baseRight)rightLimit=Math.min(rightLimit,otherLeft-otherPadding-padding);"
        "else if(otherRight<=baseLeft)leftLimit=Math.max(leftLimit,otherRight+otherPadding+padding);"
        "else{leftLimit=Math.max(leftLimit,baseLeft);rightLimit=Math.min(rightLimit,baseRight)}});"
        "document.querySelectorAll('[data-coordinate-cell]').forEach(other=>{"
        "const otherLeft=parseFloat(other.style.left)||other.offsetLeft||0,"
        "otherTop=parseFloat(other.style.top)||other.offsetTop||0,"
        "otherWidth=parseFloat(other.style.width)||other.offsetWidth||0,"
        "otherHeight=parseFloat(other.style.height)||parseFloat(other.style.minHeight)||other.offsetHeight||0,otherRight=otherLeft+otherWidth;"
        "if(!overlapsLine(baseTop,baseHeight,otherTop,otherHeight))return;"
        "if(otherLeft>=baseRight)rightLimit=Math.min(rightLimit,otherLeft-padding);"
        "else if(otherRight<=baseLeft)leftLimit=Math.max(leftLimit,otherRight+padding);"
        "else if(otherLeft<=baseLeft&&otherRight>=baseRight){leftLimit=Math.max(leftLimit,otherLeft+padding);"
        "rightLimit=Math.min(rightLimit,otherRight-padding)}"
        "else{leftLimit=Math.max(leftLimit,baseLeft);rightLimit=Math.min(rightLimit,baseRight)}});"
        "const center=Number(e.dataset.centerX||baseLeft+base/2),"
        "centerAvailable=2*Math.min(center-leftLimit,rightLimit-center);"
        "const max=e.dataset.center==='1'?Math.max(base,centerAvailable):"
        "Math.max(base,rightLimit-baseLeft);"
        "e.style.whiteSpace='nowrap';e.style.width='max-content';"
        "const desired=Math.max(base,e.scrollWidth+6);const next=Math.min(desired,max);"
        "e.style.width=next+'px';if(e.dataset.center==='1'){const center=Number(e.dataset.centerX||0);"
        "const centeredLeft=center-next/2;e.style.left=(centerAvailable<base?baseLeft:"
        "Math.max(leftLimit,Math.min(rightLimit-next,centeredLeft)))+'px';}"
        "e.style.whiteSpace='pre-wrap';e.style.height='auto';"
        "const desiredHeight=Math.max(baseHeight,e.scrollHeight),baseBottom=baseTop+baseHeight,"
        "candidateLeft=e.offsetLeft,candidateRight=candidateLeft+next,verticalPadding=paddingFor(baseHeight);"
        "let bottomLimit=sourceHeight*unitScale-padding;"
        "document.querySelectorAll('[data-positioned-word]').forEach(other=>{if(other===e)return;"
        "const otherLeft=other.classList.contains('edited')?other.offsetLeft:Number(other.dataset.baseLeft||other.offsetLeft||0),"
        "otherWidth=other.classList.contains('edited')?other.offsetWidth:Number(other.dataset.baseWidth||other.offsetWidth||0),"
        "otherTop=Number(other.dataset.baseTop||other.offsetTop||0),"
        "otherHeight=Number(other.dataset.baseHeight||other.offsetHeight||0),otherBottom=otherTop+otherHeight;"
        "if(Math.min(candidateRight,otherLeft+otherWidth)<=Math.max(candidateLeft,otherLeft))return;"
        "const verticalOverlap=Math.min(baseBottom,otherBottom)-Math.max(baseTop,otherTop),"
        "sameLine=verticalOverlap/Math.max(Math.min(baseHeight,otherHeight),1)>=.35;"
        "if(!sameLine&&otherTop>baseTop)bottomLimit=Math.min(bottomLimit,otherTop-paddingFor(otherHeight)-verticalPadding);"
        "else if(!sameLine&&otherBottom>baseTop)bottomLimit=Math.min(bottomLimit,baseBottom)});"
        "document.querySelectorAll('[data-coordinate-cell]').forEach(cell=>{"
        "const cellLeft=parseFloat(cell.style.left)||cell.offsetLeft||0,cellTop=parseFloat(cell.style.top)||cell.offsetTop||0,"
        "cellWidth=parseFloat(cell.style.width)||cell.offsetWidth||0,cellHeight=parseFloat(cell.style.height)||parseFloat(cell.style.minHeight)||cell.offsetHeight||0,"
        "cellBottom=cellTop+cellHeight;"
        "if(Math.min(candidateRight,cellLeft+cellWidth)<=Math.max(candidateLeft,cellLeft))return;"
        "if(cellTop<=baseTop&&cellBottom>=baseBottom)bottomLimit=Math.min(bottomLimit,cellBottom-verticalPadding);"
        "else if(cellTop>=baseBottom)bottomLimit=Math.min(bottomLimit,cellTop-verticalPadding);"
        "else if(cellBottom>baseTop)bottomLimit=Math.min(bottomLimit,baseBottom)});"
        "const maxHeight=Math.max(baseHeight,bottomLimit-baseTop);"
        "e.style.height=Math.min(desiredHeight,maxHeight)+'px'}"
        "function fitCoordinateCell(e){if(!e)return;const base=Math.max(7,Number(e.dataset.baseFontSize||parseFloat(getComputedStyle(e).fontSize)||9)),"
        "min=Math.min(base,Number(e.dataset.minFontSize||7)),fits=()=>e.scrollHeight<=e.clientHeight+1&&e.scrollWidth<=e.clientWidth+1;"
        "e.style.fontSize=base+'px';if(!e.innerText.trim()||fits())return;let low=min,high=base;"
        "for(let i=0;i<8;i++){const mid=(low+high)/2;e.style.fontSize=mid+'px';if(fits())low=mid;else high=mid}"
        "e.style.fontSize=low+'px'}"
        "document.querySelectorAll('[data-positioned-word]').forEach(e=>{e.dataset.baseWidth=e.offsetWidth;"
        "e.dataset.baseLeft=e.offsetLeft;e.dataset.baseTop=e.offsetTop;e.dataset.baseHeight=e.offsetHeight;"
        "e.dataset.originalText=e.innerText;"
        "e.addEventListener('focus',()=>e.classList.add('active'));"
        "e.addEventListener('input',()=>{if(e.innerText!==e.dataset.originalText){e.classList.add('edited');"
        "fitPositionedWord(e)}else{e.classList.remove('edited');e.style.width=e.dataset.baseWidth+'px';e.style.height='';}});"
        "e.addEventListener('blur',()=>{e.classList.remove('active');"
        "if(e.innerText!==e.dataset.originalText){e.classList.add('edited');fitPositionedWord(e)}"
        "else{e.classList.remove('edited');e.style.width=e.dataset.baseWidth+'px';e.style.height='';"
        "if(e.dataset.center==='1')e.style.left=e.dataset.baseLeft+'px';}});});"
        "document.querySelectorAll('[data-coordinate-cell]').forEach(e=>{e.dataset.originalText=e.innerText;"
        "e.dataset.baseFontSize=parseFloat(e.style.fontSize)||9;if(e.dataset.edited==='1'){e.classList.add('edited');fitCoordinateCell(e)}"
        "e.addEventListener('focus',()=>fitCoordinateCell(e));"
        "e.addEventListener('input',()=>{if(e.innerText!==e.dataset.originalText){e.classList.add('edited');fitCoordinateCell(e)}"
        "else if(e.dataset.edited==='1')fitCoordinateCell(e);"
        "else{e.classList.remove('edited');e.style.fontSize=e.dataset.baseFontSize+'px';}});"
        "e.addEventListener('blur',()=>{if(e.innerText!==e.dataset.originalText){e.classList.add('edited');fitCoordinateCell(e)}"
        "else if(e.dataset.edited==='1')fitCoordinateCell(e);"
        "else{e.classList.remove('edited');e.style.fontSize=e.dataset.baseFontSize+'px';}});});"
        "function fitCoordinatePage(){const page=document.getElementById('coordinatePage'),canvas=page&&page.querySelector('.coordinate-canvas');"
        "if(!page||!canvas)return;const contentWidth=document.documentElement.clientWidth||document.body.clientWidth||window.innerWidth;"
        "const available=Math.max(180,Math.min(794,contentWidth-20)),fit=Math.min(1,available/794);"
        "const sourceWidth=Number(page.dataset.sourceWidth||794),sourceHeight=Number(page.dataset.sourceHeight||1123);"
        "const coordinateHeight=sourceHeight*794/sourceWidth;"
        "page.style.width=(794*fit)+'px';page.style.height=(coordinateHeight*fit+2)+'px';"
        "canvas.style.transform='scale('+fit+')';canvas.style.height=coordinateHeight+'px';}"
        "window.addEventListener('resize',()=>{fitEditPage();fitCoordinatePage()});"
        "function cmd(name){document.execCommand(name,false,null)}"
        "function formFieldKey(e){return [e.dataset.formLeft,e.dataset.formTop].map(v=>Math.round(Number(v||0))).join(':')}"
        "function syncFormFieldViews(){const fields=[...document.querySelectorAll('[data-form-field-value]')],groups=new Map();"
        "for(const e of fields){const key=formFieldKey(e);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(e)}"
        "for(const group of groups.values()){const active=document.activeElement,"
        "source=group.find(e=>e===active)||group.find(e=>e.getClientRects().length)||group[0],"
        "value=(source.innerText||'').replace(/\\n/g,'');for(const peer of group)if(peer!==source&&peer.textContent!==value)peer.textContent=value}}"
        "function collectDocument(){syncFormFieldViews();const page=document.getElementById('page');"
        "const paragraphs=[...(page?.querySelectorAll('[data-paragraph]')||[])].map(e=>{"
        "const pieces=[...e.querySelectorAll('[data-text-piece]')];"
        "return pieces.length?pieces.map(p=>(p.textContent||'').replace(/\\n/g,'' )).join(''):e.innerText});"
        "const coordinateScale=Math.max(794/Number(document.getElementById('coordinatePage')?.dataset.sourceWidth||794),.0001);"
        "const positioned_words=[...document.querySelectorAll('[data-positioned-word]')].map(e=>({"
        "source_index:e.dataset.wordIndex===undefined?null:Number(e.dataset.wordIndex),"
        "text_paragraph_index:e.dataset.textParagraphIndex===undefined?null:Number(e.dataset.textParagraphIndex),"
        "text:e.innerText,left:Number(e.dataset.left||0),top:Number(e.dataset.top||0),"
        "width:Number(e.dataset.width||0),rendered_width:e.classList.contains('edited')?"
        "Number.parseFloat(e.style.width||'')/coordinateScale:null,"
        "height:e.classList.contains('edited')?"
        "Number.parseFloat(e.style.height||'')/coordinateScale:"
        "Number(e.dataset.height||0),is_form_field:e.dataset.isFormField==='1'}));"
        "const formValues=new Map();document.querySelectorAll('[data-form-field-value]').forEach(e=>{"
        "if(!e.getClientRects().length)return;formValues.set(formFieldKey(e),e)});"
        "for(const e of formValues.values()){const left=Number(e.dataset.formLeft||0),top=Number(e.dataset.formTop||0),"
        "width=Number(e.dataset.formWidth||0),height=Number(e.dataset.formHeight||0),"
        "text=(e.innerText||'').replace(/\\n/g,''),existing=positioned_words.find(w=>w.is_form_field&&"
        "Math.abs(w.left-left)<3&&Math.abs(w.top-top)<3);"
        "if(existing){existing.text=text;existing.left=left;existing.top=top;existing.width=width;existing.height=height}"
        "else if(text.trim())positioned_words.push({source_index:null,text,left,top,width,height,"
        "rendered_width:null,is_form_field:true})}"
        "document.querySelectorAll('[data-coordinate-cell]').forEach(e=>{"
        "const table=document.querySelector(`table[data-table='${e.dataset.tableIndex}']`);"
        "const cell=table&&table.querySelector(`td[data-row='${e.dataset.row}'][data-col='${e.dataset.col}']`);"
        "const original=e.dataset.originalText||'';"
        "if(cell&&(e.classList.contains('edited')||e.innerText!==original)){cell.innerText=e.innerText;cell.dataset.edited='1';}});"
        "const tableData=[...(page?.querySelectorAll('table[data-table]')||[])].map(table=>{"
        "let max=0;const rows=[...table.rows];rows.forEach(row=>row.querySelectorAll('td').forEach(c=>"
        "max=Math.max(max,Number(c.dataset.col||0)+Number(c.dataset.colspan||1))));"
        "const grid=rows.map(()=>Array(max).fill('')),originals=rows.map(()=>Array(max).fill(''));"
        "rows.forEach((row,r)=>row.querySelectorAll('td').forEach(c=>{"
        "const start=Number(c.dataset.col||0),span=Number(c.dataset.colspan||1);"
        "for(let rr=r;rr<r+Number(c.dataset.rowspan||1);rr++)for(let cc=start;cc<start+span;cc++)"
        "if(grid[rr]){const isStart=rr===r&&cc===start,source=c.dataset.sourceText??c.innerText;"
        "grid[rr][cc]=isStart?(c.dataset.edited==='1'?c.innerText:source):'';"
        "originals[rr][cc]=isStart?source:'';}}));return {grid,originals}});"
        "const tables=tableData.map(item=>item.grid),table_originals=tableData.map(item=>item.originals);let paragraphIndex=0;"
        "const blocks=[...(page?.children||[])].flatMap(e=>{"
        "if(e.matches('table[data-table]'))return [{type:'table',index:Number(e.dataset.table)}];"
        "if(e.hasAttribute('data-paragraph'))return [{type:'paragraph',index:paragraphIndex++}];return []});"
        "return JSON.stringify({paragraphs,tables,table_originals,positioned_words,blocks})}"
        "window.addEventListener('message',e=>{if(e.source!==parent||!e.data||"
        "e.data.type!=='iscan:collect-document'||typeof e.data.requestId!=='string')return;"
        "parent.postMessage({type:'iscan:editor-document',requestId:e.data.requestId,"
        "payload:collectDocument()},'*')});"
        "editorInitialSnapshot=collectDocument();"
        "let zoom=1,fitZoom=1;function fitEditPage(){const page=document.getElementById('page');"
        "if(!page)return;const contentWidth=document.documentElement.clientWidth||document.body.clientWidth||window.innerWidth;"
        "fitZoom=Math.min(1,Math.max(.35,(contentWidth-20)/794));applyZoom()}"
        "function applyZoom(){const actualZoom=fitZoom*zoom;const page=document.getElementById('page'),original=document.getElementById('originalPage'),"
        "coordinate=document.getElementById('coordinatePage');if(page)page.style.zoom=actualZoom;if(original)original.style.zoom=actualZoom;"
        "if(coordinate)coordinate.style.zoom=zoom;document.getElementById('zoomLabel').innerText=Math.round(zoom*100)+'%'}"
        "function changeZoom(delta){zoom=Math.max(.75,Math.min(2.4,zoom+delta));applyZoom()}"
        "function resetZoom(){zoom=1;fitEditPage()}"
        "let returnView='edit';"
        "function toggleOriginal(){const original=document.getElementById('originalPage');"
        "if(!original)return;const showing=original.style.display==='block';"
        "setView(showing?returnView:'original')}"
        "function toggleCoordinate(){const coordinate=document.getElementById('coordinatePage');"
        "if(!coordinate)return;const showing=coordinate.style.display==='block';"
        "if(showing){setView('edit');return}setView('coordinate')}"
        "function setView(view){const page=document.getElementById('page'),original=document.getElementById('originalPage'),"
        "coordinate=document.getElementById('coordinatePage');if(view!=='original')returnView=view;"
        "if(page)page.style.display=view==='edit'?'block':'none';"
        "if(original)original.style.display=view==='original'?'block':'none';"
        "if(coordinate)coordinate.style.display=view==='coordinate'?'block':'none';"
        "const originalButton=document.getElementById('originalToggle'),coordinateButton=document.getElementById('coordinateToggle');"
        "if(originalButton)originalButton.innerText=view==='original'?'返回整页编辑':'原图预览';"
        "if(coordinateButton){const hasTables=Boolean(document.querySelector('table[data-table]'));"
        "coordinateButton.innerText=view==='coordinate'?(hasTables?'清晰结构文档':'文字文档'):'原图版式校对'}}"
        f"fitEditPage();if(document.getElementById('coordinatePage')){{setView('{initial_view}');fitCoordinatePage();}}"
        "</script></body></html>"
    )
    return "".join(parts)


def _editable_character_count(result: OcrResult) -> int:
    values = [clean_ocr_text(value) for value in result.paragraphs]
    for table in result.tables:
        for row in table:
            values.extend(clean_ocr_text(value) for value in row)
    return sum(len(value.replace("\n", "").replace("\r", "")) for value in values)


def _editor_cell_text(
    fallback: object,
    positioned_words: list[PositionedWord],
) -> str:
    return normalize_table_cell_value(fallback, positioned_words)


def _html_text(value: object) -> str:
    return escape(str(value or "")).replace("\n", "<br>")


def _html_text_with_low_confidence(value: object, examples: list[str]) -> str:
    """Highlight OCR snippets that the recognition provider marked uncertain."""

    text = str(value or "")
    matches: list[tuple[int, int]] = []
    ordered_examples = sorted(
        (str(example).strip() for example in examples if str(example).strip()),
        key=len,
        reverse=True,
    )
    for example in ordered_examples:
        # OCR warnings shorten long snippets with an ellipsis. Match the part
        # that was actually retained so it remains useful in the editor.
        needle = example[:-1].rstrip() if example.endswith("…") else example
        if not needle or not any(character.isalnum() for character in needle):
            continue
        pieces = re.split(r"\s+", needle)
        pattern = r"\s+".join(re.escape(piece) for piece in pieces if piece)
        if not pattern:
            continue
        if needle[0] in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_":
            pattern = rf"(?<![A-Za-z0-9_]){pattern}"
        if needle[-1] in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_":
            pattern = rf"{pattern}(?![A-Za-z0-9_])"
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            start, end = match.span()
            if not any(start < existing_end and end > existing_start for existing_start, existing_end in matches):
                matches.append((start, end))

    if not matches:
        return _html_text(text)

    matches.sort()
    parts: list[str] = []
    cursor = 0
    for start, end in matches:
        parts.append(_html_text(text[cursor:start]))
        parts.append(f"<span class='ocr-low-confidence'>{_html_text(text[start:end])}</span>")
        cursor = end
    parts.append(_html_text(text[cursor:]))
    return "".join(parts)


def _html_attribute_text(value: object) -> str:
    """Keep an exact cell snapshot in a safe HTML data attribute."""

    return escape(str(value or "").replace("\r", ""), quote=True).replace("\n", "&#10;")


def sample_source_paper_colors(
    source_image_path: Path,
    result: OcrResult,
) -> dict[str, str]:
    """Sample paper and ink colors so edited overlays blend into source scans."""
    if not source_image_path.is_file():
        return {}

    colors: dict[str, str] = {}
    try:
        with Image.open(source_image_path) as image:
            for index, word in enumerate(result.positioned_words):
                color = sample_paper_color(
                    image,
                    (word.left, word.top, word.left + word.width, word.top + word.height),
                )
                if color:
                    colors[f"word:{index}"] = color
                ink_color = sample_source_text_color(
                    image,
                    (word.left, word.top, word.left + word.width, word.top + word.height),
                )
                if ink_color:
                    colors[f"ink:word:{index}"] = ink_color
            for table_index, cells in enumerate(result.table_cells):
                for cell in cells:
                    color = sample_paper_color(
                        image,
                        (cell.left, cell.top, cell.right, cell.bottom),
                    )
                    if color:
                        colors[f"cell:{table_index}:{cell.row_start}:{cell.col_start}"] = color
                    if clean_ocr_text(cell.text):
                        # OCR cell bounds include grid rules; avoid sampling
                        # their dark strokes as the edited text color.
                        cell_width = max(0.0, cell.right - cell.left)
                        cell_height = max(0.0, cell.bottom - cell.top)
                        border_inset = max(
                            1.0,
                            min(4.0, min(cell_width, cell_height) * 0.025),
                        )
                        ink_color = sample_source_text_color(
                            image,
                            (
                                cell.left + border_inset,
                                cell.top + border_inset,
                                cell.right - border_inset,
                                cell.bottom - border_inset,
                            ),
                        )
                        if ink_color:
                            colors[
                                f"ink:cell:{table_index}:{cell.row_start}:{cell.col_start}"
                            ] = ink_color
    except (OSError, ValueError, TypeError):
        # The original page remains editable if a legacy source image cannot
        # be sampled; the editor CSS falls back to white for that document.
        return {}
    return colors


def sample_paper_color(
    image: Image.Image,
    box: tuple[float, float, float, float],
) -> str | None:
    left, top, right, bottom = (int(round(value)) for value in box)
    left = max(0, min(left, image.width))
    right = max(0, min(right, image.width))
    top = max(0, min(top, image.height))
    bottom = max(0, min(bottom, image.height))
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    padding = max(1, min(3, int(round(min(width, height) * 0.12))))
    inset_x = min(max(width // 10, 0), max((width - 1) // 3, 0))
    inset_y = min(max(height // 10, 0), max((height - 1) // 3, 0))
    sample_boxes = (
        (left + inset_x, max(0, top - padding), right - inset_x, top),
        (left + inset_x, bottom, right - inset_x, min(image.height, bottom + padding)),
        (max(0, left - padding), top + inset_y, left, bottom - inset_y),
        (right, top + inset_y, min(image.width, right + padding), bottom - inset_y),
    )
    channels: list[list[int]] = [[], [], []]
    for sample_box in sample_boxes:
        sample_width = sample_box[2] - sample_box[0]
        sample_height = sample_box[3] - sample_box[1]
        if sample_width <= 0 or sample_height <= 0:
            continue
        target_size = (min(sample_width, 24), min(sample_height, 2))
        crop = image.crop(sample_box)
        try:
            reduced = crop.resize(target_size, Image.Resampling.BOX)
            try:
                rgba = reduced.convert("RGBA")
                try:
                    pixels = rgba.load()
                    for y in range(rgba.height):
                        for x in range(rgba.width):
                            red, green, blue, alpha = pixels[x, y]
                            channels[0].append((red * alpha + 255 * (255 - alpha)) // 255)
                            channels[1].append((green * alpha + 255 * (255 - alpha)) // 255)
                            channels[2].append((blue * alpha + 255 * (255 - alpha)) // 255)
                finally:
                    rgba.close()
            finally:
                reduced.close()
        finally:
            crop.close()

    if not channels[0]:
        return None
    red, green, blue = (int(round(median(channel))) for channel in channels)
    return f"#{red:02x}{green:02x}{blue:02x}"


def sample_source_text_color(
    image: Image.Image,
    box: tuple[float, float, float, float],
) -> str | None:
    """Estimate the printed ink color from the darkest pixels in an OCR box."""
    left, top, right, bottom = (int(round(value)) for value in box)
    left = max(0, min(left, image.width))
    right = max(0, min(right, image.width))
    top = max(0, min(top, image.height))
    bottom = max(0, min(bottom, image.height))
    if right <= left or bottom <= top:
        return None

    width, height = right - left, bottom - top
    target_width, target_height = width, height
    if width * height > 4096:
        scale = (4096 / (width * height)) ** 0.5
        target_width = max(1, int(round(width * scale)))
        target_height = max(1, int(round(height * scale)))
        sample = image.transform(
            (target_width, target_height),
            Image.Transform.EXTENT,
            (left, top, right, bottom),
            resample=Image.Resampling.BILINEAR,
        )
    else:
        sample = image.crop((left, top, right, bottom))
    try:
        rgb = sample.convert("RGB")
        try:
            flattened = getattr(rgb, "get_flattened_data", None)
            pixel_data = flattened() if flattened else rgb.getdata()
            pixels = list(pixel_data)
        finally:
            rgb.close()
    finally:
        sample.close()
    if not pixels:
        return None

    def luminance(pixel: tuple[int, int, int]) -> float:
        red, green, blue = pixel
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    dark_pixels = [pixel for pixel in pixels if luminance(pixel) < 235]
    if not dark_pixels:
        return None
    dark_pixels.sort(key=luminance)
    sample_count = max(1, min(256, int(round(len(pixels) * 0.08))) or 1)
    sample = dark_pixels[:sample_count]
    red, green, blue = (
        int(round(median(channel)))
        for channel in zip(*sample)
    )
    if luminance((red, green, blue)) >= 245:
        return None
    return f"#{red:02x}{green:02x}{blue:02x}"


def _safe_paper_color(value: object) -> str:
    color = str(value or "")
    return color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else ""


def _source_table_box(layout) -> tuple[int, int]:
    """Map the table's source-image box into the fixed 794px editor page."""

    if (
        layout
        and layout.source_width > 0
        and layout.source_right > layout.source_left
    ):
        scale = 794.0 / layout.source_width
        width = max(int(round((layout.source_right - layout.source_left) * scale)), 1)
        # The page has 38px horizontal padding; a source x=0 table therefore
        # needs a negative content offset to line up with the page edge.
        left = int(round(layout.source_left * scale - 38))
        return width, left
    return 718, 0


def _paragraph_form_field_regions(
    layout,
    paragraph_text: str,
) -> list[tuple[float, float, float, float]]:
    text = clean_ocr_text(paragraph_text)
    header_markers = ("学校", "年级", "班别", "班级", "学（籍）号", "学(籍)号", "学籍号")
    if sum(marker in text for marker in header_markers) < 2:
        return []

    fragments = sorted(layout.fragments, key=lambda item: (item.top, item.left))
    if not fragments:
        return []
    regions: list[tuple[float, float, float, float]] = []

    def add_region(left: float, top: float, width: float, height: float) -> None:
        if width < 12 or height <= 0:
            return
        for index, (other_left, other_top, other_width, other_height) in enumerate(regions):
            same_line = abs(top - other_top) <= max(height, other_height) * 0.6
            touches = left <= other_left + other_width + 4 and other_left <= left + width + 4
            if same_line and touches:
                merged_left = min(left, other_left)
                merged_top = min(top, other_top)
                merged_right = max(left + width, other_left + other_width)
                merged_bottom = max(top + height, other_top + other_height)
                regions[index] = (
                    merged_left,
                    merged_top,
                    merged_right - merged_left,
                    merged_bottom - merged_top,
                )
                return
        regions.append((left, top, width, height))

    fillable_labels = {"学校", "年级", "班别", "班级", "学（籍）号", "学(籍)号", "学籍号"}
    for fragment in fragments:
        label = clean_ocr_text(fragment.text).replace(" ", "")
        if label not in fillable_labels or fragment.width <= 0 or fragment.height <= 0:
            continue
        glyph_width = sum(
            1.0 if ord(character) >= 0x2E80 else 0.55
            for character in label
        ) * float(fragment.height)
        field_left = float(fragment.left) + glyph_width
        field_width = float(fragment.left + fragment.width) - field_left
        if field_width >= max(float(fragment.height) * 1.5, 24.0):
            add_region(
                field_left,
                float(fragment.top) + float(fragment.height) * 0.08,
                field_width,
                float(fragment.height) * 1.2,
            )

    for previous, current in zip(fragments, fragments[1:]):
        previous_height = max(float(previous.height), 1.0)
        current_height = max(float(current.height), 1.0)
        previous_center = float(previous.top) + previous_height / 2.0
        current_center = float(current.top) + current_height / 2.0
        if abs(previous_center - current_center) > max(min(previous_height, current_height) * 0.7, 4.0):
            continue
        left = float(previous.left) + float(previous.width)
        width = float(current.left) - left
        if width < max(max(previous_height, current_height) * 1.3, 24.0):
            continue
        add_region(
            left,
            float(previous.top) + previous_height * 0.08,
            width,
            max(previous_height, current_height) * 1.2,
        )

    last_fragment = max(fragments, key=lambda item: item.left + item.width)
    trailing_width = float(layout.source_width or layout.right) - float(last_fragment.left + last_fragment.width)
    if "号" in text and trailing_width >= max(float(last_fragment.height) * 1.3, 24.0):
        add_region(
            float(last_fragment.left + last_fragment.width),
            float(last_fragment.top) + float(last_fragment.height) * 0.08,
            trailing_width,
            float(last_fragment.height) * 1.2,
        )
    return sorted(regions, key=lambda item: (item[1], item[0]))


def _positioned_paragraph_html(
    layout,
    paragraph_text: str,
    paragraph_index: int,
    positioned_words: list[PositionedWord],
    low_confidence_examples: list[str] | None = None,
) -> str:
    fragments = sorted(layout.fragments, key=lambda item: (item.top, item.left))
    source_width = max(float(layout.source_width or 0), float(layout.right or 0), 1.0)
    scale = 794.0 / source_width
    source_height = max(
        [float(fragment.height) for fragment in fragments] + [24.0]
    )
    height = max(28.0, source_height * scale * 1.7)
    parts = [
        f"<div class='positioned-paragraph' data-paragraph='1' style='height:{height:.1f}px'>"
    ]
    items: list[tuple[float, float, str]] = []
    for fragment in fragments:
        left = max(float(fragment.left) * scale, 0.0)
        top = max((float(fragment.top) - float(layout.top)) * scale, 0.0)
        width = max(float(fragment.width) * scale, 1.0)
        fragment_height = max(float(fragment.height) * scale, 18.0)
        items.append(
            (
                top,
                left,
                f"<span class='positioned-fragment' data-text-piece='1' contenteditable='true' "
                f"style='left:{left:.1f}px;top:{top:.1f}px;min-height:{fragment_height:.1f}px'>"
                f"{_html_text_with_low_confidence(fragment.text, low_confidence_examples or [])}</span>",
            )
        )

    for field_index, (field_left, field_top, field_width, field_height) in enumerate(
        _paragraph_form_field_regions(layout, paragraph_text)
    ):
        existing = next(
            (
                word
                for word in positioned_words
                if word.is_form_field
                and field_left - 4 <= word.left + word.width / 2 <= field_left + field_width + 4
                and field_top - field_height <= word.top + word.height / 2 <= field_top + field_height * 2
            ),
            None,
        )
        left = field_left * scale
        top = max((field_top - float(layout.top)) * scale, 0.0)
        width = max(field_width * scale, 12.0)
        height = max(field_height * scale, 18.0)
        form_text = clean_ocr_text(existing.text) if existing else ""
        form_markup = (
            f"<span class='line-field' data-text-piece='1' data-form-field-value='1' "
            f"data-form-left='{field_left:.2f}' data-form-top='{field_top:.2f}' "
            f"data-form-width='{field_width:.2f}' data-form-height='{field_height:.2f}' "
            f"data-paragraph-index='{paragraph_index}' data-field-index='{field_index}' "
            f"contenteditable='true' spellcheck='false' "
            f"style='left:{left:.1f}px;top:{top:.1f}px;width:{width:.1f}px;height:{height:.1f}px'>"
            f"{_html_text(form_text)}</span>"
        )
        items.append((top, left, form_markup))

    for _, _, markup in sorted(items, key=lambda item: (item[0], item[1])):
        parts.append(markup)
    parts.append("</div>")
    return "".join(parts)


def _positioned_words_in_cell(
    cell: TableCell,
    words: list[PositionedWord],
) -> list[tuple[int, PositionedWord]]:
    return [
        (index, word)
        for index, word in enumerate(words)
        if cell.left <= float(word.left) + float(word.width) / 2.0 <= cell.right
        and cell.top <= float(word.top) + float(word.height) / 2.0 <= cell.bottom
    ]


def _match_source_word_index(
    word: PositionedWord,
    source_words: list[PositionedWord],
    spatial_index: tuple[dict[tuple[int, int], list[int]], float] | None = None,
) -> int | None:
    """Match a current OCR word to its source box even if the list was reordered."""
    current_center_x = float(word.left) + float(word.width) / 2.0
    current_center_y = float(word.top) + float(word.height) / 2.0
    best_index: int | None = None
    best_score = float("inf")
    candidate_indices = (
        range(len(source_words))
        if spatial_index is None
        else _source_word_candidate_indices(word, source_words, spatial_index)
    )
    exact_left_match = next(
        (
            index
            for index in candidate_indices
            if abs(float(word.left) - float(source_words[index].left)) <= 1.5
            and abs(float(word.top) - float(source_words[index].top)) <= 1.5
        ),
        None,
    )
    if exact_left_match is not None:
        return exact_left_match
    exact_center_match = next(
        (
            index
            for index in candidate_indices
            if abs(current_center_x - (float(source_words[index].left) + float(source_words[index].width) / 2.0)) <= 1.5
            and abs(float(word.top) - float(source_words[index].top)) <= 1.5
        ),
        None,
    )
    if exact_center_match is not None:
        return exact_center_match
    for index in candidate_indices:
        source_word = source_words[index]
        source_center_x = float(source_word.left) + float(source_word.width) / 2.0
        source_center_y = float(source_word.top) + float(source_word.height) / 2.0
        horizontal_delta = min(
            abs(float(word.left) - float(source_word.left)),
            abs(current_center_x - source_center_x),
        )
        vertical_delta = abs(current_center_y - source_center_y)
        score = (
            horizontal_delta
            + vertical_delta * 3.0
            + abs(float(word.height) - float(source_word.height))
            + abs(float(word.width) - float(source_word.width)) * 0.15
        )
        threshold = max(24.0, max(float(word.height), float(source_word.height)) * 2.0)
        if score <= threshold and score < best_score:
            best_index = index
            best_score = score
    return best_index


_SOURCE_WORD_INDEX_CELL_SIZE = 32.0
_SOURCE_WORD_INDEX_MAX_QUERY_BUCKETS = 4096


def _build_source_word_spatial_index(
    source_words: list[PositionedWord],
) -> tuple[dict[tuple[int, int], list[int]], float]:
    """Index source left and center anchors by their vertical center."""

    index: dict[tuple[int, int], list[int]] = {}
    max_height = 0.0
    cell_size = _SOURCE_WORD_INDEX_CELL_SIZE
    for word_index, source_word in enumerate(source_words):
        left = float(source_word.left)
        top = float(source_word.top)
        width = float(source_word.width)
        height = float(source_word.height)
        center_x = left + width / 2.0
        center_y = top + height / 2.0
        if not all(isfinite(value) for value in (left, top, width, height, center_x, center_y)):
            continue
        max_height = max(max_height, height)
        y_bucket = floor(center_y / cell_size)
        for anchor_x in (left, center_x):
            key = (floor(anchor_x / cell_size), y_bucket)
            index.setdefault(key, []).append(word_index)
    return index, max_height


def _source_word_candidate_indices(
    word: PositionedWord,
    source_words: list[PositionedWord],
    spatial_index: tuple[dict[tuple[int, int], list[int]], float],
) -> list[int]:
    """Return every source box that could pass the legacy distance score."""

    current_left = float(word.left)
    current_top = float(word.top)
    current_width = float(word.width)
    current_height = float(word.height)
    current_center_x = current_left + current_width / 2.0
    current_center_y = current_top + current_height / 2.0
    if not all(
        isfinite(value)
        for value in (
            current_left,
            current_top,
            current_width,
            current_height,
            current_center_x,
            current_center_y,
        )
    ):
        return []

    index, max_source_height = spatial_index
    max_score = max(24.0, max(current_height, max_source_height) * 2.0)
    vertical_radius = max_score / 3.0
    cell_size = _SOURCE_WORD_INDEX_CELL_SIZE
    top_start = floor((current_center_y - vertical_radius) / cell_size)
    top_end = floor((current_center_y + vertical_radius) / cell_size)
    x_anchors = (current_left, current_center_x)
    query_bucket_count = sum(
        (
            floor((anchor_x + max_score) / cell_size)
            - floor((anchor_x - max_score) / cell_size)
            + 1
        )
        * (top_end - top_start + 1)
        for anchor_x in x_anchors
    )
    if query_bucket_count > _SOURCE_WORD_INDEX_MAX_QUERY_BUCKETS:
        return list(range(len(source_words)))

    candidates: set[int] = set()
    for anchor_x in x_anchors:
        left_start = floor((anchor_x - max_score) / cell_size)
        left_end = floor((anchor_x + max_score) / cell_size)
        for left_bucket in range(left_start, left_end + 1):
            for top_bucket in range(top_start, top_end + 1):
                candidates.update(index.get((left_bucket, top_bucket), ()))
    return sorted(candidates)


def _coordinate_page_html(
    result: OcrResult,
    source_image_url: str,
    source_positioned_words: list[PositionedWord] | None = None,
    source_table_cells: list[list[TableCell]] | None = None,
    paper_colors: dict[str, str] | None = None,
    positioned_table_words: bool = False,
) -> str:
    """Render the source page with editable OCR blocks at their source positions."""

    words = result.positioned_words
    word_paragraph_indexes = _positioned_word_paragraph_indexes(result)
    paper_colors = paper_colors or {}
    has_source_baseline = source_positioned_words is not None
    original_words = source_positioned_words if has_source_baseline else words
    source_word_index = _build_source_word_spatial_index(original_words)
    source_word_indexes = [
        (
            None
            if word.is_form_field
            else _match_source_word_index(word, original_words, source_word_index)
        )
        for word in words
    ]
    original_tables = source_table_cells if source_table_cells is not None else result.table_cells
    centered_texts = _centered_paragraph_texts(result)
    source_width = max(
        [float(result.image_width)]
        + [float(word.left + word.width) for word in words]
        + [
            float(layout.source_width)
            for layout in result.table_layouts
            if layout.source_width > 0
        ]
        + [
            float(cell.right)
            for cells in result.table_cells
            for cell in cells
            if cell.right > 0
        ]
        + [1.0]
    )
    source_height = max(
        [float(result.image_height)]
        + [float(word.top + word.height) for word in words]
        + [
            float(cell.bottom)
            for cells in result.table_cells
            for cell in cells
            if cell.bottom > 0
        ]
        + [1.0]
    )
    if result.image_height <= 0:
        source_height = max(source_height, source_width * 1.414)
    scale = 794.0 / source_width
    parts = [
        f"<div class='coordinate-page' id='coordinatePage' data-source-width='{source_width:.1f}' data-source-height='{source_height:.1f}' style='display:block'>",
        f"<div class='coordinate-canvas' style='height:{source_height * 794.0 / source_width:.1f}px'>",
        f"<img class='coordinate-image' src='{escape(source_image_url, quote=True)}' alt='原始扫描页'>",
        "<div class='coordinate-layer'>",
    ]
    table_word_indices: set[int] = set()
    table_word_indexes: set[int] = set()
    for table_index, cells in enumerate(result.table_cells):
        original_cells = original_tables[table_index] if table_index < len(original_tables) else cells
        original_cells_by_start = {
            (cell.row_start, cell.col_start): cell
            for cell in original_cells
        }
        for cell in cells:
            if cell.right <= cell.left or cell.bottom <= cell.top:
                continue
            left = max(float(cell.left) * scale + 1.0, 0.0)
            top = max(float(cell.top) * scale + 1.0, 0.0)
            width = max(float(cell.right - cell.left) * scale - 2.0, 8.0)
            height = max(float(cell.bottom - cell.top) * scale - 2.0, 12.0)
            words_in_cell = _positioned_words_in_cell(cell, words)
            cell_text = clean_ocr_text(cell.text)
            if not cell_text and words_in_cell:
                cell_text = _join_cell_words(words_in_cell)
            table_word_indexes.update(index for index, _ in words_in_cell)
            if positioned_table_words and words_in_cell:
                continue
            table_word_indices.update(index for index, _ in words_in_cell)
            original_cell = original_cells_by_start.get((cell.row_start, cell.col_start))
            original_cell_text = clean_ocr_text(original_cell.text) if original_cell else ""
            style_cell = original_cell or cell
            original_words_in_cell = _positioned_words_in_cell(style_cell, original_words)
            if not original_cell_text and original_words_in_cell:
                original_cell_text = _join_cell_words(original_words_in_cell)
            original_display_text = _editor_cell_text(
                original_cell_text,
                [word for _, word in original_words_in_cell],
            )
            edited_attribute = (
                " data-edited='1'"
                if clean_ocr_text(cell_text) != clean_ocr_text(original_display_text)
                else ""
            )
            empty_attribute = " data-empty='1'" if not cell_text else ""
            style_words = [word for _, word in (original_words_in_cell or words_in_cell)]
            alignment = _coordinate_cell_alignment(style_cell, style_words)
            if "□" in cell_text:
                alignment = "left"
            elif alignment is None:
                alignment = "left" if len(cell_text.replace("\n", "")) > 24 else "center"
            alignment_attribute = f" data-align='{alignment}'"
            vertical_alignment = _coordinate_cell_vertical_alignment(style_cell, style_words)
            vertical_attribute = (
                f" data-valign='{vertical_alignment}'"
                if vertical_alignment in {"top", "bottom"}
                else ""
            )
            style_text = original_cell_text or cell_text
            source_word_heights = [float(word.height) * scale for word in style_words]
            font_size = _cell_font_size(style_text, width, height, source_word_heights)
            paper_color = _safe_paper_color(
                paper_colors.get(f"cell:{table_index}:{cell.row_start}:{cell.col_start}"),
            )
            paper_style = f";--paper-color:{paper_color}" if paper_color else ""
            ink_color = _safe_paper_color(
                paper_colors.get(f"ink:cell:{table_index}:{cell.row_start}:{cell.col_start}"),
            )
            ink_style = f";--ink-color:{ink_color}" if ink_color else ""
            parts.append(
                f"<span class='coordinate-cell' data-coordinate-cell='1' "
                f"data-table-index='{table_index}' data-row='{cell.row_start}' data-col='{cell.col_start}' "
                f"data-base-font-size='{font_size:.1f}' data-min-font-size='7' "
                f"contenteditable='true'{edited_attribute} style='left:{left:.1f}px;top:{top:.1f}px;width:{width:.1f}px;"
                f"height:{height:.1f}px;min-height:{height:.1f}px;font-size:{font_size:.1f}px;{paper_style}{ink_style}'"
                f"{empty_attribute}{alignment_attribute}{vertical_attribute}>"
                f"{_html_text_with_low_confidence(cell_text, result.low_confidence_examples)}</span>"
            )

    paragraph_layouts = list(result.paragraph_layouts)
    for paragraph_index, layout in enumerate(paragraph_layouts):
        if layout is None or not layout.fragments or paragraph_index >= len(result.paragraphs):
            continue
        paragraph_text = result.paragraphs[paragraph_index]
        for field_index, (field_left, field_top, field_width, field_height) in enumerate(
            _paragraph_form_field_regions(layout, paragraph_text)
        ):
            existing_field = next(
                (
                    word
                    for word in words
                    if word.is_form_field
                    and word.text.strip()
                    and field_left - 4 <= word.left + word.width / 2 <= field_left + field_width + 4
                    and field_top - field_height <= word.top + word.height / 2 <= field_top + field_height * 2
                ),
                None,
            )
            if existing_field is not None:
                continue
            left = max(field_left * scale, 0.0)
            top = max(field_top * scale, 0.0)
            width = max(field_width * scale, 8.0)
            height = max(field_height * scale, 8.0)
            font_size = max(7.0, height * 0.82)
            paper_color = _safe_paper_color(
                paper_colors.get(f"form:{paragraph_index}:{field_index}"),
            )
            ink_color = _safe_paper_color(
                paper_colors.get(f"ink:form:{paragraph_index}:{field_index}"),
            )
            paper_style = f"--paper-color:{paper_color};" if paper_color else ""
            ink_style = f"--ink-color:{ink_color};" if ink_color else ""
            parts.append(
                f"<span class='coordinate-field' data-coordinate-field='1' "
                f"data-form-field-value='1' data-paragraph-index='{paragraph_index}' "
                f"data-field-index='{field_index}' data-form-left='{field_left:.2f}' "
                f"data-form-top='{field_top:.2f}' data-form-width='{field_width:.2f}' "
                f"data-form-height='{field_height:.2f}' contenteditable='true' spellcheck='false' "
                f"style='left:{left:.1f}px;top:{top:.1f}px;width:{width:.1f}px;height:{height:.1f}px;"
                f"font-size:{font_size:.1f}px;{paper_style}{ink_style}'></span>"
            )
    for index, word in enumerate(words):
        if index in table_word_indices:
            continue
        word_text = clean_ocr_text(word.text)
        if word.is_form_field and not word_text:
            continue
        left = max(float(word.left) * scale, 0.0)
        top = max(float(word.top) * scale, 0.0)
        width = max(float(word.width) * scale, 4.0)
        height = max(float(word.height) * scale, 7.0)
        font_size = max(7.0, height * 0.82)
        source_word_index = source_word_indexes[index]
        original_word = (
            original_words[source_word_index]
            if source_word_index is not None
            else None
        )
        edited_attribute = (
            " data-edited='1'"
            if (
                original_word is None and has_source_baseline
            ) or (
                original_word is not None
                and word_text != clean_ocr_text(original_word.text)
            )
            else ""
        )
        is_centered_heading = bool(word_text and word_text in centered_texts)
        source_word = original_word or word
        centered_table_cell = (
            _coordinate_centered_table_cell(source_word, original_tables)
            if positioned_table_words
            else None
        )
        is_centered = is_centered_heading or centered_table_cell is not None
        center_x = (
            (float(centered_table_cell.left) + float(centered_table_cell.right))
            * scale
            / 2.0
            if centered_table_cell is not None
            else left + width / 2.0
        )
        centered_attributes = (
            " data-center='1' "
            f"data-center-x='{center_x:.1f}'"
            if is_centered
            else ""
        )
        paper_color_index = source_word_index if source_word_index is not None else index
        paper_color = _safe_paper_color(
            paper_colors.get(f"word:{paper_color_index}"),
        )
        ink_color = _safe_paper_color(
            paper_colors.get(f"ink:word:{paper_color_index}"),
        )
        paper_style = f"--paper-color:{paper_color};" if paper_color else ""
        ink_style = f"--ink-color:{ink_color};" if ink_color else ""
        form_attributes = (
            f" data-is-form-field='1' data-form-field-value='1' data-form-left='{word.left:.2f}' "
            f"data-form-top='{word.top:.2f}' data-form-width='{word.width:.2f}' "
            f"data-form-height='{word.height:.2f}'"
            if word.is_form_field
            else ""
        )
        paragraph_index = (
            None if index in table_word_indexes else word_paragraph_indexes[index]
        )
        paragraph_attribute = (
            f" data-text-paragraph-index='{paragraph_index}'"
            if paragraph_index is not None
            else ""
        )
        word_class = "coordinate-word form-field" if word.is_form_field else "coordinate-word"
        word_markup = _html_text_with_low_confidence(
            word.text,
            result.low_confidence_examples,
        )
        parts.append(
            f"<span class='{word_class}' data-positioned-word='1' data-word-index='{index}' "
            f"data-left='{word.left:.2f}' data-top='{word.top:.2f}' data-width='{word.width:.2f}' "
            f"data-height='{word.height:.2f}'{paragraph_attribute}{form_attributes} "
            f"contenteditable='true'{edited_attribute} "
            f"{centered_attributes} "
            f"style='left:{left:.1f}px;top:{top:.1f}px;width:{width:.1f}px;min-height:{height:.1f}px;"
            f"font-size:{font_size:.1f}px;{paper_style}{ink_style}"
            f"{'text-align:center;' if is_centered else ''}"
            f"{'font-weight:700;' if is_centered_heading else ''}'>"
            f"{word_markup}</span>"
        )
    parts.extend(["</div></div></div>"])
    return "".join(parts)


def _positioned_word_paragraph_indexes(
    result: OcrResult,
) -> list[int | None]:
    """Associate coordinate words with their OCR paragraph using source geometry."""

    bucket_height = 160.0
    entries: dict[int, tuple[int, float, float, float, float]] = {}
    buckets: dict[int, list[int]] = {}
    overflow: list[int] = []
    for paragraph_index, layout in enumerate(result.paragraph_layouts):
        if layout is None or paragraph_index >= len(result.paragraphs):
            continue
        bounds = (layout.left, layout.top, layout.right, layout.bottom)
        if not all(isfinite(float(value)) for value in bounds):
            continue
        left, top, right, bottom = map(float, bounds)
        if right <= left or bottom <= top:
            fragment_bounds = [
                fragment
                for fragment in layout.fragments
                if all(isfinite(float(value)) for value in (
                    fragment.left,
                    fragment.top,
                    fragment.width,
                    fragment.height,
                ))
                and fragment.width > 0
                and fragment.height > 0
            ]
            if not fragment_bounds:
                continue
            left = min(float(fragment.left) for fragment in fragment_bounds)
            top = min(float(fragment.top) for fragment in fragment_bounds)
            right = max(float(fragment.left + fragment.width) for fragment in fragment_bounds)
            bottom = max(float(fragment.top + fragment.height) for fragment in fragment_bounds)
        entries[paragraph_index] = (paragraph_index, left, top, right, bottom)
        first_bucket = floor((top - 64.0) / bucket_height)
        last_bucket = floor((bottom + 64.0) / bucket_height)
        if last_bucket - first_bucket > 64:
            overflow.append(paragraph_index)
            continue
        for bucket in range(first_bucket, last_bucket + 1):
            buckets.setdefault(bucket, []).append(paragraph_index)

    word_indexes: list[int | None] = []
    for word in result.positioned_words:
        word_top = float(word.top)
        word_bottom = word_top + max(float(word.height), 1.0)
        word_left = float(word.left)
        word_right = word_left + max(float(word.width), 1.0)
        word_center_y = (word_top + word_bottom) / 2.0
        tolerance_y = max(float(word.height) * 0.75, 12.0)
        first_bucket = floor((word_top - tolerance_y) / bucket_height)
        last_bucket = floor((word_bottom + tolerance_y) / bucket_height)
        candidates = set(overflow)
        if last_bucket - first_bucket > 16:
            candidates.update(entries)
        else:
            for bucket in range(first_bucket, last_bucket + 1):
                candidates.update(buckets.get(bucket, ()))
        if len(candidates) > 256:
            word_indexes.append(None)
            continue

        word_center_x = (word_left + word_right) / 2.0
        best_index: int | None = None
        best_score = float("inf")
        horizontal_tolerance = max(float(word.height) * 1.5, 24.0)
        for paragraph_index in candidates:
            _index, left, top, right, bottom = entries[paragraph_index]
            vertical_gap = max(top - word_bottom, word_top - bottom, 0.0)
            horizontal_gap = max(left - word_center_x, word_center_x - right, 0.0)
            if vertical_gap > tolerance_y or horizontal_gap > horizontal_tolerance:
                continue
            score = vertical_gap * 3.0 + horizontal_gap
            if score < best_score:
                best_score = score
                best_index = paragraph_index
        word_indexes.append(best_index)
    return word_indexes


def recover_source_table_cells(
    table_cells: list[list[TableCell]],
    source_words: list[PositionedWord],
    current_words: list[PositionedWord],
) -> list[list[TableCell]]:
    """Recover edited legacy cell baselines from original positioned OCR words."""

    if not source_words or not current_words or source_words == current_words:
        return table_cells

    recovered_tables: list[list[TableCell]] = []
    for cells in table_cells:
        recovered_cells = []
        for cell in cells:
            original_text = _join_cell_words(_positioned_words_in_cell(cell, source_words))
            current_text = _join_cell_words(_positioned_words_in_cell(cell, current_words))
            recovered_cells.append(
                replace(cell, text=original_text)
                if original_text and original_text != current_text
                else cell
            )
        recovered_tables.append(recovered_cells)
    return recovered_tables


def _join_cell_words(words: list[tuple[int, PositionedWord]]) -> str:
    """Recover readable cell lines when table OCR omitted cell text."""

    if not words:
        return ""
    ordered = sorted(words, key=lambda item: (item[1].top, item[1].left))
    lines: list[list[PositionedWord]] = []
    line_tops: list[float] = []
    for _, word in ordered:
        tolerance = max(float(word.height) * 0.55, 3.0)
        line_index = next(
            (index for index, top in enumerate(line_tops) if abs(float(word.top) - top) <= tolerance),
            None,
        )
        if line_index is None:
            line_tops.append(float(word.top))
            lines.append([word])
        else:
            lines[line_index].append(word)
    return "\n".join(
        _join_positioned_cell_line(line_words)
        for _, line_words in sorted(zip(line_tops, lines), key=lambda item: item[0])
    ).strip()


def _join_positioned_cell_line(words: list[PositionedWord]) -> str:
    return _join_positioned_ocr_line(
        [
            (
                word.text,
                {
                    "left": word.left,
                    "top": word.top,
                    "width": word.width,
                    "height": word.height,
                },
            )
            for word in sorted(words, key=lambda item: item.left)
        ]
    )


def _coordinate_cell_alignment(
    cell: TableCell,
    words: list[PositionedWord],
) -> str | None:
    """Infer horizontal alignment from the original OCR boxes, not text length."""

    width = float(cell.right) - float(cell.left)
    if width <= 0 or not words:
        return None

    lines: list[list[PositionedWord]] = []
    line_centers: list[float] = []
    for word in sorted(words, key=lambda item: (item.top, item.left)):
        center_y = float(word.top) + float(word.height) / 2.0
        tolerance = max(min(float(word.height), 16.0) * 0.55, 2.0)
        line_index = next(
            (
                index
                for index, center in enumerate(line_centers)
                if abs(center_y - center) <= tolerance
            ),
            None,
        )
        if line_index is None:
            lines.append([word])
            line_centers.append(center_y)
        else:
            line = lines[line_index]
            line.append(word)
            line_centers[line_index] = sum(
                float(item.top) + float(item.height) / 2.0 for item in line
            ) / len(line)

    left_margins: list[float] = []
    right_margins: list[float] = []
    for line in lines:
        line_left = min(float(word.left) for word in line)
        line_right = max(float(word.left) + float(word.width) for word in line)
        left_margins.append(max(line_left - float(cell.left), 0.0))
        right_margins.append(max(float(cell.right) - line_right, 0.0))
    left_margin = median(left_margins)
    right_margin = median(right_margins)
    tolerance = max(width * 0.12, 2.0)
    if abs(left_margin - right_margin) <= tolerance:
        return "center"
    if left_margin < right_margin and left_margin <= width * 0.28:
        return "left"
    if right_margin < left_margin and right_margin <= width * 0.28:
        return "right"
    return "left"


def _coordinate_centered_table_cell(
    word: PositionedWord,
    tables: list[list[TableCell]],
) -> TableCell | None:
    """Find a table cell whose OCR word is centered by its source margins."""

    center_x = float(word.left) + float(word.width) / 2.0
    center_y = float(word.top) + float(word.height) / 2.0
    for cells in tables:
        for cell in cells:
            cell_width = float(cell.right) - float(cell.left)
            if cell_width <= 0 or cell.bottom <= cell.top:
                continue
            if not (
                cell.left <= center_x <= cell.right
                and cell.top <= center_y <= cell.bottom
            ):
                continue
            if float(word.width) > cell_width * 1.05:
                return None
            left_margin = float(word.left) - float(cell.left)
            right_margin = float(cell.right) - (float(word.left) + float(word.width))
            if abs(left_margin - right_margin) <= max(8.0, cell_width * 0.15):
                return cell
            return None
    return None


def _coordinate_cell_vertical_alignment(
    cell: TableCell,
    words: list[PositionedWord],
) -> str | None:
    """Infer top/bottom alignment only when the source text clearly indicates it."""

    height = float(cell.bottom) - float(cell.top)
    if height <= 0 or not words:
        return None
    top_margin = max(min(float(word.top) for word in words) - float(cell.top), 0.0)
    bottom_margin = max(
        float(cell.bottom)
        - max(float(word.top) + float(word.height) for word in words),
        0.0,
    )
    if top_margin <= height * 0.10 and bottom_margin >= height * 0.20:
        return "top"
    if bottom_margin <= height * 0.10 and top_margin >= height * 0.20:
        return "bottom"
    return "center"


def _cell_font_size(
    text: str,
    width: float,
    height: float,
    source_word_heights: list[float] | None = None,
) -> float:
    """Estimate source typography and leave edited text to fit inside the fixed cell."""

    lines = text.splitlines() or [""]
    longest_line = max((len(line) for line in lines), default=1)
    line_count = max(len(lines), 1)
    by_height = max(7.0, (height - 4.0) * 0.84 / line_count)
    valid_heights = [float(value) for value in (source_word_heights or []) if value > 0]
    if valid_heights:
        source_size = median(valid_heights) * 1.08
        return max(7.0, min(22.0, by_height, source_size))

    # When OCR has no word boxes, estimate the fallback from the text itself.
    by_width = max(7.0, width * 0.90 / max(longest_line, 1))
    return max(7.0, min(22.0, by_height, by_width))


def _centered_paragraph_texts(result: OcrResult) -> set[str]:
    styles = list(result.paragraph_styles)
    if len(styles) < len(result.paragraphs):
        styles.extend(["body"] * (len(result.paragraphs) - len(styles)))
    return {
        clean_ocr_text(text)
        for text, style in zip(result.paragraphs, styles)
        if style in {"title", "subtitle"} and clean_ocr_text(text)
    }
