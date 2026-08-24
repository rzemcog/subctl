#!/usr/bin/env bash
set -Eeuo pipefail

echo "install-web.sh is kept as a compatibility alias; use install.sh instead." >&2
exec "$(dirname "${BASH_SOURCE[0]}")/install.sh" "$@"
