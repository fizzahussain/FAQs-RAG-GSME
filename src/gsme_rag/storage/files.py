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
