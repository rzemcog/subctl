# Production deployment and rollback

Production releases use one commit-bound release bundle. A release is the pair
of a full Git commit SHA and an immutable application wheel with its SHA256;
the bundle also retains the dependency wheels and matching systemd/Caddy files.
The package is built from a clean, already-pushed commit using `git archive`.
Rollback installs the saved bundle and never rebuilds an old commit.

## Runtime layout

The source checkout location and SSH target are selected by each operator and
belong in private local configuration. Set `SUBCTL_SOURCE_DIR` to the checkout
path and `SUBCTL_SSH_TARGET` to a destination from the local SSH config before
using the remote commands below. The installer uses these standard service
locations on the target host:

```text
/opt/subctl/venv             installed Python runtime
/etc/subctl/config.yaml      protected deployment configuration
/var/lib/subctl/registry/    protected user registry
/var/lib/subctl/{cache,public,ui}/ generated runtime state
/etc/systemd/system/         installed service and timer files
/etc/caddy/Caddyfile         rendered Caddy configuration
/var/lib/subctl/releases/    immutable release bundles and records
```

`/var/lib/subctl/releases/<release-id>/` contains `manifest.json`, the
application wheel, a runtime dependency wheelhouse, service snapshots, and the
rendered Caddyfile. The manifest records the full commit, package filename and
SHA256, all bundle file hashes, build time, and build environment. The full
commit and bundle SHA256 form the release ID. A deployment event and
`current.json` record the successful deployment or rollback time. `anchor.json`
identifies the initial fallback. These records and bundles are root-owned; the
bundle files are read-only and existing release IDs are never overwritten.

Configuration, user registry, cache, generated subscription files, UI state,
Mihomo data, secrets, TLS material, and other protected state are outside the
release bundle. The release installer does not copy or replace them.

## First rollback anchor

Choose a full commit SHA whose tracked service and Caddy files match the active
files on the target host. Bootstrap verifies that match before it writes the
anchor or stores a fallback bundle. The anchor records the historical runtime
relationship as `inferred`; it does not claim to prove the exact source that a
previously running process loaded.

Before connecting, set `SUBCTL_SSH_TARGET` in a private local shell using your
SSH config. After connecting, set `SUBCTL_SOURCE_DIR` in the target shell to the
checkout path; keep both values out of the public repository. The public domain
is read from protected configuration on the target host:

```bash
ssh "$SUBCTL_SSH_TARGET"
cd "$SUBCTL_SOURCE_DIR"
SUBCTL_DOMAIN="$(/opt/subctl/venv/bin/python -c 'import yaml; from urllib.parse import urlsplit; print(urlsplit(yaml.safe_load(open("/etc/subctl/config.yaml"))["public"]["base_url"]).hostname)')"
sudo python3 deploy/release.py bootstrap \
  --commit "$(git rev-parse HEAD)" \
  --domain "$SUBCTL_DOMAIN"
sudo python3 deploy/release.py status
```

If the active infrastructure files no longer match the selected commit,
bootstrap stops before writing the anchor so the discrepancy can be reviewed.

## Official deploy workflow

Commit and push source changes before building a package. On the production
checkout, fetch the pushed main branch and inspect its state:

```bash
cd "$SUBCTL_SOURCE_DIR"
SUBCTL_DOMAIN="$(/opt/subctl/venv/bin/python -c 'import yaml; from urllib.parse import urlsplit; print(urlsplit(yaml.safe_load(open("/etc/subctl/config.yaml"))["public"]["base_url"]).hostname)')"
git fetch origin main
git status --short --branch
git merge --ff-only origin/main
git status --porcelain --untracked-files=all
git rev-parse HEAD
```

The final `git status` must be empty. `build` also rejects a dirty checkout, a
commit that is not `HEAD`, or a commit not reachable from `origin/main`.

Build from that exact pushed commit and deploy the release ID printed by the
builder:

```bash
sudo python3 deploy/release.py build \
  --commit "$(git rev-parse HEAD)" \
  --domain "$SUBCTL_DOMAIN"
sudo python3 deploy/release.py status
sudo python3 deploy/release.py deploy --release-id <release-id>
sudo python3 deploy/release.py status
```

The helper serializes release operations, validates the target and current
fallback hashes, pauses the refresh timer and web service, then calls the
release mode of `deploy/install.sh`. That mode installs the saved wheels with
`pip --no-index`, validates and applies the matching systemd/Caddy snapshots,
reloads systemd and Caddy, and checks `pip check`. The helper explicitly
restarts `subctl-web.service`, checks `/`, `/health`, and `/healthz`, runs
`subctl-refresh.service` once, runs the public subscription smoke check, resumes
`subctl-refresh.timer`, and only then writes `current.json` and the success
event. The refresh unit's successful completion confirms both provider refresh
and rendering succeeded. The smoke process keeps the configured public URL,
HTTP Host, and TLS SNI while resolving that hostname to `127.0.0.1` inside the
check process. This exercises Caddy and every provider and user subscription
route without relying on the VPS network's public-IP hairpin route. An external
probe is needed to assess DNS and network reachability from client networks.

`systemctl enable --now subctl-web.service` does **not** restart an already
running web process. Production releases always issue an explicit restart.

If installation or verification fails, the helper applies the retained
fallback package, dependencies, units, and Caddy snapshot and verifies that
release. It records the fallback as active only after recovery checks pass. If
fallback recovery also fails, it leaves the web service stopped and preserves
the last verified active record for operator recovery.

## Status and rollback

Status keeps the source checkout `HEAD` distinct from the release recorded in
`/var/lib/subctl/releases/current.json`. Rollback
intentionally leaves the source checkout on the current release-control
branch; the old release's commit remains the provenance/audit anchor while the
saved package is installed.

```bash
sudo python3 deploy/release.py status
sudo python3 deploy/release.py rollback --release-id <saved-release-id>
sudo python3 deploy/release.py status
```

Rollback verifies the release manifest, all bundle hashes, and that the full
commit exists in the local Git object database. It installs the stored package
and dependency wheels without rebuilding source, restores that bundle's
systemd/Caddy files, explicitly restarts the web service, and runs health and
public smoke checks. A failed rollback automatically restores the previously
active release. Never run `pip install` from a guessed path or restore
application code from a working tree as a production rollback.

## Fresh installation

The ordinary installer remains for a new host after protected configuration
has been prepared. It builds from the checked-out source and installs the
initial services:

```bash
export SUBCTL_DOMAIN=sub.example.com
sudo -E ./deploy/install.sh
```

This is a bootstrap/fresh-install path, not the production upgrade or rollback
workflow for an existing deployment. Production upgrades and rollbacks must use
`deploy/release.py` so the application package and infrastructure files come
from one verified immutable bundle.
