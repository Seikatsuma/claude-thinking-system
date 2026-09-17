# -*- coding: utf-8 -*-
"""Общие стили для документов."""
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

DARK   = RGBColor(0x1A,0x22,0x2E)
GREY   = RGBColor(0x5A,0x63,0x6E)
BLUE   = RGBColor(0x0B,0x5E,0x8A)
RED    = RGBColor(0xC0,0x39,0x2B)
GREEN  = RGBColor(0x1E,0x84,0x49)
AMBER  = RGBColor(0xB9,0x77,0x0E)

def new_doc():
    doc = Document()
    st = doc.styles['Normal']
    st.font.name = 'Calibri'
    st.font.size = Pt(11)
    st.element.rPr.rFonts.set(qn('w:eastAsia'), 'Calibri')
    for s in doc.sections:
        s.top_margin = s.bottom_margin = Cm(1.8)
        s.left_margin = s.right_margin = Cm(2.0)
    return doc

def shade(cell, hexcolor):
    el = OxmlElement('w:shd'); el.set(qn('w:val'),'clear'); el.set(qn('w:fill'),hexcolor)
    cell._tc.get_or_add_tcPr().append(el)

import re as _re
_NUM_GAP = _re.compile(r'(?<=\d)[  ](?=\d)')

def nbsp(text):
    """Неразрывный пробел внутри числовых групп: «350 000 ₽» не разорвётся между строками."""
    text = _NUM_GAP.sub(' ', text)
    return _re.sub(r'(?<=\d)\s(?=₽|%)', ' ', text)

def p(doc, text='', size=11, bold=False, color=DARK, before=0, after=4, align=None, italic=False):
    par = doc.add_paragraph()
    par.paragraph_format.space_before = Pt(before)
    par.paragraph_format.space_after  = Pt(after)
    if align: par.alignment = align
    r = par.add_run(nbsp(text)); r.font.size = Pt(size); r.bold = bold; r.italic = italic; r.font.color.rgb = color
    return par

def h1(doc, text):
    return p(doc, text, size=20, bold=True, before=10, after=8)

def h2(doc, text, color=BLUE):
    return p(doc, text, size=14, bold=True, color=color, before=14, after=6)

def rule(doc):
    par = doc.add_paragraph(); par.paragraph_format.space_before=Pt(2); par.paragraph_format.space_after=Pt(6)
    pPr = par._p.get_or_add_pPr(); pbdr = OxmlElement('w:pBdr'); bot = OxmlElement('w:bottom')
    bot.set(qn('w:val'),'single'); bot.set(qn('w:sz'),'6'); bot.set(qn('w:color'),'DCE3EA')
    pbdr.append(bot); pPr.append(pbdr)

def money(n):
    return f'{n:,.0f} ₽'.replace(',', ' ')


def table_layout(table, widths_cm, pad=(60, 130, 60, 130)):
    """Жёсткая раскладка колонок. Только штатный API python-docx —
    ручная вставка узлов ломает порядок элементов в OOXML, и файл перестаёт открываться."""
    table.autofit = False                      # сам ставит w:tblLayout в нужное место
    tblPr = table._tbl.tblPr

    # отступы внутри ячеек: w:tblCellMar идёт после w:tblLayout и перед w:tblLook
    mar = OxmlElement('w:tblCellMar')
    for side, val in zip(('top', 'left', 'bottom', 'right'), pad):
        el = OxmlElement(f'w:{side}')
        el.set(qn('w:w'), str(val)); el.set(qn('w:type'), 'dxa')
        mar.append(el)
    tblPr.insert_element_before(mar, 'w:tblLook', 'w:tblStyleRowBandSize',
                                'w:tblStyleColBandSize', 'w:tblCaption', 'w:tblDescription')

    grid = table._tbl.find(qn('w:tblGrid'))
    if grid is not None:
        for gc in list(grid):
            grid.remove(gc)
        for w in widths_cm:
            gc = OxmlElement('w:gridCol'); gc.set(qn('w:w'), str(int(w * 567))); grid.append(gc)

    for row in table.rows:
        cells = row.cells
        seen = set()
        uniq = [c for c in cells if id(c._tc) not in seen and not seen.add(id(c._tc))]
        if len(uniq) == len(widths_cm):
            for c, w in zip(uniq, widths_cm):
                c.width = Cm(w)
        elif len(uniq) == 1:                   # строка-заголовок во всю ширину
            uniq[0].width = Cm(sum(widths_cm))

def repeat_header(table):
    """Шапка повторяется на каждой странице."""
    trPr = table.rows[0]._tr.get_or_add_trPr()
    el = OxmlElement('w:tblHeader'); el.set(qn('w:val'), 'true')
    trPr.insert_element_before(el, 'w:trHeight', 'w:tblCellSpacing', 'w:jc', 'w:hidden',
                               'w:ins', 'w:del', 'w:trPrChange')

def page_break_before(par):
    par.paragraph_format.page_break_before = True
    return par
def keep_rows_together(table):
    """Строка таблицы не разрывается между страницами."""
    for row in table.rows:
        trPr = row._tr.get_or_add_trPr()
        el = OxmlElement('w:cantSplit')
        trPr.insert_element_before(el, 'w:trHeight', 'w:tblHeader', 'w:tblCellSpacing',
                                   'w:jc', 'w:hidden', 'w:ins', 'w:del', 'w:trPrChange')
