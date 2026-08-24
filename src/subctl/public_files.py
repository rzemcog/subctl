from __future__ import annotations

import os
from pathlib import Path

PUBLIC_DIR_MODE = 0o755
PUBLIC_FILE_MODE = 0o644


def ensure_public_directory(path: Path) -> None:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, PUBLIC_DIR_MODE)

