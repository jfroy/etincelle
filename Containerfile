FROM quay.io/fedora/fedora-bootc:42

# Container runtime configuration
COPY containers/policy.json /etc/containers/policy.json

# Enable sigstore attachment lookup so cosign-style signatures attached as OCI
# artifacts (sha256-<digest>.sig) are discovered during pull. Default
# containers/image behavior only checks the legacy lookaside path.
COPY system/registries.d/sigstore-attachments.yaml /etc/containers/registries.d/sigstore-attachments.yaml

# Sigstore public-good trust roots (refreshed on every image build).
# Referenced by /etc/containers/policy.json for keyless verification.
RUN mkdir -p /etc/pki/sigstore/roots && \
    curl -fsSL -o /etc/pki/sigstore/roots/fulcio_v1.crt.pem https://fulcio.sigstore.dev/api/v1/rootCert && \
    curl -fsSL -o /etc/pki/sigstore/roots/rekor.pub https://rekor.sigstore.dev/api/v1/log/publicKey

# Quadlet service definitions
COPY containers/systemd/ /etc/containers/systemd/

# Service configurations (managed via git, baked into image)
COPY caddy/Caddyfile /etc/caddy/Caddyfile
COPY image-factory/config.yaml /etc/image-factory/config.yaml
COPY registry/config.yml /etc/registry/config.yml

# Passwordless sudo for the wheel group
COPY system/wheel-nopasswd /etc/sudoers.d/wheel-nopasswd
RUN chmod 0440 /etc/sudoers.d/wheel-nopasswd

# Tailscale (auth performed post-install via scripts/provision-secrets.sh)
COPY system/tailscale.repo /etc/yum.repos.d/tailscale.repo
RUN dnf -y install tailscale && dnf clean all

# Secrets directories (contents provisioned post-install, never in image)
RUN mkdir -p /etc/etincelle/secrets && chmod 700 /etc/etincelle/secrets && \
    mkdir -p /etc/image-factory/keys && chmod 750 /etc/image-factory/keys

# Enable auto-update timers and tailscaled
RUN systemctl enable \
    bootc-fetch-apply-updates.timer \
    podman-auto-update.timer \
    tailscaled.service

RUN echo "etincelle" > /etc/hostname
