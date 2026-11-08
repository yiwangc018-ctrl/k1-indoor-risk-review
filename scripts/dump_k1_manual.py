import sys, json
from docx import Document

path = r"E:\AAA new door\电子协会的比赛\K1边缘视觉实验手册(1).docx"
doc = Document(path)

out = []
body = doc.element.body
from docx.table import Table
from docx.text.paragraph import Paragraph

def iter_block_items(parent):
    from docx.oxml.ns import qn
    for child in parent.iterchildren():
        if child.tag == qn('w:p'):
            yield Paragraph(child, doc)
        elif child.tag == qn('w:tbl'):
            yield Table(child, doc)

for block in iter_block_items(body):
    if isinstance(block, Paragraph):
        t = block.text.strip()
        if t:
            style = block.style.name if block.style is not None else ""
            out.append(f"[{style}] {t}")
    else:
        out.append("=== TABLE ===")
        for row in block.rows:
            cells = []
            for c in row.cells:
                cells.append(c.text.replace("\n", " / ").strip())
            out.append(" | ".join(cells))
        out.append("=== END TABLE ===")

print("\n".join(out))
print("\n\n--- META ---")
print("paragraphs:", len(doc.paragraphs), "tables:", len(doc.tables))
print("sections:", len(doc.sections))
