# etincelle: bootc GitOps Design

**Date:** 2026-05-19
**Status:** Approved

## Overview

Convert the etincelle utility VM (Fedora CoreOS, podman/quadlets) to Fedora image-mode (bootc). Replace nginx proxy manager with Caddy for gitops-compatible reverse proxy management. All configuration lives in git; secrets provisioned once post-install over SSH.

## Repo Structure

```
etincelle/
├── Containerfile                      # bootc OS image
├── caddy/
│   ├── Containerfile                  # custom Caddy + cloudflare DNS plugin
│   └── Caddyfile                      # reverse proxy config
├── containers/
│   └── systemd/
│       ├── caddy.container
│       ├── discovery-service.container
│       ├── image-factory.container
│       └── registry.container
├── image-factory/
│   └── config.yaml                    # no keys — provisioned post-install
├── registry/
│   └── config.yml
├── scripts/
│   └── provision-secrets.sh           # post-install secret placement helper
└── .github/
    └── workflows/
        ├── bootc.yml                  # build + push OS image
        └── caddy.yml                  # build + push Caddy image
```

## Images

### OS Image — `ghcr.io/jfroy/etincelle`

Base: `quay.io/fedora/fedora-bootc:42`

**`Containerfile`:**
```dockerfile
FROM quay.io/fedora/fedora-bootc:42

COPY containers/systemd/ /etc/containers/systemd/
COPY caddy/Caddyfile /etc/caddy/Caddyfile
COPY image-factory/config.yaml /etc/image-factory/config.yaml
COPY registry/config.yml /etc/registry/config.yml

RUN mkdir -p /etc/etincelle/secrets && chmod 700 /etc/etincelle/secrets

RUN systemctl enable bootc-fetch-apply-updates.timer podman-auto-update.timer

RUN echo "etincelle" > /etc/hostname
```

Bakes in: quadlet files, Caddyfile, image-factory config, registry config, hostname, enabled systemd timers.

Does not bake in: image-factory PEM keys, Cloudflare API token.

### Caddy Image — `ghcr.io/jfroy/caddy-etincelle`

Two-stage build using xcaddy to add the Cloudflare DNS plugin.

**`caddy/Containerfile`:**
```dockerfile
FROM caddy:builder AS builder
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:latest
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
```

## Reverse Proxy (Caddy)

**`caddy/Caddyfile`:**
```caddyfile
{
    acme_dns cloudflare {env.CLOUDFLARE_API_TOKEN}
}

ds.etincelle.cloud {
    reverse_proxy h2c://localhost:4000
}

registry.etincelle.cloud {
    reverse_proxy localhost:5000
}

tif.etincelle.cloud {
    reverse_proxy localhost:8080
}
```

- `ds.etincelle.cloud` → discovery-service gRPC on :4000 via `h2c://` (unencrypted HTTP/2; Caddy terminates TLS)
- `registry.etincelle.cloud` → container registry on :5000
- `tif.etincelle.cloud` → image-factory on :8080

TLS certificates issued automatically via Let's Encrypt DNS-01 challenge using the Cloudflare API. No port 80 challenge needed.

**`containers/systemd/caddy.container`:**
```ini
[Unit]
Description=Caddy reverse proxy
After=network-online.target
Wants=network-online.target

[Container]
AutoUpdate=registry
ContainerName=caddy
Image=ghcr.io/jfroy/caddy-etincelle:latest
Network=host
Volume=/etc/caddy/Caddyfile:/etc/caddy/Caddyfile:ro,z
Volume=/var/caddy/data:/data:rw,U,Z
Volume=/var/caddy/config:/config:rw,U,Z
EnvironmentFile=/etc/etincelle/secrets/caddy.env

[Service]
Restart=always

[Install]
WantedBy=multi-user.target
```

## Secrets Management

Secrets never enter the git repo or the built image. Provisioned once post-install over SSH.

**`/etc/etincelle/secrets/caddy.env`** (chmod 600):
```
CLOUDFLARE_API_TOKEN=<token>
```

**`/etc/image-factory/keys/`** (chmod 640):
- `image-factory.pem`
- `pcr-signing-key.pem`
- `uki-signing-cert.pem`
- `uki-signing-key.pem`

**`scripts/provision-secrets.sh`** documents and automates placement:
```bash
#!/usr/bin/env bash
set -euo pipefail
HOST="${1:?usage: provision-secrets.sh <host>}"
ssh "core@${HOST}" sudo mkdir -p /etc/image-factory/keys
scp image-factory/keys/*.pem "core@${HOST}:/tmp/"
ssh "core@${HOST}" "sudo mv /tmp/*.pem /etc/image-factory/keys/ && sudo chmod 640 /etc/image-factory/keys/*"
ssh "core@${HOST}" "read -rsp 'Cloudflare API token: ' tok && printf 'CLOUDFLARE_API_TOKEN=%s\n' \"\$tok\" | sudo tee /etc/etincelle/secrets/caddy.env > /dev/null && sudo chmod 600 /etc/etincelle/secrets/caddy.env"
```

## CI/CD — GitHub Actions

Both workflows follow the same pattern: parallel native builds, manifest merge, cosign signing.

### Build Pattern

```
jobs:
  build:
    strategy:
      matrix:
        include:
          - platform: linux/amd64
            runner: ubuntu-24.04
          - platform: linux/arm64
            runner: ubuntu-24.04-arm
    runs-on: ${{ matrix.runner }}
    outputs:
      digest: ${{ steps.push.outputs.digest }}
    steps:
      - Build image for single platform
      - Push to ghcr.io by digest (no tag)
      - Output digest

  merge:
    needs: build
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
      id-token: write        # required for keyless cosign OIDC
    steps:
      - docker buildx imagetools create --tag ghcr.io/jfroy/<image>:latest <amd64-digest> <arm64-digest>
      - cosign sign --yes ghcr.io/jfroy/<image>@<merged-digest>
```

### Triggers

- `bootc.yml`: paths `Containerfile`, `containers/**`, `caddy/Caddyfile`, `image-factory/config.yaml`, `registry/config.yml`
- `caddy.yml`: paths `caddy/Containerfile`

### Image Signing

Keyless cosign via Sigstore OIDC (GitHub Actions identity). No cosign key to manage. Signatures stored in Rekor transparency log. Verification identity tied to `https://github.com/jfroy/etincelle` + workflow ref.

Container policy (`containers/policy.json`) updated to require cosign signature verification for `ghcr.io/jfroy/*` images. `bootc` and `podman` both enforce policy before running images.

## Quadlet Updates Required

Existing quadlets mount config from `/var/...` (Butane-provisioned paths). With bootc, config moves to `/etc/...` (image-owned). Volume paths in `image-factory.container` and `registry.container` must be updated:

- `image-factory.container`: `/var/image-factory/config.yaml` → `/etc/image-factory/config.yaml`; `/var/image-factory/keys` → `/etc/image-factory/keys`
- `registry.container`: `/var/registry/config.yml` → `/etc/registry/config.yml`

Persistent data volumes (`/var/image-factory/tmp`, `/var/registry/data`) stay in `/var/` — bootc never touches `/var/`.

`discovery-service.container` data volume (`/var/discovery-service`) is already pure data — no change needed.

## Auto-Update

| Component | Mechanism | Trigger |
|-----------|-----------|---------|
| OS image | `bootc-fetch-apply-updates.timer` | Periodic pull from ghcr.io |
| Containers | `podman-auto-update.timer` | Periodic; `AutoUpdate=registry` in each quadlet |

Both timers enabled in the OS image. VM reboots after OS image update (bootc behavior). Container updates are in-place restarts.
