from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from gsme_rag.parsing.limits import validate_office_container
from gsme_rag.parsing import parse_to_markdown


class OfficeUploadLimitTests(TestCase):
    def test_traversal_and_duplicate_entries_are_rejected_before_format_parsing(self):
        with TemporaryDirectory() as temporary:
            for name in ("../outside.xml", "C:/outside.xml", "/outside.xml"):
                path = Path(temporary) / "bad.docx"
                with ZipFile(path, "w") as archive:
                    archive.writestr(name, "untrusted")
                with self.assertRaisesRegex(ValueError, "unsafe"):
                    parse_to_markdown(path)

    def test_expanded_size_and_entry_count_are_bounded_without_decompression(self):
        item = SimpleNamespace(filename="word/document.xml", file_size=65 * 1024 * 1024, flag_bits=0)
        with patch("gsme_rag.parsing.limits.ZipFile") as archive:
            archive.return_value.__enter__.return_value.infolist.return_value = [item]
            with self.assertRaisesRegex(ValueError, "expanded size"):
                validate_office_container(Path("oversized.docx"))
            item.file_size = 0
            archive.return_value.__enter__.return_value.infolist.return_value = [item] * 10_001
            with self.assertRaisesRegex(ValueError, "entry limit"):
                validate_office_container(Path("too-many.xlsx"))

    def test_non_office_input_does_not_open_an_archive(self):
        with patch("gsme_rag.parsing.limits.ZipFile") as archive:
            validate_office_container(Path("source.pdf"))
            archive.assert_not_called()
