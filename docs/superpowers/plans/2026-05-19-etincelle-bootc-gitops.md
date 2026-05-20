# etincelle bootc GitOps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the etincelle repo into a bootc image-mode Fedora gitops system with Caddy replacing nginx proxy manager, multi-arch images signed with keyless cosign, and secrets provisioned post-install.

**Architecture:** A root `Containerfile` builds the OS image (pushed to ghcr.io/jfroy/etincelle). A separate `caddy/Containerfile` builds a custom Caddy image with the Cloudflare DNS plugin (pushed to ghcr.io/jfroy/caddy-etincelle). GitHub Actions builds both images on native amd64 and arm64 runners in parallel, merges into a multi-arch manifest, and signs with keyless cosign. The VM runs `bootc-fetch-apply-updates.timer` for OS auto-update and `podman-auto-update.timer` for container auto-update.

**Tech Stack:** Fedora bootc 42, Podman 5.x quadlets, Caddy 2.x + caddy-dns/cloudflare, xcaddy, cosign (keyless/Sigstore), GitHub Actions, docker/build-push-action, ghcr.io

---

## File Map

| Path | Action | Purpose |
|------|--------|---------|
| `Containerfile` | Create | bootc OS image |
| `.gitignore` | Create | Exclude image-factory keys |
| `caddy/Containerfile` | Create | Caddy image with cloudflare plugin |
| `caddy/Caddyfile` | Create | Reverse proxy config |
| `containers/policy.json` | Modify | Add sigstoreSigned entries for ghcr.io/jfroy |
| `containers/systemd/caddy.container` | Create | Caddy quadlet |
| `containers/systemd/discovery-service.container` | Modify | Minor cleanup |
| `containers/systemd/image-factory.container` | Modify | Volume paths /var→/etc, drop Pull=always |
| `containers/systemd/registry.container` | Modify | Volume path /var→/etc, drop User=root |
| `containers/systemd/nginx-proxy-manager.container` | Delete | Replaced by caddy |
| `image-factory/keys/*.pem` | Delete + gitignore | Secrets must not be in repo |
| `scripts/provision-secrets.sh` | Create | Post-install secret placement helper |
| `.github/workflows/caddy.yml` | Create | Caddy image CI/CD |
| `.github/workflows/bootc.yml` | Create | OS image CI/CD |
| `etincelle.bu` | Delete | Replaced by bootc |
| `nginx-proxy-manager/` | Delete | Replaced by caddy |
| `containers/toolbox.conf` | Delete | Not needed on server |

---

## Task 1: Remove Obsolete Files and Add .gitignore

**Files:**
- Delete: `etincelle.bu`, `nginx-proxy-manager/`, `containers/toolbox.conf`, `containers/systemd/nginx-proxy-manager.container`, `image-factory/keys/*.pem`
- Create: `.gitignore`

- [ ] **Step 1: Delete obsolete files**

```bash
rm etincelle.bu
rm -rf nginx-proxy-manager/
rm containers/toolbox.conf
rm containers/systemd/nginx-proxy-manager.container
rm image-factory/keys/image-factory.pem \
   image-factory/keys/pcr-signing-key.pem \
   image-factory/keys/uki-signing-cert.pem \
   image-factory/keys/uki-signing-key.pem
rmdir image-factory/keys
```

- [ ] **Step 2: Create .gitignore**

```
# image-factory PEM keys — provisioned post-install, never committed
image-factory/keys/
```

Save as `.gitignore` at repo root.

- [ ] **Step 3: Verify no PEM files tracked**

```bash
git status
```

Expected: deleted files shown as staged for removal, `image-factory/keys/` listed as untracked (not shown if directory is now gone).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: remove obsolete files and add .gitignore for secrets"
```

---

## Task 2: Caddy Image

**Files:**
- Create: `caddy/Containerfile`
- Create: `caddy/Caddyfile`

- [ ] **Step 1: Create caddy/Containerfile**

```dockerfile
FROM caddy:builder AS builder
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:latest
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
```

- [ ] **Step 2: Create caddy/Caddyfile**

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

Notes:
- `h2c://` on port 4000: discovery-service speaks gRPC (unencrypted HTTP/2) to the local backend; Caddy terminates TLS externally.
- Port 5000: container registry.
- Port 8080: image-factory HTTP API.

- [ ] **Step 3: Build the Caddy image locally to verify it compiles**

```bash
docker build -t caddy-etincelle-test -f caddy/Containerfile caddy/
```

Expected: successful multi-stage build, no errors. xcaddy compiles caddy with the cloudflare module — this takes 1-3 minutes.

- [ ] **Step 4: Validate Caddyfile syntax**

```bash
docker run --rm \
  -v "$(pwd)/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" \
  caddy-etincelle-test \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

Expected: `Valid configuration` or similar success message. The `acme_dns cloudflare` block will load the module — since CLOUDFLARE_API_TOKEN isn't set, the validate step may warn but should not fail on syntax.

- [ ] **Step 5: Commit**

```bash
git add caddy/
git commit -m "feat: add Caddy image with Cloudflare DNS plugin and Caddyfile"
```

---

## Task 3: Update Quadlet Container Files

**Files:**
- Create: `containers/systemd/caddy.container`
- Modify: `containers/systemd/discovery-service.container`
- Modify: `containers/systemd/image-factory.container`
- Modify: `containers/systemd/registry.container`

- [ ] **Step 1: Create containers/systemd/caddy.container**

```ini
[Unit]
Description=Caddy reverse proxy
After=network-online.target discovery-service.service registry.service image-factory.service
Wants=network-online.target

[Container]
AutoUpdate=registry
ContainerName=caddy
Image=ghcr.io/jfroy/caddy-etincelle:latest
Network=host
Volume=/etc/caddy/Caddyfile:/etc/caddy/Caddyfile:ro,z
Volume=/var/caddy/data:/data:rw,Z
Volume=/var/caddy/config:/config:rw,Z
EnvironmentFile=/etc/etincelle/secrets/caddy.env

[Service]
Restart=always

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Replace containers/systemd/discovery-service.container**

```ini
[Unit]
Description=Talos discovery service
After=network-online.target
Wants=network-online.target

[Container]
AutoUpdate=registry
ContainerName=discovery-service
Image=ghcr.io/siderolabs/discovery-service:latest
Network=host
Volume=/var/discovery-service:/var/discovery-service:rw,Z
Exec=-addr :4000 -landing-addr :4001 -metrics-addr :4002

[Service]
Restart=always

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: Replace containers/systemd/image-factory.container**

Config and keys mount from `/etc/image-factory/` (image-owned path, populated by Containerfile + post-install provisioning). Temp dir stays in `/var/` (persistent data). Added `Requires=registry.service` because image-factory uses the local registry on `:5000` for OCI artifact caching.

```ini
[Unit]
Description=Talos image factory
After=network-online.target registry.service
Wants=network-online.target
Requires=registry.service

[Container]
AutoUpdate=registry
ContainerName=image-factory
Image=ghcr.io/jfroy/siderolabs/image-factory:latest
Network=host
Volume=/etc/image-factory/config.yaml:/config.yaml:ro,z
Volume=/etc/image-factory/keys:/keys:ro,z
Volume=/var/image-factory/tmp:/tmp:rw,Z
Exec=--config /config.yaml

[Service]
Restart=always

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Replace containers/systemd/registry.container**

Config mounts from `/etc/registry/` (image-owned). Data stays in `/var/registry/data` (persistent). Removed `User=root` (unnecessary with proper SELinux labels; the registry image defaults to an appropriate user).

```ini
[Unit]
Description=Container image registry
After=network-online.target
Wants=network-online.target

[Container]
AutoUpdate=registry
ContainerName=registry
Environment=OTEL_TRACES_EXPORTER=none
Image=docker.io/library/registry:3.1.1
Network=host
Volume=/var/registry/data:/var/lib/registry:rw,Z
Volume=/etc/registry/config.yml:/etc/distribution/config.yml:ro,z

[Service]
Restart=always

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 5: Verify quadlet file syntax**

Podman's quadlet generator validates unit files. Run on the host (if podman available) or simply check for obvious issues:

```bash
find containers/systemd/ -name '*.container' -exec grep -l '\[Container\]' {} \;
```

Expected: all 4 files listed.

- [ ] **Step 6: Commit**

```bash
git add containers/systemd/
git commit -m "feat: update quadlets — add caddy, update volume paths, drop nginx proxy manager"
```

---

## Task 4: Update Container Policy for Cosign Verification

**Files:**
- Modify: `containers/policy.json`

The current policy accepts anything. Add `sigstoreSigned` entries for `ghcr.io/jfroy/etincelle` (signed by bootc.yml) and `ghcr.io/jfroy/caddy-etincelle` (signed by caddy.yml). Fedora 42 ships the Sigstore root CA and Rekor public key at the paths below.

- [ ] **Step 1: Replace containers/policy.json**

```json
{
    "default": [
        {
            "type": "insecureAcceptAnything"
        }
    ],
    "transports": {
        "docker": {
            "ghcr.io/jfroy/etincelle": [
                {
                    "type": "sigstoreSigned",
                    "fulcio": {
                        "caPath": "/etc/pki/sigstore/roots/fulcio_v1.crt.pem",
                        "oidcIssuer": "https://token.actions.githubusercontent.com",
                        "subjectEmail": "https://github.com/jfroy/etincelle/.github/workflows/bootc.yml@refs/heads/main"
                    },
                    "rekorPublicKeyPath": "/etc/pki/sigstore/roots/rekor.pub",
                    "signedIdentity": {
                        "type": "matchRepository"
                    }
                }
            ],
            "ghcr.io/jfroy/caddy-etincelle": [
                {
                    "type": "sigstoreSigned",
                    "fulcio": {
                        "caPath": "/etc/pki/sigstore/roots/fulcio_v1.crt.pem",
                        "oidcIssuer": "https://token.actions.githubusercontent.com",
                        "subjectEmail": "https://github.com/jfroy/etincelle/.github/workflows/caddy.yml@refs/heads/main"
                    },
                    "rekorPublicKeyPath": "/etc/pki/sigstore/roots/rekor.pub",
                    "signedIdentity": {
                        "type": "matchRepository"
                    }
                }
            ]
        },
        "docker-daemon": {
            "": [{"type": "insecureAcceptAnything"}]
        }
    }
}
```

Note: `subjectEmail` here holds a URI SAN value (the GitHub Actions workflow identity). Fedora 42's containers/image library accepts URI values in this field for keyless Sigstore signatures. Verify this works on first deployment with `podman pull --policy /etc/containers/policy.json ghcr.io/jfroy/etincelle:latest`.

- [ ] **Step 2: Validate JSON syntax**

```bash
jq . containers/policy.json
```

Expected: pretty-printed JSON output, no errors.

- [ ] **Step 3: Commit**

```bash
git add containers/policy.json
git commit -m "feat: add sigstoreSigned policy for ghcr.io/jfroy images"
```

---

## Task 5: bootc OS Containerfile

**Files:**
- Create: `Containerfile`

- [ ] **Step 1: Create Containerfile**

```dockerfile
FROM quay.io/fedora/fedora-bootc:42

# Container runtime configuration
COPY containers/policy.json /etc/containers/policy.json
COPY containers/registries.conf /etc/containers/registries.conf
COPY containers/registries.conf.d/ /etc/containers/registries.conf.d/
COPY containers/registries.d/ /etc/containers/registries.d/

# Quadlet service definitions
COPY containers/systemd/ /etc/containers/systemd/

# Service configurations (managed via git, baked into image)
COPY caddy/Caddyfile /etc/caddy/Caddyfile
COPY image-factory/config.yaml /etc/image-factory/config.yaml
COPY registry/config.yml /etc/registry/config.yml

# Secrets directories (contents provisioned post-install, never in image)
RUN mkdir -p /etc/etincelle/secrets && chmod 700 /etc/etincelle/secrets && \
    mkdir -p /etc/image-factory/keys && chmod 750 /etc/image-factory/keys

# Enable auto-update timers
RUN systemctl enable bootc-fetch-apply-updates.timer podman-auto-update.timer

RUN echo "etincelle" > /etc/hostname
```

- [ ] **Step 2: Build the OS image locally on amd64 to verify**

```bash
docker build -t etincelle-test .
```

Expected: successful build. All COPY commands resolve. `systemctl enable` succeeds (fedora-bootc base has systemd). No errors.

If `docker` is not available, use `podman build -t etincelle-test .`.

- [ ] **Step 3: Spot-check baked files in the image**

```bash
docker run --rm etincelle-test ls /etc/containers/systemd/
docker run --rm etincelle-test ls /etc/caddy/
docker run --rm etincelle-test cat /etc/hostname
```

Expected:
- `caddy.container discovery-service.container image-factory.container registry.container`
- `Caddyfile`
- `etincelle`

- [ ] **Step 4: Commit**

```bash
git add Containerfile
git commit -m "feat: add bootc OS Containerfile"
```

---

## Task 6: GitHub Actions — Caddy Workflow

**Files:**
- Create: `.github/workflows/caddy.yml`

Pattern: two parallel native-arch build jobs → artifact-based digest collection → manifest merge job → cosign sign.

- [ ] **Step 1: Create .github/workflows/caddy.yml**

```yaml
name: Caddy Image

on:
  push:
    branches: [main]
    paths:
      - caddy/Containerfile

env:
  IMAGE: ghcr.io/jfroy/caddy-etincelle

jobs:
  build:
    strategy:
      fail-fast: false
      matrix:
        include:
          - platform: linux/amd64
            runner: ubuntu-24.04
          - platform: linux/arm64
            runner: ubuntu-24.04-arm
    runs-on: ${{ matrix.runner }}
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - name: Set platform env
        run: |
          platform=${{ matrix.platform }}
          echo "PLATFORM_PAIR=${platform//\//-}" >> "$GITHUB_ENV"

      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - uses: docker/setup-buildx-action@v3

      - name: Build and push by digest
        id: build
        uses: docker/build-push-action@v6
        with:
          context: caddy
          file: caddy/Containerfile
          platforms: ${{ matrix.platform }}
          push: true
          outputs: type=image,name=${{ env.IMAGE }},push-by-digest=true,name-canonical=true

      - name: Export digest
        run: |
          mkdir -p /tmp/digests
          digest="${{ steps.build.outputs.digest }}"
          touch "/tmp/digests/${digest#sha256:}"

      - uses: actions/upload-artifact@v4
        with:
          name: digests-caddy-${{ env.PLATFORM_PAIR }}
          path: /tmp/digests/*
          if-no-files-found: error
          retention-days: 1

  merge:
    needs: build
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
      id-token: write
    steps:
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - uses: actions/download-artifact@v4
        with:
          path: /tmp/digests
          pattern: digests-caddy-*
          merge-multiple: true

      - uses: docker/setup-buildx-action@v3

      - name: Create and push manifest list
        working-directory: /tmp/digests
        run: |
          docker buildx imagetools create \
            --tag ${{ env.IMAGE }}:latest \
            $(printf '${{ env.IMAGE }}@sha256:%s ' *)

      - uses: sigstore/cosign-installer@v3

      - name: Sign manifest
        run: |
          DIGEST=$(docker buildx imagetools inspect ${{ env.IMAGE }}:latest \
            --format '{{.Manifest.Digest}}')
          cosign sign --yes "${{ env.IMAGE }}@${DIGEST}"
```

- [ ] **Step 2: Verify YAML syntax**

```bash
python3 -c "import yaml, sys; yaml.safe_load(open('.github/workflows/caddy.yml'))" && echo "OK"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/caddy.yml
git commit -m "feat: add GitHub Actions workflow for Caddy image"
```

---

## Task 7: GitHub Actions — bootc OS Workflow

**Files:**
- Create: `.github/workflows/bootc.yml`

Same pattern as caddy.yml but triggers on OS image source files and builds the root Containerfile.

- [ ] **Step 1: Create .github/workflows/bootc.yml**

```yaml
name: bootc OS Image

on:
  push:
    branches: [main]
    paths:
      - Containerfile
      - containers/**
      - caddy/Caddyfile
      - image-factory/config.yaml
      - registry/config.yml

env:
  IMAGE: ghcr.io/jfroy/etincelle

jobs:
  build:
    strategy:
      fail-fast: false
      matrix:
        include:
          - platform: linux/amd64
            runner: ubuntu-24.04
          - platform: linux/arm64
            runner: ubuntu-24.04-arm
    runs-on: ${{ matrix.runner }}
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - name: Set platform env
        run: |
          platform=${{ matrix.platform }}
          echo "PLATFORM_PAIR=${platform//\//-}" >> "$GITHUB_ENV"

      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - uses: docker/setup-buildx-action@v3

      - name: Build and push by digest
        id: build
        uses: docker/build-push-action@v6
        with:
          context: .
          file: Containerfile
          platforms: ${{ matrix.platform }}
          push: true
          outputs: type=image,name=${{ env.IMAGE }},push-by-digest=true,name-canonical=true

      - name: Export digest
        run: |
          mkdir -p /tmp/digests
          digest="${{ steps.build.outputs.digest }}"
          touch "/tmp/digests/${digest#sha256:}"

      - uses: actions/upload-artifact@v4
        with:
          name: digests-bootc-${{ env.PLATFORM_PAIR }}
          path: /tmp/digests/*
          if-no-files-found: error
          retention-days: 1

  merge:
    needs: build
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
      id-token: write
    steps:
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - uses: actions/download-artifact@v4
        with:
          path: /tmp/digests
          pattern: digests-bootc-*
          merge-multiple: true

      - uses: docker/setup-buildx-action@v3

      - name: Create and push manifest list
        working-directory: /tmp/digests
        run: |
          docker buildx imagetools create \
            --tag ${{ env.IMAGE }}:latest \
            $(printf '${{ env.IMAGE }}@sha256:%s ' *)

      - uses: sigstore/cosign-installer@v3

      - name: Sign manifest
        run: |
          DIGEST=$(docker buildx imagetools inspect ${{ env.IMAGE }}:latest \
            --format '{{.Manifest.Digest}}')
          cosign sign --yes "${{ env.IMAGE }}@${DIGEST}"
```

- [ ] **Step 2: Verify YAML syntax**

```bash
python3 -c "import yaml, sys; yaml.safe_load(open('.github/workflows/bootc.yml'))" && echo "OK"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/bootc.yml
git commit -m "feat: add GitHub Actions workflow for bootc OS image"
```

---

## Task 8: Secrets Provisioning Script

**Files:**
- Create: `scripts/provision-secrets.sh`

This script documents and automates the one-time post-install secret placement. Run from a machine that holds the image-factory keys, targeting the newly installed etincelle VM.

- [ ] **Step 1: Create scripts/provision-secrets.sh**

```bash
#!/usr/bin/env bash
# One-time post-install secret provisioning for etincelle.
# Run from a host that has the image-factory PEM keys.
# Usage: ./scripts/provision-secrets.sh <hostname-or-ip>
set -euo pipefail

HOST="${1:?Usage: $0 <hostname-or-ip>}"
SSH="ssh core@${HOST}"
SCP="scp"

echo "==> Provisioning secrets on ${HOST}"

# image-factory signing keys
echo "--> Copying image-factory keys..."
$SSH sudo mkdir -p /etc/image-factory/keys
$SSH sudo chmod 750 /etc/image-factory/keys
for key in image-factory/keys/*.pem; do
    $SCP "${key}" "core@${HOST}:/tmp/$(basename "${key}")"
    $SSH sudo mv "/tmp/$(basename "${key}")" /etc/image-factory/keys/
    $SSH sudo chmod 640 "/etc/image-factory/keys/$(basename "${key}")"
done
echo "    Done."

# Cloudflare API token for Caddy
echo "--> Setting Cloudflare API token..."
$SSH sudo mkdir -p /etc/etincelle/secrets
$SSH sudo chmod 700 /etc/etincelle/secrets
read -rsp "Cloudflare API token: " CF_TOKEN
echo
printf 'CLOUDFLARE_API_TOKEN=%s\n' "${CF_TOKEN}" \
    | $SSH "sudo tee /etc/etincelle/secrets/caddy.env > /dev/null"
$SSH sudo chmod 600 /etc/etincelle/secrets/caddy.env
unset CF_TOKEN
echo "    Done."

echo "==> Secrets provisioned. Start services with:"
echo "    ssh core@${HOST} sudo systemctl start caddy.service image-factory.service"
```

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/provision-secrets.sh
```

- [ ] **Step 3: Verify bash syntax**

```bash
bash -n scripts/provision-secrets.sh
```

Expected: no output (no syntax errors).

- [ ] **Step 4: Commit**

```bash
git add scripts/provision-secrets.sh
git commit -m "feat: add post-install secrets provisioning script"
```

---

## Task 9: Push and Verify CI

- [ ] **Step 1: Push branch to GitHub**

```bash
git push -u origin main
```

- [ ] **Step 2: Watch Actions runs**

Open `https://github.com/jfroy/etincelle/actions`. Two workflows should trigger: `Caddy Image` and `bootc OS Image`. Each should show two parallel `build` jobs (amd64 + arm64) followed by a `merge` job.

- [ ] **Step 3: Verify images on ghcr.io**

After workflows complete:

```bash
docker buildx imagetools inspect ghcr.io/jfroy/etincelle:latest
docker buildx imagetools inspect ghcr.io/jfroy/caddy-etincelle:latest
```

Expected: each shows a manifest list with two entries — `linux/amd64` and `linux/arm64`.

- [ ] **Step 4: Verify cosign signatures**

```bash
cosign verify \
  --certificate-identity "https://github.com/jfroy/etincelle/.github/workflows/bootc.yml@refs/heads/main" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  ghcr.io/jfroy/etincelle:latest

cosign verify \
  --certificate-identity "https://github.com/jfroy/etincelle/.github/workflows/caddy.yml@refs/heads/main" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  ghcr.io/jfroy/caddy-etincelle:latest
```

Expected: `Verification for ghcr.io/jfroy/...:latest -- The following checks were performed...` with no errors.

---

## Post-Deployment Notes

After `bootc install` on the VM:

1. Run `./scripts/provision-secrets.sh <vm-ip>` from the machine holding the PEM keys.
2. SSH in and verify services: `sudo systemctl status caddy.service registry.service image-factory.service discovery-service.service`
3. Check Caddy obtained certs: `sudo podman logs caddy` — should show Let's Encrypt DNS-01 challenge success.
4. Test routes: `curl -I https://registry.etincelle.cloud/v2/` `curl -I https://tif.etincelle.cloud/`

The VM will auto-update the OS image via `bootc-fetch-apply-updates.timer` on each push to main. Container images auto-update via `podman-auto-update.timer`.
