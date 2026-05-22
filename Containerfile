FROM quay.io/fedora/fedora-bootc:45

# Container runtime configuration. Image signature verification is currently
# disabled for ghcr.io/jfroy/* images; see issue tracking re-enablement once
# containers/container-libs#625 (buildSignerURI) lands.
COPY containers/policy.json /etc/containers/policy.json

# Quadlet service definitions
COPY containers/systemd/ /etc/containers/systemd/

# Create /var data directories at boot (bootc/ostree resets /var per deployment).
COPY system/tmpfiles.d/etincelle.conf /usr/lib/tmpfiles.d/etincelle.conf

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
