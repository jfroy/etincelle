#!/usr/bin/env bash
# One-time post-install secret provisioning for etincelle.
# Reads secrets from 1Password (requires `op` CLI, active session).
# Usage: ./scripts/provision-secrets.sh <hostname-or-ip>
#   SSH_USER (env, default: etincelle) — SSH login user on the target VM
set -euo pipefail

HOST="${1:?Usage: $0 <hostname-or-ip>}"
SSH_USER="${SSH_USER:-etincelle}"
SSH="ssh ${SSH_USER}@${HOST}"

echo "==> Provisioning secrets on ${HOST}"

echo "--> Copying image-factory keys..."
$SSH sudo mkdir -p /etc/image-factory/keys
$SSH sudo chmod 750 /etc/image-factory/keys

mapfile -t filenames < <(op item get "image factory keys" --vault kantai --format json \
    | jq -r '.files[].name')

for filename in "${filenames[@]}"; do
    op read "op://kantai/image factory keys/${filename}" \
        | $SSH "sudo tee /etc/image-factory/keys/${filename} > /dev/null"
    $SSH sudo chmod 640 "/etc/image-factory/keys/${filename}"
done
echo "    Done."

echo "--> Setting Cloudflare API token..."
$SSH sudo mkdir -p /etc/etincelle/secrets
$SSH sudo chmod 700 /etc/etincelle/secrets
CF_TOKEN=$(op read "op://kantai/cloudflare-etincelle/credential")
printf 'CLOUDFLARE_API_TOKEN=%s\n' "${CF_TOKEN}" \
    | $SSH "sudo tee /etc/etincelle/secrets/caddy.env > /dev/null"
$SSH sudo chmod 600 /etc/etincelle/secrets/caddy.env
unset CF_TOKEN
echo "    Done."

echo "==> Starting services..."
$SSH sudo systemctl start caddy.service image-factory.service
echo "==> Done."
