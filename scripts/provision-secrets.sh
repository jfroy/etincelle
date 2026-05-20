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
