# Security policy

## Scope

subctl handles subscription URLs and generated VPN profiles. A deployment may
also contain provider shared tokens, user tokens and Mihomo controller
secrets. Treat all of them as credentials.

## Do not disclose secrets

Never commit or paste production config, users registries, generated YAML/raw
files, provider cache, controller secrets or private keys. If a credential was
committed, rotate it immediately and remove the exposed data from Git history.

## Reporting

For a suspected vulnerability, contact the repository maintainers privately
before opening a public issue. Include a minimal reproduction, affected
version and impact. Do not include live tokens or subscription URLs.

## Supported versions

Security fixes target the latest `main` revision. Operators should keep the
Python dependencies, Caddy and Mihomo binary updated independently.
