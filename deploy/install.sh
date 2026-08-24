#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "run this installer as root (for example: sudo -E $0)" >&2
  exit 1
fi

: "${SUBCTL_DOMAIN:?set SUBCTL_DOMAIN to the public subscription hostname}"
if [[ "$SUBCTL_DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
  :
else
  echo "SUBCTL_DOMAIN must contain only letters, digits, dots and hyphens" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${SUBCTL_APP_DIR:-/opt/subctl}"
VENV_DIR="${SUBCTL_VENV_DIR:-/opt/subctl/venv}"
CONFIG_DIR="${SUBCTL_CONFIG_DIR:-/etc/subctl}"
STATE_DIR="${SUBCTL_STATE_DIR:-/var/lib/subctl}"

if [[ "$ROOT_DIR" != "$APP_DIR" ]]; then
  echo "run the installer from $APP_DIR or set SUBCTL_APP_DIR=$ROOT_DIR" >&2
  exit 1
fi

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
command -v npm >/dev/null 2>&1 || { echo "npm is required to build the Web UI" >&2; exit 1; }

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
