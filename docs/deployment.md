# Production deployment and rollback

Production releases use one commit-bound release bundle. A release is the pair
of a full Git commit SHA and an immutable application wheel with its SHA256;
the bundle also retains the dependency wheels and matching systemd/Caddy files.
The package is built from a clean, already-pushed commit using `git archive`.
Rollback installs the saved bundle and never rebuilds an old commit.

## Confirmed production topology

On `ru-vps`:

```text
/root/subctl                 Git source/control checkout
/opt/subctl                  runtime parent; not a Git checkout
/opt/subctl/venv             installed Python runtime
/etc/subctl/config.yaml      protected production configuration
/var/lib/subctl/registry/    protected user registry
/var/lib/subctl/{cache,public,ui}/ generated runtime state
/etc/systemd/system/         active subctl service and timer files
/etc/caddy/Caddyfile         active rendered Caddy configuration
/var/lib/subctl/releases/    immutable release bundles and release records
```

`/var/lib/subctl/releases/<release-id>/` contains `manifest.json`, the
application wheel, a runtime dependency wheelhouse, three systemd snapshots,
and the rendered Caddyfile. The manifest records the full commit, package
filename and SHA256, all bundle file hashes, build time, and build environment.
The full commit and bundle SHA256 form the release ID. A deployment event and
`current.json` record the successful deployment or rollback time. `anchor.json`
identifies the initial fallback. These records and bundles are root-owned; the
bundle files are read-only and existing release IDs are never overwritten.

Configuration, user registry, cache, generated subscription files, UI state,
Mihomo data, secrets, TLS material, and other protected state are outside the
release bundle. The release installer does not copy or replace them.

## First rollback anchor

The first Release N anchor is based on commit
`780fe63d697230f728ec8f3c7584d65b360730f1`. This is the nearest reproducible
Git baseline to the production process that was running. It is **not proven to
be the exact revision loaded in that process**; the historical relationship is
recorded as `inferred` in the manifest and `anchor.json`.

Bootstrap stores the package built from that commit and captures the currently
active systemd/Caddy files after verifying that they match the selected
baseline. It does not install the anchor or restart a service. Run this once on
the confirmed host before the first new deployment:

```bash
ssh ru-vps
cd /root/subctl
SUBCTL_DOMAIN="$(/opt/subctl/venv/bin/python -c 'import yaml; from urllib.parse import urlsplit; print(urlsplit(yaml.safe_load(open("/etc/subctl/config.yaml"))["public"]["base_url"]).hostname)')"
sudo python3 deploy/release.py bootstrap \
  --commit 780fe63d697230f728ec8f3c7584d65b360730f1 \
  --domain "$SUBCTL_DOMAIN"
sudo python3 deploy/release.py status
```

If the active infrastructure files no longer match the baseline, bootstrap
stops before writing the anchor so the discrepancy can be reviewed.

## Official deploy workflow

Commit and push source changes before building a package. On the production
checkout, fetch the pushed main branch and inspect its state:

```bash
cd /root/subctl
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

Status keeps two revisions distinct: `/root/subctl` source checkout `HEAD` and
the release recorded in `/var/lib/subctl/releases/current.json`. Rollback
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
workflow for `ru-vps`. Production upgrades and rollbacks must use
`deploy/release.py` so the application package and infrastructure files come
from one verified immutable bundle.
