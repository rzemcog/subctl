from __future__ import annotations

import ast
import ipaddress
import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
HOST_RE = re.compile(
    r"(?i)(?<![a-z0-9_-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"(?:com|net|org|io|dev|app|test|example|invalid|one|online|local|"
    r"info|biz|ai|edu|gov|mil|co|ru|me|de|uk|us|ca|jp|in|pro|cloud|"
    r"tech|tv|gg|name|xyz|site)(?![a-z0-9_-])"
)
IPV4_RE = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")
CHROME_VERSION_RE = re.compile(r"(?i)Chrome/[0-9]+(?:\.[0-9]+){3}")
URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+")
SAFE_HOST_SUFFIXES = (
    ".example",
    ".example.com",
    ".example.net",
    ".example.org",
    ".test",
    ".invalid",
    ".github.com",
    ".gstatic.com",
    ".python.org",
    ".ubuntu.com",
    ".debian.org",
    ".nodejs.org",
)
SAFE_PUBLIC_HOSTS = {
    "example.com",
    "example.net",
    "example.org",
    "github.com",
    "gstatic.com",
    "pypi.org",
    "caddyserver.com",
    "python.org",
    "ubuntu.com",
    "debian.org",
    "nodejs.org",
    "metacubex.one",
    "wiki.metacubex.one",
}
SAFE_PUBLIC_IPS = {"1.1.1.1", "8.8.8.8"}
TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".toml", ".sh", ".jsx", ".html", ".css"}
SCAN_ROOTS = ("src", "examples", "tests", "docs", "deploy", "scripts", "web/src")


def _is_neutral_hostname(value: str) -> bool:
    hostname = value.casefold().rstrip(".")
    return hostname in SAFE_PUBLIC_HOSTS or hostname.endswith(SAFE_HOST_SUFFIXES)


def _is_neutral_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    shared_range = ipaddress.ip_network("100.64.0.0/10")
    return value in SAFE_PUBLIC_IPS or address in shared_range or not address.is_global


def _domain_scan_text(path: Path, content: str) -> str:
    if path.suffix.casefold() != ".py":
        if path.suffix.casefold() in {".jsx", ".css", ".html"}:
            return " ".join(URL_RE.findall(content))
        return content
    parsed = ast.parse(content)
    return " ".join(
        node.value
        for node in ast.walk(parsed)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def test_repository_configuration_example_uses_an_empty_hosts_mapping():
    config = yaml.safe_load((REPO_ROOT / "examples/config.yaml").read_text(encoding="utf-8"))

    assert config["mihomo"]["hosts"] == {}


def test_repository_network_literals_are_placeholders_or_public_dependencies():
    files = [
        path
        for root_name in SCAN_ROOTS
        for path in (REPO_ROOT / root_name).rglob("*")
        if path.is_file() and path.suffix.casefold() in TEXT_SUFFIXES
    ]
    files.append(REPO_ROOT / "README.md")
    unneutral_files: set[Path] = set()

    for path in files:
        content = path.read_text(encoding="utf-8", errors="replace")
        domain_text = _domain_scan_text(path, content)
        ip_text = CHROME_VERSION_RE.sub("", content)
        if any(not _is_neutral_hostname(host) for host in HOST_RE.findall(domain_text)):
            unneutral_files.add(path)
        if any(not _is_neutral_ip(address) for address in IPV4_RE.findall(ip_text)):
            unneutral_files.add(path)

    assert not unneutral_files, (
        "repository text contains non-placeholder network literals in "
        f"{len(unneutral_files)} file(s); move endpoint values to protected runtime config"
    )
