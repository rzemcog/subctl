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

mihomo:
  hosts: {}
  geox_urls: {}
  routing:
    direct_node_name_keywords: []
    direct_rules: []

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
  # Documentation-only addresses; replace them with the deployment resolvers.
  dns_nameservers: ["192.0.2.53", "198.51.100.53"]
```

`render.provider_download_proxy` is deprecated. It remains accepted when loading existing config and UI settings, but no longer changes generated fetch routes. Both profile types use the shared `FETCH-AUTO-PROVIDER` and `FETCH-PRIVATE` recovery groups. Keep existing values or remove them during config cleanup; new settings submissions omit this field.

The `mihomo` section is protected deployment configuration and cannot be changed through UI settings. `mihomo.hosts` maps exact DNS hostnames to unscoped IP addresses; validated entries are copied into generated profiles without replacing the URL hostname, HTTP Host, or TLS SNI.

User and gateway profiles share a policy topology with a LIVE HTTP provider and inline SEED snapshot. `PROVIDER-AUTO` prefers `PROVIDER-AUTO-LIVE`, then `PROVIDER-AUTO-SEED`. The user `BASE` defaults to `PROXY`; gateway `BASE` keeps `gateway.base_default`. Provider downloads use `FETCH-PRIVATE` (SEED, DIRECT) and `FETCH-AUTO-PROVIDER` (PRIVATE, SEED, DIRECT). The gateway keeps its configured direct upstream provider URL; user profiles use the published `/feeds/provider/...` source. The public LIVE provider sends a browser-like `User-Agent` header; the gateway source does not. Original node names remain visible without LIVE, SEED, or BOOTSTRAP prefixes, and fetch recovery does not add duplicate seed proxies to the top-level `proxies` list.

Set `mihomo.routing.direct_node_name_keywords` to classify provider nodes whose names belong in the `DIRECT-PROXY` selector. Set `mihomo.routing.direct_rules` to append operator-specific Mihomo rules before the final `MATCH,BASE` rule. Those values are not part of the UI settings overlay. With empty lists, no custom direct rules or node-name keywords are applied. Other LIVE nodes matching `render.provider_exclude_keywords` remain available in `EXCLUDED-NODES`; direct-node keywords take precedence when a name matches both classes. `PROXY` includes `DIRECT-PROXY` and `EXCLUDED-NODES`; `BASE` includes `DIRECT-PROXY`.

`render.provider_exclude_keywords` controls YAML selection and its SEED snapshot. It does not filter `.raw` output: when provider inclusion is enabled, `.raw` retains every node from the validated provider feed.

`mihomo.geox_urls` optionally maps `geoip`, `geosite`, `mmdb`, and `asn` to public source URLs. Configured URLs are copied into generated profiles and must not contain credentials, query parameters, or fragments; an empty mapping leaves source selection to Mihomo. Automatic geodata updates run every 24 hours. `geodata-mode: false` makes `GEOIP` rules use the full MMDB database instead of a lite DAT file. No binary geodata files are merged. User profiles enable TUN with `auto-route`, the `mips` stack, and MTU 1350. When the public endpoint hostname has an entry in protected `mihomo.hosts`, that mapped address is excluded from TUN routing; if `public.base_url` uses an IP literal, that endpoint address is excluded directly. No endpoint mapping or exclusion is added when the protected configuration does not provide one. Gateway TUN settings remain controlled by the gateway renderer and its explicit `enable_tun` option.

When provider inclusion is enabled, both renderers need a validated `provider.decoded` snapshot in the configured state cache to embed `provider-seed`. Run `subctl refresh-provider` before a first gateway render if that cache has not been populated.

The explicit `GLOBAL` group contains policy-group names only. It does not list physical provider nodes. Recovery groups are hidden from the Mihomo group picker.

The SEED converter currently accepts VLESS/TCP nodes using `none`, TLS, or Reality security, plus basic Trojan/TCP nodes with common SNI, ALPN, fingerprint, and `allowInsecure` fields. It fails the render without printing provider credentials if the cache contains a node format or parameter set it cannot faithfully represent. Add a converter and Mihomo validation before enabling other URI formats in the provider feed.

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

The Web UI and systemd refresh timer apply the same effective configuration. UI settings cannot change protected Mihomo policy and data sources, gateway routing, or credentials.
