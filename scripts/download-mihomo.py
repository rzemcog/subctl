from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import urllib.request
from pathlib import Path


REPOSITORY = "MetaCubeX/mihomo"
PIN_PATH = Path(__file__).resolve().parents[1] / ".github" / "mihomo-pin.json"


def download_pinned_mihomo(output: Path) -> None:
    pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    tag = pin.get("tag")
    asset = pin.get("asset")
    digest = pin.get("sha256")
    if not isinstance(tag, str) or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("pinned Mihomo tag must be a stable semantic version")
    if asset != f"mihomo-linux-amd64-compatible-{tag}.gz":
        raise ValueError("pinned Mihomo asset must be the compatible Linux amd64 build")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("pinned Mihomo sha256 must contain 64 lowercase hexadecimal characters")

    url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{asset}"
    request = urllib.request.Request(url, headers={"User-Agent": "subctl-mihomo-ci"})
    with urllib.request.urlopen(request, timeout=60) as response:
        archive = response.read()
    actual_digest = hashlib.sha256(archive).hexdigest()
    if actual_digest != digest:
        raise ValueError("downloaded Mihomo asset does not match its pinned sha256")

    binary = gzip.decompress(archive)
    if not binary.startswith(b"\x7fELF"):
        raise ValueError("pinned Mihomo asset did not contain a Linux executable")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(binary)
    output.chmod(0o755)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    download_pinned_mihomo(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
