#!/usr/bin/env python3
"""
extract_pdf.py  —  extract text from a datasheet PDF

Usage:
    python tools/extract_pdf.py <path/to/PARTNUM.pdf>

Outputs <path/to/PARTNUM.txt> with raw text, one page per block.
Requires: pip install pymupdf
"""

import sys
import os

def extract(pdf_path: str) -> str:
    txt_path = os.path.splitext(pdf_path)[0] + ".txt"

    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("ERROR: PyMuPDF not installed — run: pip install pymupdf")
        sys.exit(1)

    doc = fitz.open(pdf_path)
    pages = []
    for i, page in enumerate(doc):
        text = page.get_text()
        if text.strip():
            pages.append(f"=== Page {i+1} ===\n{text}")
    page_count = len(doc)
    doc.close()

    out = "\n".join(pages)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(out)

    print(f"[ok] {page_count} pages → {txt_path} ({len(out)//1024} kB)")
    return txt_path

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: extract_pdf.py <file.pdf>")
        sys.exit(1)
    extract(sys.argv[1])
