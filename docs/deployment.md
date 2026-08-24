# Deployment on Ubuntu 24.04 or Debian 12

This guide deploys the CLI, Web UI, Caddy publisher and optional Mihomo
gateway. Keep credentials out of the repository and shell history where
possible.

## 1. Install prerequisites

```bash
sudo apt update
sudo apt install -y \
  ca-certificates curl git python3 python3-venv python3-pip \
  nodejs npm caddy tar coreutils gettext-base
```

Install Mihomo separately when the gateway is required. Verify that the binary
is available as `/usr/local/bin/mihomo` and create its service account:

```bash
sudo useradd --system --home-dir /var/lib/mihomo --shell /usr/sbin/nologin mihomo || true
```

Install MetaCubeXD only when the local Mihomo panel is needed:

```bash
sudo ./deploy/install-metacubexd.sh install
```

## 2. Prepare the checkout

```bash
sudo git clone https://github.com/<owner>/<repo>.git /opt/subctl
cd /opt/subctl
```

## 3. Create protected configuration

```bash
sudo install -d -o root -g subctl -m 0750 /etc/subctl
sudo install -d -o subctl -g subctl -m 0750 \
  /var/lib/subctl/registry /var/lib/subctl/ui /var/lib/subctl/public
sudo install -o root -g subctl -m 0640 /path/to/config.yaml /etc/subctl/config.yaml
sudo install -o subctl -g subctl -m 0600 /path/to/users.yaml \
  /var/lib/subctl/registry/users.yaml
```

Create the files from [configuration.md](configuration.md). Never copy real
production values into `examples/` or commit them.

## 4. Install services and Caddy

Set the public hostname and run the idempotent installer:

```bash
export SUBCTL_DOMAIN=sub.example.com
sudo -E ./deploy/install.sh
```

The installer validates the hostname, installs the Web UI and refresh units,
renders the Caddy template with the supplied domain, validates Caddy and
reloads services. It does not create or rotate secrets.

The Web UI listens on `127.0.0.1:12790`. Caddy serves subscription endpoints
and proxies tokenized paths to FastAPI so download telemetry can be recorded
without logging token URLs.

## 5. Enable the refresh timer

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now subctl-refresh.timer
systemctl list-timers subctl-refresh.timer
sudo journalctl -u subctl-refresh.service -n 50 --no-pager
```

The timer refreshes the provider and renders users under a shared lock. A
provider failure keeps the last valid cache and public feed.

## 6. Access the UI and Mihomo panel

```bash
ssh -N \
  -L 12790:127.0.0.1:12790 \
  -L 19090:127.0.0.1:19090 \
  admin@vps.example.com
```

Open `http://127.0.0.1:12790/` and, when Mihomo is enabled,
`http://127.0.0.1:19090/ui/`.

## Upgrade

```bash
cd /opt/subctl
git pull --ff-only
sudo -E SUBCTL_DOMAIN=sub.example.com ./deploy/install.sh
```

The installer preserves `/etc/subctl` and `/var/lib/subctl`. Check service
status and run the smoke checks after upgrading.
