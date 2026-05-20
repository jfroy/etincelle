# etincelle

Bootstrap utility server for the [flatops](https://github.com/jfroy/flatops) Kubernetes cluster. Runs Fedora image-mode (bootc), managed via GitOps — push to `main` builds and publishes a new OS image to ghcr.io.

## Services

| Service | Purpose |
|---------|---------|
| [Talos discovery service](https://github.com/siderolabs/discovery-service) | Node discovery for Talos cluster bootstrap (`ds.etincelle.cloud`) |
| [Talos image factory](https://github.com/siderolabs/image-factory) | Builds custom Talos OS images for cluster nodes (`tif.etincelle.cloud`) |
| [Distribution registry](https://github.com/distribution/distribution) | OCI registry used as image factory artifact cache (`registry.etincelle.cloud`) |
| [Caddy](https://caddyserver.com) | Reverse proxy with automatic TLS via Cloudflare DNS-01 |

## Secrets

Provisioned once post-install via `scripts/provision-secrets.sh`. Never committed to this repo:

- `/etc/image-factory/keys/` — Talos image factory signing keys
- `/etc/etincelle/secrets/caddy.env` — Cloudflare API token for ACME DNS challenge
