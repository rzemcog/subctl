# subctl

`subctl` manages generated VPN subscription profiles for Mihomo-compatible
clients. It combines private subscriptions from 3x-ui with a shared provider
feed, renders YAML and raw Base64 outputs, and publishes them through Caddy or
the optional administration UI.

The project is designed for a self-hosted Debian-based VPS. Secrets stay in
protected files on the server; the repository contains only code, templates,
tests and safe examples.

## What it does

- validates provider and personal subscription feeds;
- atomically refreshes the shared provider cache;
- renders Mihomo YAML profiles and raw combined subscriptions;
- manages the YAML user registry through CLI or Web UI;
- records render status, provider refresh results and subscription fetch events;
- supports Caddy, systemd timers and an optional Mihomo gateway;
- provides a React/FastAPI administration UI available through an SSH tunnel.

## Architecture

```text
3x-ui personal URL ─┐
                    ├─ subctl registry + renderer ── YAML/raw files ── Caddy
provider feed URL ──┘             │                         │
                                  └─ provider cache         └─ VPN clients

SSH tunnel ── FastAPI/React Web UI
SSH tunnel ── Mihomo controller + MetaCubeXD UI (optional)
```

The provider source is refreshed and validated before publication. A failed
refresh preserves the last valid cache. A failed personal fetch preserves that
user's last valid raw file.

## Requirements

The supported deployment targets are Ubuntu 24.04 and Debian 12 with systemd.
Development requires:

- Python 3.10 or newer;
- Node.js 20.19+ (or 22.12+) and npm for the Web UI;
- Git and a POSIX shell.

Server deployment additionally requires Caddy. Mihomo and MetaCubeXD are
optional unless the gateway features are used. A working 3x-ui installation
or another provider that exposes compatible subscription URLs is required.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

cd web
npm ci
npm run build
cd ..

pytest
```

The frontend build writes package assets to `src/subctl/web_dist/`. Generated
assets, virtual environments and caches are ignored by Git.

## Local CLI example

The files in `examples/` contain no real credentials. Replace the placeholder
provider URL with a test endpoint before refreshing.

```bash
subctl --config examples/config.yaml \
  --users examples/users.yaml \
  --state-dir ./tmp/state \
  --output-dir ./tmp/public \
  list-users

subctl --config examples/config.yaml \
  --users examples/users.yaml \
  --state-dir ./tmp/state \
  --output-dir ./tmp/public \
  refresh-provider

subctl --config examples/config.yaml \
  --users examples/users.yaml \
  --state-dir ./tmp/state \
  --output-dir ./tmp/public \
  render --all
```

See [configuration](docs/configuration.md) for the protected config format.

## Deployment

The recommended workflow is manual configuration followed by the idempotent
installer/checker:

```bash
export SUBCTL_DOMAIN=sub.example.com
sudo ./deploy/install.sh
```

The installer does not create secrets and does not read credentials from Git.
Create `/etc/subctl/config.yaml` and the protected users registry first, or
follow the complete guide in [docs/deployment.md](docs/deployment.md).

The deployment templates use these public paths:

- `/s/<user-token>.yaml` — Mihomo YAML profile;
- `/s/<user-token>.raw` — raw Base64 subscription;
- `/feeds/provider/<shared-token>` — shared provider feed.

## Web UI and Mihomo panel

The Web UI listens on loopback only. Open it through a tunnel:

```bash
ssh -N \
  -L 12790:127.0.0.1:12790 \
  -L 19090:127.0.0.1:19090 \
  admin@vps.example.com
```

Then open:

- `http://127.0.0.1:12790/` — subctl administration UI;
- `http://127.0.0.1:19090/ui/` — Mihomo/MetaCubeXD panel.

The UI shows provider refresh duration and node counts, per-user render and
artifact status, last YAML/raw fetches, safe template settings, preview,
version history and rollback.

## Security model

- Keep provider upstream URLs, shared tokens, 3x-ui URLs and controller secrets
  outside the repository.
- Store `/etc/subctl/config.yaml` and the users registry with restrictive
  permissions, normally `0600` or `0640` as required by the service account.
- The Web UI binds to `127.0.0.1`; SSH is the access boundary.
- Tokenized subscription paths are not access-logged by Caddy or Uvicorn.
- UI settings are limited to safe render/composition overrides. Gateway secrets
  and routing rules remain protected configuration.
- Never paste production config, generated subscriptions or tokens into an
  issue, pull request or test fixture.

## Operations

Use [docs/operations.md](docs/operations.md) for refresh/render commands,
systemd logs, smoke checks, upgrades and rollback. For contribution rules see
[CONTRIBUTING.md](CONTRIBUTING.md); for vulnerability reports see
[SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
