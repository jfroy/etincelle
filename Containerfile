FROM quay.io/fedora/fedora-bootc:42

# Container runtime configuration
COPY containers/policy.json /etc/containers/policy.json

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
