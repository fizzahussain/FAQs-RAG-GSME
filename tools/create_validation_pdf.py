"""Create a synthetic PDF to test native text, OCR, tables and visual review."""

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


OUTPUT = Path(__file__).resolve().parents[1] / "examples" / "GSME_Parsing_Validation_0.14.3.pdf"
WIDTH, HEIGHT = 612, 792


def furniture(pdf, page):
    pdf.setStrokeColor(colors.HexColor("#1c6581"))
    pdf.setLineWidth(1.4)
    pdf.line(48, 745, 564, 745)
    pdf.setFillColor(colors.HexColor("#1c6581"))
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(48, 757, "GSME  /  PARSING VALIDATION")
    pdf.setFillColor(colors.HexColor("#45586a"))
    pdf.setFont("Helvetica", 8)
    pdf.drawRightString(564, 758, "SYNTHETIC TEST DOCUMENT")
    pdf.line(48, 53, 564, 53)
    pdf.drawString(48, 38, "CONFIDENTIAL TEST FOOTER - NOT SOURCE EVIDENCE")
    pdf.drawRightString(564, 38, str(page))


def title(pdf, text, y):
    pdf.setFillColor(colors.HexColor("#143f56"))
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(48, y, text)
    return y - 28


def section(pdf, text, y):
    pdf.setFillColor(colors.HexColor("#143f56"))
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(48, y, text)
    return y - 20


def lines(pdf, values, y, leading=17):
    pdf.setFillColor(colors.HexColor("#253342"))
    pdf.setFont("Helvetica", 10)
    for value in values:
        pdf.drawString(48, y, value)
        y -= leading
    return y


def table(pdf, x, top, widths, rows, height=28):
    total = sum(widths)
    for row_number, row in enumerate(rows):
        y = top - row_number * height
        if row_number == 0:
            pdf.setFillColor(colors.HexColor("#dfeef3"))
            pdf.rect(x, y - height, total, height, stroke=0, fill=1)
        pdf.setStrokeColor(colors.HexColor("#8a9eaa"))
        pdf.rect(x, y - height, total, height, stroke=1, fill=0)
        xx = x
        for width, value in zip(widths, row):
            pdf.setFillColor(colors.HexColor("#253342"))
            pdf.setFont("Helvetica-Bold" if row_number == 0 else "Helvetica", 9)
            pdf.drawString(xx + 7, y - 18, value)
            xx += width
            pdf.line(xx, y, xx, y - height)


def raster():
    image = Image.new("RGB", (1200, 310), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((12, 12, 1188, 298), outline="#29516c", width=4)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font = ImageFont.truetype(font_path, 53)
    draw.text((55, 52), "OCR TEST: YIELD 92 PERCENT", font=font, fill="#172b3c")
    draw.text((55, 160), "LOT A17  /  REVIEW NUMBERS", font=font, fill="#172b3c")
    data = BytesIO()
    image.save(data, format="PNG")
    data.seek(0)
    return ImageReader(data)


def main():
    pdf = canvas.Canvas(str(OUTPUT), pagesize=(WIDTH, HEIGHT))
    pdf.setTitle("GSME parsing validation 0.14.3 - synthetic")
    furniture(pdf, 1)
    y = title(pdf, "Parsing validation - synthetic", 704)
    y = section(pdf, "Chapter 1  Manual context and tables", y)
    y = lines(pdf, [
        "1.1 Overview of the inspection flow",
        "This is selectable PDF text. It should remain ordinary paper prose, not an FAQ.",
        "The repeated header and footer should not become retrieval evidence.",
    ], y)
    y -= 18
    y = section(pdf, "1.2 Wafer sample readings", y)
    table(pdf, 48, y - 4, [172, 172, 172], [
        ["Lot", "Yield", "Disposition"],
        ["A17", "92 percent", "reviewed example"],
        ["B08", "87 percent", "reviewed example"],
    ])
    y -= 115
    y = lines(pdf, [
        "The table cells are source data. They require row-level review when inferred.",
        "1.3 A question in a technical manual",
        "What is the purpose of this sample?",
        "It tests whether one incidental question changes the whole manual into an FAQ.",
        "1.4 Next numbered section continues the manual structure.",
    ], y)
    pdf.showPage()

    furniture(pdf, 2)
    y = title(pdf, "Image OCR and visual absence", 704)
    y = section(pdf, "Chapter 2  Embedded raster image", y)
    y = lines(pdf, [
        "This surrounding sentence is selectable text and should remain unflagged.",
        "The large box below is a PNG embedded into the PDF, not selectable text.",
    ], y)
    pdf.drawImage(raster(), 62, 390, width=488, height=127)
    pdf.setFillColor(colors.HexColor("#45586a"))
    pdf.setFont("Helvetica-Oblique", 9)
    pdf.drawCentredString(306, 372, "Figure 1. Synthetic OCR image - numbers need source review")
    y = section(pdf, "2.1 Expected behavior", 324)
    lines(pdf, [
        "With Tesseract installed, the OCR text should be marked for review.",
        "Without Tesseract, an explicit unavailable warning and visual gap must remain.",
        "Neither path proves the image contains a reliable numeric fact.",
    ], y)
    pdf.showPage()

    furniture(pdf, 3)
    y = title(pdf, "Charts and formulas need review", 704)
    y = section(pdf, "Chapter 3  Numeric evidence boundary", y)
    y = lines(pdf, [
        "3.1 Formula source line",
        "GDPW = 3.14 x (wafer radius - edge exclusion)^2 / die area",
        "A plain line is available, but symbol fidelity must be checked before indexing.",
    ], y)
    y -= 22
    y = section(pdf, "3.2 Chart label mapping", y)
    colors_used = [colors.HexColor("#2e7594"), colors.HexColor("#e59b5b")]
    pdf.setFillColor(colors_used[0])
    pdf.wedge(100, 285, 280, 465, 90, 72, stroke=0, fill=1)
    pdf.setFillColor(colors_used[1])
    pdf.wedge(100, 285, 280, 465, 162, 288, stroke=0, fill=1)
    pdf.setFillColor(colors.HexColor("#253342"))
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(285, 427, "A: 20 percent")
    pdf.drawString(285, 405, "B: 80 percent")
    pdf.setFont("Helvetica-Oblique", 9)
    pdf.drawString(100, 262, "Figure 2. Synthetic chart - spatial relationships require review")
    lines(pdf, [
        "3.3 Closing notes",
        "A chart caption alone does not verify category-to-percentage mapping.",
        "A visual-evidence warning should survive the parsing and chunking stages.",
    ], 218)
    pdf.showPage()
    pdf.save()
    print(OUTPUT)


if __name__ == "__main__":
    main()
