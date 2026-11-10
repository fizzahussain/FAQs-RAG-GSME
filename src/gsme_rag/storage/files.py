"""Local storage for original files"""

from hashlib import sha256
from pathlib import Path
import os
from uuid import uuid4


def checksum(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verified_markdown(path: Path, expected_sha256: str) -> bytes:
    """Allow only the legacy Windows newline bug, never a changed artifact.

    Keep the original bytes on disk for inspection. Even newline recovery must
    reproduce the recorded hash; technical content is never silently repaired.
    """
    content = path.read_bytes()
    if sha256(content).hexdigest() == expected_sha256:
        return content
    legacy = content.replace(b"\r\n", b"\n")
    if sha256(legacy).hexdigest() == expected_sha256:
        return legacy
    raise ValueError("Normalized Markdown changed after ingestion; submit a new version")


def save_original(path: Path, root: Path, tenant_id: str, version_id: str) -> tuple[Path, str]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Source must be a regular local file")
    # The tenant folder avoids putting customer names in local paths
    target_dir = root / "originals" / sha256(tenant_id.encode()).hexdigest()[:16] / version_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / ("source" + path.suffix.lower())
    temporary = target_dir / ("." + uuid4().hex)
    digest = sha256()
    try:
        with path.open("rb") as source, temporary.open("xb") as destination:
            # Hash each block as it is copied so large files stay out of memory
            for block in iter(lambda: source.read(1024 * 1024), b""):
                destination.write(block)
                digest.update(block)
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target, digest.hexdigest()
