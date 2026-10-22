# Windows test follow-up

Version 0.14.2 responds to the Windows Python 3.13 test output from version 0.14.1.

- The fresh install had selected PyMuPDF 2.0. Its table extraction produced an extra empty column for the test PDF. The supported requirement is now `PyMuPDF>=1.24,<2` so installation selects the 1.x line used for validation. This is a compatibility bound, not a claim that every future PDF table will be correct.
- The HTTP test client was missing from the base environment. Install the `test` extra (`python -m pip install -e ".[test]"`) for `httpx2` when running the test suite. Docling remains separately optional under the `pdf` extra.
- A failed PyMuPDF file open kept an uploaded corrupt PDF locked while the API tried to remove its temporary file on Windows. Files up to 20 MiB, including all allowed API uploads, now open from bytes. Larger local CLI PDFs still open by path so they do not need a whole-file copy in memory. The API continues to return HTTP 422 for an invalid PDF. This code path passed locally; the Windows result still needs confirmation on the user's machine.
- Two OCR integration tests previously failed when the Tesseract executable was absent. They now report skipped until Tesseract is installed and visible on PATH. The application itself continues to warn if OCR cannot run. A skip does not verify OCR behavior on that machine.

Validation: the complete 79-test suite passed locally with PyMuPDF 1.28.2 and Tesseract installed. A separate test run simulating unavailable Tesseract yielded nine passing stress tests and two explicit skips. Windows file cleanup requires a rerun on Windows for confirmation.

After extracting this release into a fresh folder, use `py -3.13 -m venv .venv`, then `.\.venv\Scripts\python.exe -m pip install -e ".[test]"` and `.\.venv\Scripts\python.exe -m unittest discover -s tests -v` (omit the spaces before the environment Python in PowerShell). Install Tesseract and reopen the terminal to run all OCR integration tests.
