"""Regenerates the sample PDF in sample_notes/notes. Development only.

Needs reportlab (pip install reportlab). The app itself never runs this and
reads PDFs with nothing but Python.
"""
import os
import sys

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "sample_notes", "notes")
path = os.path.join(OUT, "2026-11-09 finance budget letter.pdf")
ss = getSampleStyleSheet()
doc = SimpleDocTemplate(path, pagesize=A4, title="Budget confirmation", author="Finance (fictional)")
story = [
    Paragraph("Finance Business Partnering", ss["Normal"]),
    Paragraph("Budget confirmation", ss["Title"]),
    Paragraph("9 Nov 2026", ss["Normal"]),
    Paragraph("To: Analytics team lead<br/>From: Mei Lin, Finance Business Partner", ss["Normal"]),
    Spacer(1, 14),
    Paragraph("Analytics platform (Helix): year-one budget", ss["Heading2"]),
    Paragraph("This letter confirms the revised year-one budget for the Helix analytics platform, following "
              "the October finance review.", ss["Normal"]),
    Spacer(1, 10),
    Table([["Approved budget", "£28k for year one"], ["Cost centre", "CC-4471"],
           ["Scope", "Pilot with 2 teams from 1 Dec 2026"], ["Review", "March 2027"]],
          colWidths=[130, 300], style=TableStyle([("GRID", (0, 0), (-1, -1), 0.5, "#999999"),
                                                  ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold")])),
    Spacer(1, 10),
    Paragraph("Any spend above the approved amount needs a new approval before it is committed. "
              "Please raise purchase orders against cost centre CC-4471 only.", ss["Normal"]),
    Spacer(1, 18),
    Paragraph("Sent by Mei Lin, Finance, 9 Nov 2026.", ss["Normal"]),
]
doc.build(story)
print("wrote", os.path.abspath(path))
