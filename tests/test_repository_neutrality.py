from __future__ import annotations

import ast
import ipaddress
import re
import shlex
import subprocess
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
WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?<![a-z0-9])[a-z]:[\\/]"
    r"(?:[^\\/\s\"'`]+[\\/])+"
    r"[^\\/\s\"'`]*"
)
URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+")
UNIX_USER_HOME_RE = re.compile(r"(?i)(?<![a-z0-9])/(?:users|home)/[a-z0-9._-]+(?:/|$)")
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
    ".npmjs.org",
    ".yarnpkg.com",
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
    "npmjs.org",
    "yarnpkg.com",
    "opencollective.com",
    "tidelift.com",
}
TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".toml", ".sh", ".jsx", ".html", ".css"}


def _is_neutral_hostname(value: str) -> bool:
    hostname = value.casefold().rstrip(".")
    return hostname in SAFE_PUBLIC_HOSTS or hostname.endswith(SAFE_HOST_SUFFIXES)


def _is_neutral_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    shared_range = ipaddress.ip_network("100.64.0.0/10")
    return address in shared_range or not address.is_global


def _is_neutral_ssh_target(value: str) -> bool:
    target = value.strip("\"'`")
    if target.startswith("$") or (target.startswith("<") and target.endswith(">")):
        return True
    if "@" in target:
        target = target.rsplit("@", 1)[1]
    return _is_neutral_hostname(target)



def _domain_scan_text(path: Path, content: str) -> str:
    if path.suffix.casefold() == ".py":
        parsed = ast.parse(content)
        return " ".join(
            node.value
            for node in ast.walk(parsed)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
    if path.suffix.casefold() in {".jsx", ".css", ".html"}:
        return " ".join(URL_RE.findall(content))
    return content


def _ssh_destinations(content: str) -> list[str]:
    logical_lines = re.sub(r"\\\r?\n[ \t]*", " ", content)
    destinations: list[str] = []
    for match in re.finditer(r"(?m)^[ \t]*ssh[ \t]+([^\r\n]+)", logical_lines):
        try:
            arguments = shlex.split(match.group(1))
        except ValueError:
            continue
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            if argument == "--":
                index += 1
                if index < len(arguments):
                    destinations.append(arguments[index])
                break
            if not argument.startswith("-"):
                destinations.append(argument)
                break
            option = argument[:2]
            if len(argument) == 2 and option.lower() in {"-b", "-c", "-d", "-e", "-f", "-i", "-j", "-l", "-m", "-o", "-p", "-q", "-s", "-w"}:
                index += 2
            else:
                index += 1
    return destinations


def _tracked_text_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    paths = [Path(item.decode("utf-8")) for item in result.stdout.split(b"\0") if item]
    return [
        REPO_ROOT / path
        for path in paths
        if path.suffix.casefold() in TEXT_SUFFIXES and (REPO_ROOT / path).is_file()
    ]


def test_repository_configuration_example_uses_an_empty_hosts_mapping():
    config = yaml.safe_load((REPO_ROOT / "examples/config.yaml").read_text(encoding="utf-8"))

    assert config["mihomo"]["hosts"] == {}
    assert config["mihomo"]["geox_urls"] == {}
    assert config["mihomo"]["routing"] == {
        "direct_node_name_keywords": [],
        "direct_rules": [],
    }


def test_tracked_repository_network_literals_are_neutral_or_public_dependencies():
    unsafe_files: set[Path] = set()
    for path in _tracked_text_files():
        content = path.read_text(encoding="utf-8", errors="replace")
        ip_text = CHROME_VERSION_RE.sub("", content)
        domain_text = _domain_scan_text(path, content)
        unsafe_host = any(
            not _is_neutral_hostname(host) for host in HOST_RE.findall(domain_text)
        )
        unsafe_ip = any(
            not _is_neutral_ip(address) for address in IPV4_RE.findall(ip_text)
        )
        unsafe_path = bool(
            WINDOWS_ABSOLUTE_PATH_RE.search(content) or UNIX_USER_HOME_RE.search(content)
        )
        unsafe_ssh = any(
            not _is_neutral_ssh_target(target) for target in _ssh_destinations(content)
        )
        if unsafe_host or unsafe_ip or unsafe_path or unsafe_ssh:
            unsafe_files.add(path.relative_to(REPO_ROOT))

    assert not unsafe_files, (
        "tracked text contains a non-neutral hostname/IP, workstation path, "
        "or concrete SSH target in "
        f"{len(unsafe_files)} file(s): "
        f"{', '.join(sorted(map(str, unsafe_files)))}"
    )
