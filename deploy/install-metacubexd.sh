#!/bin/sh
set -eu

VERSION="1.265.2"
ARCHIVE="compressed-dist.tgz"
SHA256="43d5f2073eb26a66b93e10f43cdc14f3fc0ade17e311adde513c6583cd1c948d"
URL="https://github.com/MetaCubeX/metacubexd/releases/download/v${VERSION}/${ARCHIVE}"
ROOT="${METACUBEXD_ROOT:-/var/lib/mihomo/ui}"
RELEASE_DIR="${ROOT}/releases/${VERSION}"
CURRENT="${ROOT}/current"

usage() {
    echo "usage: $0 install | select VERSION" >&2
    exit 2
}

select_release() {
    version=$1
    target="${ROOT}/releases/${version}"
    test -f "${target}/index.html" || {
        echo "release is not installed: ${version}" >&2
        exit 1
    }
    link="${ROOT}/.current.tmp.$$"
    ln -s "releases/${version}" "$link"
    mv -Tf "$link" "$CURRENT"
}

case "${1:-}" in
    install)
        command -v curl >/dev/null
        command -v sha256sum >/dev/null
        command -v tar >/dev/null
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT HUP INT TERM
        curl --fail --location --proto '=https' --tlsv1.2 --output "${tmp}/${ARCHIVE}" "$URL"
        printf '%s  %s\n' "$SHA256" "${tmp}/${ARCHIVE}" | sha256sum --check --status
        mkdir -p "${ROOT}/releases"
        if test ! -d "$RELEASE_DIR"; then
            stage="${ROOT}/releases/.${VERSION}.tmp.$$"
            mkdir "$stage"
            tar -xzf "${tmp}/${ARCHIVE}" -C "$stage" --no-same-owner --no-same-permissions
            test -f "${stage}/index.html"
            chmod -R a-w "$stage"
            find "$stage" -type d -exec chmod 0755 {} +
            find "$stage" -type f -exec chmod 0644 {} +
            chown -R root:root "$stage"
            mv "$stage" "$RELEASE_DIR"
        fi
        select_release "$VERSION"
        ;;
    select)
        test "$#" -eq 2 || usage
        select_release "$2"
        ;;
    *) usage ;;
esac
