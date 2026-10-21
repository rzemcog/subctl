from __future__ import annotations

import ast
import io
import ipaddress
import re
import shlex
import subprocess
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOST_RE = re.compile(
    r"(?i)(?<![a-z0-9_-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"(?:com|net|org|io|dev|app|test|example|invalid|one|online|local|"
    r"info|biz|ai|edu|gov|mil|co|ru|me|de|uk|us|ca|jp|in|pro|cloud|"
    r"tech|tv|gg|xyz|site|xn--[a-z0-9-]{2,59}[a-z0-9])(?![a-z0-9_-])"
)
IPV4_RE = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")
IPV6_RE = re.compile(r"(?i)(?<![a-z0-9:.])([0-9a-f:.]{2,})(?![a-z0-9:.])")
IPV6_LABEL_RE = re.compile(r"(?i)\bIPv6:\s*([0-9a-f:.]{2,})(?![a-z0-9:.])")
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
    ".githubusercontent.com",
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
    "githubusercontent.com",
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
        strings = [
            node.value
            for node in ast.walk(parsed)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        comments = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(content).readline)
            if token.type == tokenize.COMMENT
        ]
        return " ".join((*strings, *comments))
    if path.suffix.casefold() in {".jsx", ".css", ".html"}:
        return " ".join(URL_RE.findall(content))
    return content


def _ip_literals(content: str) -> list[str]:
    found: list[str] = []
    candidates = (
        *IPV4_RE.findall(content),
        *IPV6_RE.findall(content),
        *IPV6_LABEL_RE.findall(content),
    )
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        normalized = address.compressed
        if normalized not in found:
            found.append(normalized)
    return found


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


def test_repository_neutrality_classifies_reserved_ipv6_as_neutral():
    assert _is_neutral_ip("2001:db8::1") is True


def test_repository_neutrality_classifies_global_ipv6_without_embedding_it():
    public_address = ipaddress.IPv6Address(
        ":".join(("2606", "4700", "4700", "0", "0", "0", "0", "1111"))
    )
    assert _is_neutral_ip(public_address.compressed) is False
    seven_hextets = ":".join(
        ("2606", "4700", "4700", "0", "0", "0", "0", "", "")
    )
    assert _is_neutral_ip(ipaddress.IPv6Address(seven_hextets).compressed) is False


def test_repository_neutrality_extracts_compressed_and_embedded_ipv6():
    assert _ip_literals("resolver [2001:db8::53] and ::ffff:192.0.2.1") == [
        "192.0.2.1",
        "2001:db8::53",
        "::ffff:192.0.2.1",
    ]
    seven_hextets = ":".join(
        ("2606", "4700", "4700", "0", "0", "0", "0", "", "")
    )
    compressed = ipaddress.IPv6Address(seven_hextets).compressed
    assert _ip_literals(f"[{seven_hextets}]") == [compressed]
    assert _ip_literals("IPv6:2001:db8::53") == ["2001:db8::53"]


def test_tracked_repository_network_literals_are_neutral_or_public_dependencies():
    unsafe_files: set[Path] = set()
    for path in _tracked_text_files():
        content = path.read_text(encoding="utf-8", errors="replace")
        domain_text = _domain_scan_text(path, content)
        unsafe_host = any(
            not _is_neutral_hostname(host) for host in HOST_RE.findall(domain_text)
        )
        unsafe_ip = any(
            not _is_neutral_ip(address) for address in _ip_literals(content)
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
