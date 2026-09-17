#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTML-превью документа → PDF → PNG постранично.

Работает через связку browser-tools (у системного playwright не хватает библиотек).
Использование: preview.py <файл.docx> <каталог> [страниц]
"""
import os, sys, subprocess, tempfile

KIT = os.path.dirname(os.path.abspath(__file__))
# Python с playwright: переменная DOCXKIT_PYTHON, иначе ~/browser-tools/venv, иначе текущий python3
# (нужно: pip install playwright && python3 -m playwright install --with-deps chromium).
_venv = os.path.expanduser('~/browser-tools/venv/bin/python')
VENV = os.environ.get('DOCXKIT_PYTHON') or (_venv if os.path.exists(_venv) else sys.executable)
LIBS = os.path.expanduser('~/browser-tools/syslibs/extracted/usr/lib/x86_64-linux-gnu')

RENDER = r'''
import sys
from playwright.sync_api import sync_playwright
html_path, pdf_path = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    b = p.chromium.launch(args=['--no-sandbox'])
    pg = b.new_page()
    pg.goto('file://' + html_path)
    pg.pdf(path=pdf_path, format='A4', print_background=True,
           margin={'top':'0','right':'0','bottom':'0','left':'0'})
    b.close()
print(pdf_path)
'''

def build(docx_path, outdir, max_pages=12, dpi=110):
    os.makedirs(outdir, exist_ok=True)
    sys.path.insert(0, KIT)
    from docx_to_html import convert
    html_path = os.path.join(outdir, 'preview.html')
    convert(docx_path, html_path)

    pdf_path = os.path.join(outdir, 'preview.pdf')
    with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False) as f:
        f.write(RENDER); script = f.name
    env = dict(os.environ, LD_LIBRARY_PATH=LIBS + ':' + os.environ.get('LD_LIBRARY_PATH', ''))
    r = subprocess.run([VENV, script, html_path, pdf_path], env=env,
                       capture_output=True, text=True, timeout=180)
    os.unlink(script)
    if not os.path.exists(pdf_path):
        raise RuntimeError('рендер не удался: ' + (r.stderr or r.stdout)[:400])

    import fitz
    doc = fitz.open(pdf_path)
    pages = []
    for i in range(min(len(doc), max_pages)):
        pix = doc[i].get_pixmap(dpi=dpi)
        p = os.path.join(outdir, f'page{i+1:02d}.png')
        pix.save(p); pages.append(p)
    doc.close()
    return pages

if __name__ == '__main__':
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 12
    for p in build(sys.argv[1], sys.argv[2], n): print(p)
