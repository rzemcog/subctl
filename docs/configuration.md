# Configuration

`subctl` reads a protected YAML configuration and a separate users registry.
Do not commit either production file.

## Main configuration

```yaml
provider:
  upstream_url: "https://provider.example/subscription"
  shared_token: "generated-url-safe-token"
  refresh_interval_seconds: 900

public:
  base_url: "https://sub.example.com"
  output_dir: "/var/lib/subctl/public"

render:
  profile_update_interval_seconds: 3600
  provider_update_interval_seconds: 900
  healthcheck_url: "https://www.gstatic.com/generate_204"
  healthcheck_interval_seconds: 15
  healthcheck_timeout_milliseconds: 3000
  healthcheck_max_failed_times: 2
  healthcheck_tolerance_milliseconds: 50
  healthcheck_lazy: true
  provider_exclude_keywords: ["test", "expired"]

# Optional gateway section, required only by render-gateway and Mihomo units.
gateway:
  private_upstream_url: "https://panel.example.com/sub/gateway-client"
  controller_secret: "generated-controller-secret"
  output_path: "/etc/mihomo/config.yaml"
  output_owner: "mihomo"
  output_group: "mihomo"
  socks_port: 10808
  controller_port: 19090
  base_default: "PROXY"
  physical_interface: "eth0"
  external_ui_path: "/var/lib/mihomo/ui/current"
  tun_output_path: "/etc/mihomo/config-tun.yaml"
  dns_nameservers: ["1.1.1.1", "8.8.8.8"]
```

`shared_token` and `controller_secret` must be generated per deployment. URL
values may contain credentials and must be treated as secrets. The upstream
provider URL is never embedded in generated user YAML; user profiles reference
the public provider feed instead.

Generate URL-safe values on the server:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

The gateway controller secret must be at least 32 characters.

## Users registry

```yaml
users:
  alice:
    token: "generated-url-safe-token"
    xui_subscription: "https://panel.example.com/sub/alice"
```

Prefer the CLI for registry changes:

```bash
subctl --users /var/lib/subctl/registry/users.yaml \
  add-user --name alice --xui-sub-url https://panel.example.com/sub/alice
```

The Web UI uses the same registry and keeps token rotation atomic. Rotating a
token invalidates the previous YAML/raw paths.

## UI settings override

Published safe UI settings are stored in
`/var/lib/subctl/ui/settings.yaml`. They can change render intervals,
health-check parameters, provider exclusions, composition order and prefixes.
They cannot replace secrets, gateway routing rules or the protected registry.

The Web UI and systemd refresh timer apply the same effective configuration.
