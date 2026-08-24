#!/bin/sh
set -eu

SUBCTL_BIN=${SUBCTL_BIN:-/opt/subctl/venv/bin/subctl}
SUBCTL_CONFIG=${SUBCTL_CONFIG:-/etc/subctl/config.yaml}
INTERFACE=${MIHOMO_TUN_INTERFACE:-ens3}

if systemctl is-active --quiet mihomo-tun.service; then
    echo 'refusing to rerender while mihomo-tun.service is active' >&2
    exit 1
fi

peer=${MIHOMO_SSH_PEER_IP:-}
if [ -z "$peer" ] && [ -n "${SSH_CONNECTION:-}" ]; then
    peer=$(printf '%s\n' "$SSH_CONNECTION" | awk '{print $1}')
fi
[ -n "$peer" ] || {
    echo 'set MIHOMO_SSH_PEER_IP when running outside an SSH session' >&2
    exit 1
}

vps_ip=${MIHOMO_VPS_IP:-}
if [ -z "$vps_ip" ]; then
    vps_ip=$(ip -4 -o addr show dev "$INTERFACE" scope global | awk 'NR == 1 {split($4, a, "/"); print a[1]}')
fi
[ -n "$vps_ip" ] || { echo "could not determine IPv4 address on $INTERFACE" >&2; exit 1; }

management_cidr=${MIHOMO_MANAGEMENT_CIDR:-}
if [ -z "$management_cidr" ]; then
    management_cidr=$(ip -4 route show dev "$INTERFACE" proto kernel scope link | awk 'NR == 1 {print $1}')
fi
[ -n "$management_cidr" ] || { echo "could not determine management CIDR on $INTERFACE" >&2; exit 1; }

set -- \
    --tun-route-exclude-address "$peer/32" \
    --tun-route-exclude-address "$vps_ip/32" \
    --tun-route-exclude-address "$management_cidr"

for extra in ${MIHOMO_TUN_EXTRA_EXCLUDES:-}; do
    set -- "$@" --tun-route-exclude-address "$extra"
done

exec "$SUBCTL_BIN" --config "$SUBCTL_CONFIG" render-gateway --tun "$@"
