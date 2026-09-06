#!/usr/bin/env bash
# One-time post-install secret provisioning for etincelle.
# Reads secrets from 1Password (requires `op` CLI, active session).
# Usage: ./scripts/provision-secrets.sh <hostname-or-ip>
#   SSH_USER   (env, default: etincelle) — SSH login user on the target VM
#   TS_AUTHKEY (env, optional)           — Tailscale auth key; prompted if unset
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

echo "--> Setting Beszel agent token..."
BESZEL_TOKEN=$(op read "op://kantai/beszel-etincelle/TOKEN")
printf 'TOKEN=%s\n' "${BESZEL_TOKEN}" \
    | $SSH "sudo tee /etc/etincelle/secrets/beszel-agent.env > /dev/null"
$SSH sudo chmod 600 /etc/etincelle/secrets/beszel-agent.env
unset BESZEL_TOKEN
echo "    Done."

echo "--> Setting Trove agent token..."
TROVE_TOKEN=$(op read "op://kantai/trove-etincelle/TOKEN")
printf 'TROVE_TOKEN=%s\n' "${TROVE_TOKEN}" \
    | $SSH "sudo tee /etc/etincelle/secrets/trove-agent.env > /dev/null"
$SSH sudo chmod 600 /etc/etincelle/secrets/trove-agent.env
unset TROVE_TOKEN
echo "    Done."

echo "--> Setting OpenBao seal key..."
OPENBAO_SEAL_KEY=$(op read "op://kantai/openbao-etincelle/seal-key" 2> /dev/null || true)
if [[ -z "${OPENBAO_SEAL_KEY}" ]]; then
    if op item get openbao-etincelle --vault kantai > /dev/null 2>&1; then
        op_cmd='op item edit openbao-etincelle --vault kantai'
    else
        op_cmd='op item create --vault kantai --category "Secure Note" --title openbao-etincelle'
    fi
    cat <<MSG
    No seal key at op://kantai/openbao-etincelle/seal-key. OpenBao cannot start
    (or ever unseal again) without it, so it must exist in 1Password before it is
    used. Create it with:

      ${op_cmd} "seal-key[password]=\$(openssl rand -hex 32)"

MSG
    read -rp "    Generate and store it in 1Password now? [y/N] " answer
    if [[ "${answer}" =~ ^[Yy]$ ]]; then
        eval "${op_cmd}" "seal-key[password]=$(openssl rand -hex 32)" > /dev/null
        OPENBAO_SEAL_KEY=$(op read "op://kantai/openbao-etincelle/seal-key")
    else
        echo "    Aborting: create the seal key and re-run." >&2
        exit 1
    fi
fi
if [[ ! "${OPENBAO_SEAL_KEY}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "    ERROR: seal-key must be exactly 64 hex characters (32 bytes)." >&2
    exit 1
fi
# The static seal does not trim whitespace: write exactly the 64 hex characters.
printf '%s' "${OPENBAO_SEAL_KEY}" \
    | $SSH "sudo tee /etc/etincelle/secrets/openbao-seal.key > /dev/null"
unset OPENBAO_SEAL_KEY
# The key file is bind-mounted into the container, which runs as the image's
# unprivileged user: make it readable by that UID only.
OPENBAO_IMAGE=$($SSH "sed -n 's/^Image=//p' /etc/containers/systemd/openbao.container")
OPENBAO_UID=$($SSH "sudo podman run --rm --entrypoint id ${OPENBAO_IMAGE} -u")
$SSH sudo chown "${OPENBAO_UID}:root" /etc/etincelle/secrets/openbao-seal.key
$SSH sudo chmod 400 /etc/etincelle/secrets/openbao-seal.key
echo "    Done (owner uid ${OPENBAO_UID})."

echo "--> Setting OpenBao backup (R2) credentials..."
R2_ACCESS_KEY_ID=$(op read "op://kantai/openbao-backup-r2/access-key-id")
R2_SECRET_ACCESS_KEY=$(op read "op://kantai/openbao-backup-r2/secret-access-key")
R2_ENDPOINT=$(op read "op://kantai/openbao-backup-r2/endpoint")
R2_BUCKET=$(op read "op://kantai/openbao-backup-r2/bucket")
printf 'RCLONE_CONFIG_R2_ACCESS_KEY_ID=%s\nRCLONE_CONFIG_R2_SECRET_ACCESS_KEY=%s\nRCLONE_CONFIG_R2_ENDPOINT=%s\nOPENBAO_BACKUP_BUCKET=%s\n' \
    "${R2_ACCESS_KEY_ID}" "${R2_SECRET_ACCESS_KEY}" "${R2_ENDPOINT}" "${R2_BUCKET}" \
    | $SSH "sudo tee /etc/etincelle/secrets/openbao-backup.env > /dev/null"
$SSH sudo chmod 600 /etc/etincelle/secrets/openbao-backup.env
unset R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_ENDPOINT R2_BUCKET
echo "    Done."

echo "--> Setting OpenBao snapshot AppRole credentials..."
if SNAPSHOT_ROLE_ID=$(op read "op://kantai/openbao-snapshot-etincelle/role-id" 2> /dev/null); then
    SNAPSHOT_SECRET_ID=$(op read "op://kantai/openbao-snapshot-etincelle/secret-id")
    printf 'OPENBAO_ROLE_ID=%s\nOPENBAO_SECRET_ID=%s\n' "${SNAPSHOT_ROLE_ID}" "${SNAPSHOT_SECRET_ID}" \
        | $SSH "sudo tee /etc/etincelle/secrets/openbao-snapshot.env > /dev/null"
    $SSH sudo chmod 600 /etc/etincelle/secrets/openbao-snapshot.env
    unset SNAPSHOT_ROLE_ID SNAPSHOT_SECRET_ID
    echo "    Done."
else
    echo "    Skipped (not in 1Password yet; created by 'task openbao-init')."
fi

echo "==> Starting services..."
$SSH sudo systemctl start caddy.service image-factory.service beszel-agent.service trove-agent.service openbao.service
echo "    Done."

echo "==> Joining Tailscale..."
if [[ -z "${TS_AUTHKEY:-}" ]]; then
    read -rsp "    Tailscale auth key (empty to skip): " TS_AUTHKEY
    echo
fi
if [[ -n "${TS_AUTHKEY:-}" ]]; then
    $SSH sudo tailscale up --auth-key="${TS_AUTHKEY}"
    unset TS_AUTHKEY
    echo "    Done."
else
    echo "    Skipped (run 'sudo tailscale up' on the host to authenticate later)."
fi

echo "==> Done."
