#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "run this installer as root (for example: sudo -E $0)" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${SUBCTL_APP_DIR:-/opt/subctl}"
VENV_DIR="${SUBCTL_VENV_DIR:-/opt/subctl/venv}"
CONFIG_DIR="${SUBCTL_CONFIG_DIR:-/etc/subctl}"
STATE_DIR="${SUBCTL_STATE_DIR:-/var/lib/subctl}"

if [[ "${1:-}" == "--release-dir" ]]; then
  if [[ $# -ne 2 ]]; then
    echo "usage: $0 --release-dir /var/lib/subctl/releases/<release-id>" >&2
    exit 2
  fi
  RELEASE_DIR="$(realpath -e -- "$2")"
  RELEASE_ROOT="$(realpath -e -- "$STATE_DIR/releases")"
  if [[ "$(dirname "$RELEASE_DIR")" != "$RELEASE_ROOT" ]]; then
    echo "release directory must be a direct child of $RELEASE_ROOT" >&2
    exit 1
  fi
  command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
  command -v caddy >/dev/null 2>&1 || { echo "caddy is required" >&2; exit 1; }
  command -v systemd-analyze >/dev/null 2>&1 || { echo "systemd-analyze is required" >&2; exit 1; }
  [[ -f "$CONFIG_DIR/config.yaml" ]] || { echo "missing $CONFIG_DIR/config.yaml" >&2; exit 1; }
  [[ -x "$VENV_DIR/bin/python" ]] || { echo "missing runtime Python at $VENV_DIR/bin/python" >&2; exit 1; }

  python3 "$ROOT_DIR/deploy/release.py" verify --path "$RELEASE_DIR" >/dev/null
  for unit in subctl-web.service subctl-refresh.service subctl-refresh.timer; do
    [[ -f "$RELEASE_DIR/systemd/$unit" ]] || { echo "release is missing $unit" >&2; exit 1; }
  done
  [[ -f "$RELEASE_DIR/Caddyfile" ]] || { echo "release is missing Caddyfile" >&2; exit 1; }
  mapfile -t APP_WHEELS < <(find "$RELEASE_DIR/package" -maxdepth 1 -type f -name '*.whl' -print)
  mapfile -t DEPENDENCY_WHEELS < <(find "$RELEASE_DIR/wheelhouse" -maxdepth 1 -type f -name '*.whl' -print | sort)
  [[ ${#APP_WHEELS[@]} -eq 1 && ${#DEPENDENCY_WHEELS[@]} -gt 0 ]] || {
    echo "release must contain one application wheel and dependency wheels" >&2
    exit 1
  }

  systemd-analyze verify \
    "$RELEASE_DIR/systemd/subctl-web.service" \
    "$RELEASE_DIR/systemd/subctl-refresh.service" \
    "$RELEASE_DIR/systemd/subctl-refresh.timer"
  caddy validate --config "$RELEASE_DIR/Caddyfile"
  "$VENV_DIR/bin/python" -m pip install --no-index --no-deps --force-reinstall \
    "${APP_WHEELS[0]}" "${DEPENDENCY_WHEELS[@]}"
  "$VENV_DIR/bin/python" -m pip check

  staged_files=()
  cleanup_release_staging() {
    for staged_file in "${staged_files[@]}"; do
      rm -f -- "$staged_file"
    done
  }
  trap cleanup_release_staging EXIT

  install -d -o root -g root -m 0755 /etc/systemd/system /etc/caddy
  for unit in subctl-web.service subctl-refresh.service subctl-refresh.timer; do
    staged="/etc/systemd/system/.${unit}.subctl.$$"
    install -o root -g root -m 0644 "$RELEASE_DIR/systemd/$unit" "$staged"
    staged_files+=("$staged")
  done
  staged_caddy="/etc/caddy/.Caddyfile.subctl.$$"
  install -o root -g root -m 0644 "$RELEASE_DIR/Caddyfile" "$staged_caddy"
  staged_files+=("$staged_caddy")
  caddy validate --config "$staged_caddy"

  for unit in subctl-web.service subctl-refresh.service subctl-refresh.timer; do
    mv -f -- "/etc/systemd/system/.${unit}.subctl.$$" "/etc/systemd/system/$unit"
  done
  mv -f -- "$staged_caddy" /etc/caddy/Caddyfile
  systemctl daemon-reload
  caddy validate --config /etc/caddy/Caddyfile
  if systemctl is-active --quiet caddy.service; then
    systemctl reload caddy.service
  else
    systemctl enable --now caddy.service
  fi
  echo "subctl immutable release assets installed; restart and verification are managed by release.py"
  exit 0
fi

: "${SUBCTL_DOMAIN:?set SUBCTL_DOMAIN to the public subscription hostname}"
if [[ "$SUBCTL_DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
  :
else
  echo "SUBCTL_DOMAIN must contain only letters, digits, dots and hyphens" >&2
  exit 1
fi

if [[ "$ROOT_DIR" != "$APP_DIR" ]]; then
  echo "run the installer from $APP_DIR or set SUBCTL_APP_DIR=$ROOT_DIR" >&2
  exit 1
fi

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
command -v node >/dev/null 2>&1 || { echo "Node.js 20.19+ is required" >&2; exit 1; }
command -v npm >/dev/null 2>&1 || { echo "npm is required to build the Web UI" >&2; exit 1; }
if ! node -e 'const [major, minor] = process.versions.node.split(".").map(Number); const valid = (major === 20 && minor >= 19) || (major === 22 && minor >= 12) || major > 22; process.exit(valid ? 0 : 1)'; then
  echo "Node.js 20.19+ (or 22.12+) is required; found $(node --version)" >&2
  exit 1
fi

getent group subctl >/dev/null 2>&1 || groupadd --system subctl
id -u subctl >/dev/null 2>&1 || useradd --system --home-dir "$STATE_DIR" \
  --shell /usr/sbin/nologin --gid subctl subctl

install -d -o root -g root -m 0755 "$APP_DIR"
install -d -o root -g subctl -m 0750 "$CONFIG_DIR"
install -d -o subctl -g subctl -m 0750 \
  "$STATE_DIR" "$STATE_DIR/registry" "$STATE_DIR/ui" "$STATE_DIR/public"

if [[ ! -e "$VENV_DIR/bin/python" ]]; then
  install -d -o root -g root -m 0755 "$(dirname "$VENV_DIR")"
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip

if [[ -f "$ROOT_DIR/web/package.json" ]]; then
  npm ci --prefix "$ROOT_DIR/web"
  npm run build --prefix "$ROOT_DIR/web"
fi
"$VENV_DIR/bin/python" -m pip install "$ROOT_DIR"

if [[ ! -e "$STATE_DIR/registry/users.yaml" ]]; then
  printf 'users:\n' | install -o subctl -g subctl -m 0600 /dev/stdin "$STATE_DIR/registry/users.yaml"
fi
if [[ ! -e "$CONFIG_DIR/config.yaml" ]]; then
  echo "missing $CONFIG_DIR/config.yaml; create protected configuration first" >&2
  exit 1
fi

install -o root -g root -m 0644 "$ROOT_DIR/deploy/subctl-web.service" \
  /etc/systemd/system/subctl-web.service
install -o root -g root -m 0644 "$ROOT_DIR/deploy/subctl-refresh.service" \
  /etc/systemd/system/subctl-refresh.service
install -o root -g root -m 0644 "$ROOT_DIR/deploy/subctl-refresh.timer" \
  /etc/systemd/system/subctl-refresh.timer

if [[ -d /etc/caddy ]]; then
  caddyfile_tmp="/etc/caddy/.Caddyfile.subctl.tmp.$$"
  sed 's|{\$SUBCTL_DOMAIN}|'"$SUBCTL_DOMAIN"'|g' \
    "$ROOT_DIR/deploy/Caddyfile" > "$caddyfile_tmp"
  chmod 0644 "$caddyfile_tmp"
  chown root:root "$caddyfile_tmp"
  install -m 0644 "$caddyfile_tmp" /etc/caddy/Caddyfile
  rm -f "$caddyfile_tmp"
  caddy validate --config /etc/caddy/Caddyfile
fi

systemctl daemon-reload
systemctl enable --now subctl-web.service subctl-refresh.timer
if systemctl is-active --quiet caddy; then
  systemctl reload caddy
fi

echo "subctl installed for ${SUBCTL_DOMAIN}"
