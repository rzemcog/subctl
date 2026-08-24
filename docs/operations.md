# Operations

## Manual refresh and render

```bash
subctl --config /etc/subctl/config.yaml \
  --users /var/lib/subctl/registry/users.yaml \
  --state-dir /var/lib/subctl \
  --output-dir /var/lib/subctl/public refresh-provider

subctl --config /etc/subctl/config.yaml \
  --users /var/lib/subctl/registry/users.yaml \
  --state-dir /var/lib/subctl \
  --output-dir /var/lib/subctl/public render --all
```

Use `render --user <name>` for a single user. `Перегенерировать` in the Web UI
uses the current provider cache; `Обновить provider` downloads, validates and
publishes a new provider before rendering all users.

## Logs and state

```bash
systemctl status subctl-web.service subctl-refresh.timer
journalctl -u subctl-web.service -n 100 --no-pager
journalctl -u subctl-refresh.service -n 100 --no-pager
```

State is kept under `/var/lib/subctl`:

- `cache/` — validated provider cache;
- `public/` — generated files served by Caddy;
- `registry/` — production users registry;
- `ui/` — jobs, settings versions, provider status and fetch telemetry.

## Health checks

```bash
SUBCTL_CONFIG=/etc/subctl/config.yaml \
SUBCTL_USERS=/var/lib/subctl/registry/users.yaml \
python3 scripts/smoke-caddy.py

SUBCTL_CONFIG=/etc/subctl/config.yaml \
python3 scripts/healthcheck-mihomo-ui.py
```

For a complete local generation check:

```bash
SUBCTL_CONFIG=/etc/subctl/config.yaml \
SUBCTL_USERS=/var/lib/subctl/registry/users.yaml \
SUBCTL_STATE_DIR=/var/lib/subctl \
SUBCTL_OUTPUT_DIR=/var/lib/subctl/public \
SUBCTL_BIN=/opt/subctl/venv/bin/subctl \
scripts/smoke-real-subscriptions.sh
```

Checks never print subscription URLs or token values. Generated smoke notes
are stored with restrictive permissions under the state directory.

## Rollback and recovery

Provider refresh is transactional. If upstream fetch, validation or publication
fails, the previous valid cache and public feed remain in place. If a render
fails for one user, that user's last valid artifact remains available.

For a bad application release, stop the timer, install the previous package
version, restart the Web UI and run a full render:

```bash
sudo systemctl disable --now subctl-refresh.timer
/opt/subctl/venv/bin/pip install /path/to/previous/subctl
sudo systemctl restart subctl-web.service
sudo systemctl start subctl-refresh.service
sudo systemctl enable --now subctl-refresh.timer
```

Do not restore secrets from Git. Restore protected configuration and registry
files from an external secret-managed backup only.
