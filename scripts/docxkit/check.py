#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Единая проверка документа перед отправкой человеку: разбор вёрстки + картинки страниц.

    python3 ~/scripts/docxkit/check.py <файл.docx> [каталог_превью] [страниц]

Печатает найденные проблемы и пути к PNG страниц — их нужно ОТКРЫТЬ И ПОСМОТРЕТЬ.
Автопроверка ловит геометрию, но не ловит смысл: заголовок не на своём месте,
таблица, разъехавшаяся по колонкам, нечитаемое сочетание цветов — это видно только глазом.
"""
import os, sys
KIT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT)
from preflight import check
from preview import build

def main():
    docx = sys.argv[1]
    outdir = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser('~/render/check')
    pages = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    print('=== разбор вёрстки ===')
    problems = check(docx)
    print('\n=== страницы для просмотра ===')
    try:
        for p in build(docx, outdir, pages):
            print('  ', p)
    except Exception as e:
        print('  превью не собралось:', e)
    print('\nПосмотри картинки глазами, прежде чем отправлять.')
    return 1 if problems else 0

if __name__ == '__main__':
    sys.exit(main())
