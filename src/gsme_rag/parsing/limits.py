"""Bound Office ZIP containers before any XML/image parser expands them."""

from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile


def validate_office_container(path: Path) -> None:
    if path.suffix.casefold() not in {".docx", ".pptx", ".xlsx"}:
        return
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 10_000 or sum(item.file_size for item in entries) > 128 * 1024 * 1024:
                raise ValueError("Office archive exceeds the expanded size or entry limit")
            names = set()
            for item in entries:
                name = item.filename.replace("\\", "/")
                if (name in names or name.startswith("/") or ".." in PurePosixPath(name).parts
                        or ":" in name or item.flag_bits & 1):
                    raise ValueError("Office archive has ambiguous, unsafe or encrypted entries")
                names.add(name)
                if item.file_size > 64 * 1024 * 1024:
                    raise ValueError("An Office archive entry exceeds the expanded size limit")
    except BadZipFile:
        # Let the format reader report ordinary corruption through its existing
        # error path; no archive expansion can occur for a non-ZIP container.
        return
