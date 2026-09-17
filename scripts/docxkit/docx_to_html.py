#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Превращает .docx в HTML, максимально близкий по геометрии к тому, что покажет Word.

Нужен не для красоты, а для проверки вёрстки: страница А4 с настоящими полями,
колонки таблиц в тех же миллиметрах, шрифты того же кегля. Если текст переполняет
ячейку здесь — переполнит и в Word.

Оговорка: в системе стоит только DejaVu Sans, он на ~10 % шире Calibri, поэтому
превью пессимистично. Что влезло здесь — влезет и в Word.
"""
import sys, html
from docx import Document
from docx.oxml.ns import qn

EMU_MM = 36000.0
TWIP_MM = 56.7

def _mm(emu): return (emu or 0) / EMU_MM

def _rpr(run):
    st = []
    if run.font.size: st.append(f'font-size:{run.font.size.pt}pt')
    if run.bold: st.append('font-weight:700')
    if run.italic: st.append('font-style:italic')
    c = run.font.color
    if c is not None and c.rgb is not None: st.append(f'color:#{c.rgb}')
    return ';'.join(st)

def _ppr(par):
    pf = par.paragraph_format
    st = []
    if pf.space_before is not None: st.append(f'margin-top:{pf.space_before.pt}pt')
    if pf.space_after  is not None: st.append(f'margin-bottom:{pf.space_after.pt}pt')
    if pf.line_spacing: st.append(f'line-height:{pf.line_spacing}')
    al = str(par.alignment or '')
    if 'RIGHT' in al: st.append('text-align:right')
    elif 'CENTER' in al: st.append('text-align:center')
    return ';'.join(st)

def par_html(par):
    inner = ''.join(f'<span style="{_rpr(r)}">{html.escape(r.text)}</span>' for r in par.runs)
    if not inner: inner = '&nbsp;'
    brk = 'page-break-before:always;' if par.paragraph_format.page_break_before else ''
    # горизонтальная линейка, сделанная рамкой абзаца
    pbdr = par._p.find(qn('w:pPr'))
    rule = ''
    if pbdr is not None and pbdr.find(qn('w:pBdr')) is not None:
        rule = 'border-bottom:1px solid #dce3ea;'
    return f'<p style="{brk}{rule}{_ppr(par)}">{inner}</p>'

def _shade(tc):
    tcPr = tc.tcPr
    if tcPr is None: return ''
    shd = tcPr.find(qn('w:shd'))
    if shd is None: return ''
    fill = shd.get(qn('w:fill'))
    return f'background:#{fill};' if fill and fill != 'auto' else ''

def table_html(t):
    grid = t._tbl.find(qn('w:tblGrid'))
    widths = [int(g.get(qn('w:w'))) / TWIP_MM for g in grid] if grid is not None else []
    cols = ''.join(f'<col style="width:{w:.2f}mm">' for w in widths)
    rows = []
    for row in t.rows:
        tcs = row._tr.findall(qn('w:tc'))
        cells = []
        for tc in tcs:
            span = tc.tcPr.find(qn('w:gridSpan')) if tc.tcPr is not None else None
            n = int(span.get(qn('w:val'))) if span is not None else 1
            from docx.table import _Cell
            cell = _Cell(tc, t)
            body = ''.join(par_html(p) for p in cell.paragraphs)
            cells.append(f'<td colspan="{n}" style="{_shade(tc)}">{body}</td>')
        rows.append('<tr>' + ''.join(cells) + '</tr>')
    return (f'<table style="width:{sum(widths):.2f}mm">{cols}' + ''.join(rows) + '</table>')

def convert(path, out_html):
    d = Document(path)
    sec = d.sections[0]
    pw, ph = _mm(sec.page_width), _mm(sec.page_height)
    ml, mr = _mm(sec.left_margin), _mm(sec.right_margin)
    mt, mb = _mm(sec.top_margin), _mm(sec.bottom_margin)

    body = []
    for child in d.element.body.iterchildren():
        tag = child.tag.split('}')[1]
        if tag == 'p':
            from docx.text.paragraph import Paragraph
            body.append(par_html(Paragraph(child, d)))
        elif tag == 'tbl':
            from docx.table import Table
            body.append(table_html(Table(child, d)))

    css = f"""
    @page {{ size: {pw:.1f}mm {ph:.1f}mm; margin: 0; }}
    body {{ margin:0; background:#8a94a0; font-family:'DejaVu Sans',sans-serif; font-size:11pt; color:#1a222e; }}
    .page {{ width:{pw:.1f}mm; min-height:{ph:.1f}mm; padding:{mt:.1f}mm {mr:.1f}mm {mb:.1f}mm {ml:.1f}mm;
             background:#fff; margin:0 auto 8mm; box-sizing:border-box; }}
    p {{ margin:0; }}
    table {{ border-collapse:collapse; table-layout:fixed; margin:2mm 0; }}
    td {{ border:0.5pt solid #000; vertical-align:top; padding:1.06mm 2.29mm; word-wrap:break-word; }}
    """
    doc = (f'<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head>'
           f'<body><div class="page">' + ''.join(body) + '</div></body></html>')
    open(out_html, 'w', encoding='utf-8').write(doc)
    return out_html

if __name__ == '__main__':
    print(convert(sys.argv[1], sys.argv[2]))
