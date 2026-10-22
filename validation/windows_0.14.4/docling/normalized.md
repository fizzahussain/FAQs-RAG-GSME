<!-- page: 1 -->

**Parsing validation - synthetic**

# Chapter 1 Manual context and tables

## 1.1 Overview of the inspection flow

This is selectable PDF text. It should remain ordinary paper prose, not an FAQ.

The repeated header and footer should not become retrieval evidence.

## 1.2 Wafer sample readings

<!-- review-required: docling-table -->

| Lot   | Yield      | Disposition      |
|-------|------------|------------------|
| A17   | 92 percent | reviewed example |
| B08   | 87 percent | reviewed example |

<!-- review-end -->

The table cells are source data. They require row-level review when inferred.

## 1.3 A question in a technical manual

What is the purpose of this sample?

It tests whether one incidental question changes the whole manual into an FAQ.

## 1.4 Next numbered section continues the manual structure.

<!-- page: 2 -->

**Image OCR and visual absence**

# Chapter 2 Embedded raster image

This surrounding sentence is selectable text and should remain unflagged.

The large box below is a PNG embedded into the PDF, not selectable text.

<!-- review-required: pdf-image-ocr -->

Image text (OCR): OCR TEST: YIELD 92 PERCENT LOT A17 / REVIEW NUMBERS

<!-- review-end -->

Figure 1. Synthetic OCR image - numbers need source review

## 2.1 Expected behavior

With Tesseract installed, the OCR text should be marked for review.

Without Tesseract, an explicit unavailable warning and visual gap must remain.

Neither path proves the image contains a reliable numeric fact.


<!-- review-required: visual-evidence -->

Visual evidence on page 2 requires source review; no verified description is available.

<!-- review-end -->

<!-- page: 3 -->

<!-- review-required: visual-page -->

**Charts and formulas need review**

# Chapter 3 Numeric evidence boundary

## 3.1 Formula source line

GDPW = 3.14 x (wafer radius - edge exclusion)^2 / die area

A plain line is available, but symbol fidelity must be checked before indexing.

## 3.2 Chart label mapping

Figure 2. Synthetic chart - spatial relationships require review

Visual evidence on page 3 requires source review; no verified description is available.

<!-- review-required: recovered-selectable-text -->

Recovered selectable text omitted by layout conversion:

A: 20 percent

B: 80 percent

<!-- review-end -->

## 3.3 Closing notes

A chart caption alone does not verify category-to-percentage mapping.

A visual-evidence warning should survive the parsing and chunking stages.

<!-- review-end -->
